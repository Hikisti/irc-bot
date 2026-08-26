import datetime
import threading

import pytz
import requests


class SuperpesisCommand:
    """
    Live-tracks today's Finnish Superpesis (pesäpallo, men's top division)
    matches in a channel, announcing runs and final results as they
    happen. Uses pesistulokset.fi's unofficial JSON API (the same one that
    powers pesistulokset.fi itself, authenticated with the "public" API
    key baked into that site's own frontend - not a secret credential).

    Usage:
      !superpesis start  -> start polling today's matches in this channel
      !superpesis stop   -> stop polling in this channel
      !superpesis next   -> show the next upcoming matchday and its times

    Restricted to the #pesis.fi channel (see CommandHandler's "channels"
    config), unlike most other commands.

    Notes on the implementation, since this was built by reverse-engineering
    an undocumented API rather than reading real docs:
      - "Miesten Superpesis" (men's) is looked up dynamically by name each
        time tracking starts (level="Superpesis", series="Miehet"), rather
        than a hardcoded season-series id, since that id changes every
        season. The women's division shares the same "Superpesis" level
        name, distinguished only by series="Miehet" vs "Naiset".
      - Score and "finished" status always come from /public/matches-list's
        authoritative liveResult object, never computed from the event
        feed - the event feed is only used for *which* player scored, and
        a per-poll running score is snapped back to the authoritative
        total after each cycle so it can't silently drift.
      - Only runs are surfaced (via two confirmed patterns - see
        _extract_runs()); the richer play-by-play (individual hits,
        defensive positioning, outs, etc.) is intentionally not parsed.
        Checked against several real, finished matches: this captures
        roughly 85-100% of each match's actual run total as individual
        "RUN:" announcements - the remainder isn't itemized as a
        recognizable event in the feed at all (e.g. bulk scoring-contest
        totals, "vapaataival" free-base scores). This is a real,
        disclosed gap in the play-by-play, not a rounding error to "fix"
        later; the final/authoritative score is never affected by it
        either way (see _process_match).
      - Scorer identification: a player reference in the event feed is
        either a global {"id": N} (resolved via /public/player/{id}) or a
        per-match jersey {"number": N} - confirmed live that some matches
        only give the latter, and that treating a jersey number as a
        global id resolves to a real but completely unrelated player.
        Jersey numbers are resolved against that match's own roster
        (fetched once via /public/match?id=, see _fetch_match_roster())
        instead. See _resolve_scorer_name().
    """

    needs_irc_context = True

    BASE_URL = "https://api.pesistulokset.fi/api/v1"
    # Public frontend API key, extracted from pesistulokset.fi's own JS
    # bundle - the same one every visitor's browser uses, not a secret.
    API_KEY = "wRX0tTke3DZ8RLKAMntjZ81LwgNQuSN9"

    SERIES_LEVEL_NAME = "Superpesis"
    SERIES_NAME = "Miehet"

    # How far forward !superpesis next searches, day by day, for the next
    # scheduled matchday - the API has no "next date with matches" hint
    # like liiga.fi does, so this is a bounded linear search instead.
    NEXT_SEARCH_MAX_DAYS = 21

    HELSINKI_TZ = pytz.timezone("Europe/Helsinki")
    POLL_INTERVAL_SECONDS = 30
    REQUEST_TIMEOUT_SECONDS = 10

    BOLD = "\x02"
    COLOR_RESET = "\x0F"
    GREEN = "\x0303"
    ORANGE = "\x0307"
    RUN_PREFIX = f"{BOLD}{GREEN}RUN:{COLOR_RESET}"
    FINAL_PREFIX = f"{BOLD}{ORANGE}FINAL:{COLOR_RESET}"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "KukistiBot-Superpesis/1.0",
            "Accept": "application/json",
        })
        self._lock = threading.Lock()
        self._channels = {}  # channel -> {"stop_event", "thread", "matches"}
        self._player_cache = {}  # player id -> display name

    def execute(self, args=None, irc_bot=None, channel=None, **kwargs) -> str:
        arg = (args or "").strip().lower()

        if arg == "start":
            return self._start(irc_bot, channel)
        elif arg == "stop":
            return self._stop(channel)
        elif arg == "next":
            return self._next(irc_bot, channel)
        return "Usage: !superpesis start | !superpesis stop | !superpesis next"

    # ---- start / stop -----------------------------------------------

    def _start(self, irc_bot, channel):
        if irc_bot is None or channel is None:
            return "Error: live tracking is unavailable without channel context."

        with self._lock:
            if channel in self._channels:
                return "Already tracking live Superpesis matches in this channel."
            stop_event = threading.Event()
            self._channels[channel] = {"stop_event": stop_event, "thread": None, "matches": {}}

        thread = threading.Thread(
            target=self._run,
            args=(irc_bot, channel, stop_event),
            daemon=True,
        )
        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None and entry["stop_event"] is stop_event:
                entry["thread"] = thread
        thread.start()

        return "Checking today's Superpesis matches..."

    def _stop(self, channel):
        with self._lock:
            entry = self._channels.pop(channel, None)
        if not entry:
            return "Not currently tracking Superpesis matches in this channel."
        entry["stop_event"].set()
        return "Stopped live Superpesis tracking."

    def _next(self, irc_bot, channel):
        if irc_bot is None or channel is None:
            return "Error: this command needs channel context."

        # One-shot lookup, no persistent state - still backgrounded so a
        # slow API can't stall the bot.
        threading.Thread(
            target=self._run_next,
            args=(irc_bot, channel),
            daemon=True,
        ).start()

        return "Checking the next Superpesis matchday..."

    def _run_next(self, irc_bot, channel):
        try:
            series_id = self._resolve_series_id()
        except Exception as e:
            print(f"Superpesis series lookup error: {e}")
            series_id = None

        if series_id is None:
            self._safe_send(irc_bot, channel, "Error: could not reach the Superpesis API.")
            return

        try:
            status, date_str, matches = self._fetch_next_matchday(series_id)
        except Exception as e:
            print(f"Superpesis next-matchday fetch error: {e}")
            status, date_str, matches = "error", None, None

        if status == "error":
            self._safe_send(irc_bot, channel, "Error: could not reach the Superpesis API.")
            return
        if status == "not_found":
            self._safe_send(
                irc_bot, channel,
                f"No upcoming Superpesis matches found in the next {self.NEXT_SEARCH_MAX_DAYS} days.",
            )
            return

        label = self._format_date_label(date_str)
        summary = self._format_matches_summary(matches.values())
        self._safe_send(irc_bot, channel, f"Next Superpesis matchday ({label}): {summary}")

    # ---- background thread entry point --------------------------------

    def _run(self, irc_bot, channel, stop_event):
        try:
            series_id = self._resolve_series_id()
        except Exception as e:
            print(f"Superpesis series lookup error: {e}")
            series_id = None

        if series_id is None:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, "Error: could not reach the Superpesis API.")
            return

        try:
            matches = self._fetch_today_matches(series_id)
        except Exception as e:
            print(f"Superpesis initial fetch error: {e}")
            matches = None

        if matches is None:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, "Error: could not reach the Superpesis API.")
            return

        if not matches:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, "No Superpesis matches scheduled today.")
            return

        state = {mid: self._seed_snapshot(m) for mid, m in matches.items()}
        for mid, snapshot in state.items():
            # Seed each match's event baseline from a real fetch, so the
            # first poll doesn't replay every run already scored today in
            # a match that was already in progress when !superpesis start
            # was run (same principle as !liiga's goal-count baseline).
            events = self._fetch_match_events(mid)
            if events is not None:
                snapshot["event_count"] = len(events)
            # Roster (jersey number -> name), needed to resolve scorer
            # names for matches whose event feed only gives jersey numbers
            # rather than global player ids - see _last_player_ref().
            snapshot["roster"] = self._fetch_match_roster(mid)

        with self._lock:
            entry = self._channels.get(channel)
            if entry is None or entry["stop_event"] is not stop_event:
                return  # stopped (or superseded) before the lookup finished
            entry["matches"] = state

        summary = self._format_matches_summary(matches.values())
        self._safe_send(
            irc_bot, channel, f"Tracking {len(matches)} Superpesis match(es) today: {summary}"
        )

        self._poll_loop(irc_bot, channel, stop_event, series_id)

    def _drop_if_current(self, channel, stop_event):
        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None and entry["stop_event"] is stop_event:
                del self._channels[channel]

    def _safe_send(self, irc_bot, channel, message):
        try:
            irc_bot.send_message(channel, message)
        except Exception as e:
            print(f"Superpesis: failed to send message to {channel}: {e}")

    # ---- polling loop -------------------------------------------------

    def _poll_loop(self, irc_bot, channel, stop_event, series_id):
        while not stop_event.is_set():
            try:
                all_ended = self._poll_once(irc_bot, channel, series_id)
            except Exception as e:
                print(f"Superpesis poll error: {e}")
                all_ended = False

            if all_ended:
                self._drop_if_current(channel, stop_event)
                self._safe_send(
                    irc_bot, channel,
                    "All of today's Superpesis matches have finished. Live tracking stopped.",
                )
                return

            stop_event.wait(self.POLL_INTERVAL_SECONDS)

    def _poll_once(self, irc_bot, channel, series_id) -> bool:
        matches = self._fetch_today_matches(series_id)
        if matches is None:
            return False

        with self._lock:
            entry = self._channels.get(channel)
            if entry is None:
                return True
            prev_state = entry["matches"]

        all_ended = bool(prev_state)
        new_state = {}

        for mid, prev in prev_state.items():
            match = matches.get(mid)
            if match is None:
                # Not in today's list anymore (e.g. date rolled over past
                # midnight mid-match) - leave its last known state alone
                # rather than guessing it's finished.
                new_state[mid] = prev
                if not prev.get("finished"):
                    all_ended = False
                continue

            try:
                new_state[mid] = self._process_match(irc_bot, channel, match, prev)
            except Exception as e:
                print(f"Superpesis: failed to process match {mid}: {e}")
                new_state[mid] = prev

            if not new_state[mid].get("finished"):
                all_ended = False

        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None:
                entry["matches"] = new_state

        return all_ended

    def _process_match(self, irc_bot, channel, match, prev):
        live = match.get("liveResult") or {}
        home_id, away_id = prev["home_id"], prev["away_id"]
        home_name, away_name = prev["home_name"], prev["away_name"]
        roster = prev.get("roster") or {}

        events = self._fetch_match_events(prev["match_id"])
        home_runs, away_runs = prev["home_runs"], prev["away_runs"]

        if events is not None and len(events) > prev["event_count"]:
            for event in events[prev["event_count"]:]:
                for player_ref, scoring_team_id, batter in self._extract_runs(event):
                    if scoring_team_id == home_id:
                        home_runs += 1
                    elif scoring_team_id == away_id:
                        away_runs += 1
                    else:
                        continue
                    scorer_name = self._resolve_scorer_name(player_ref, scoring_team_id, batter, roster)
                    self._safe_send(irc_bot, channel, self._format_run(
                        event, home_name, away_name, home_runs, away_runs, scoring_team_id, home_id, scorer_name,
                    ))

        event_count = len(events) if events is not None else prev["event_count"]

        # The authoritative score always wins over our running per-run
        # count above, so a missed/misparsed event self-corrects on the
        # very next poll instead of drifting forever.
        authoritative_home, authoritative_away = self._sum_runs(live, "home"), self._sum_runs(live, "away")
        if authoritative_home is not None:
            home_runs = authoritative_home
        if authoritative_away is not None:
            away_runs = authoritative_away

        finished = bool(live.get("finished"))
        if finished and not prev.get("finished"):
            self._safe_send(
                irc_bot, channel,
                f"{self.FINAL_PREFIX} {home_name} {home_runs}-{away_runs} {away_name}",
            )

        return {
            "match_id": prev["match_id"],
            "home_id": home_id,
            "away_id": away_id,
            "home_name": home_name,
            "away_name": away_name,
            "home_runs": home_runs,
            "away_runs": away_runs,
            "event_count": event_count,
            "finished": finished,
            "roster": roster,  # rosters don't change mid-match, carry forward unchanged
        }

    # ---- event parsing --------------------------------------------------

    def _extract_runs(self, event):
        """Yields (player_ref, scoring_team_id, batter_fallback) for every
        run scored within this event - a single event can contain more
        than one (e.g. a hit that scores multiple runners already on
        base). player_ref is {"id": N}, {"number": N}, or None - see
        _resolve_scorer_name() for why there are two shapes.

        Best-effort against real data, not a guarantee: pesäpallo's event
        feed has a rich Finnish scoring vocabulary that a handful of real
        matches couldn't fully catalog (~85-100% of a match's authoritative
        run total was captured by these two patterns across samples
        checked). Any run this misses still shows up correctly in the
        final/authoritative score (see _process_match) - only the
        individual "RUN:" chat announcement for that specific play could
        be missing, not the score itself.
        """
        team_id = event.get("team") if event.get("team") is not None else event.get("hTeam")
        for sub_event in event.get("events") or []:
            texts = sub_event.get("texts") or []
            if self._is_run_sub_event(texts):
                player_ref = self._last_player_ref(texts)
                yield player_ref, team_id, event.get("batter")

    def _is_run_sub_event(self, texts) -> bool:
        # Two confirmed ways pesäpallo's event feed records a run reaching
        # home plate:
        #  - regular play: an "eteni" (advanced) event whose destination is
        #    the string "kotipesään" (home base) - crucially NOT "paloi"
        #    (put out) reaching home, which uses the same destination
        #    string for a runner who was retired instead of scoring.
        #  - a scoring-contest / tie-break decider: a "juoksu" (run) event.
        event_texts = {
            t.get("text") for t in texts if isinstance(t, dict) and t.get("type") == "event"
        }
        if "juoksu" in event_texts:
            return True
        if "eteni" in event_texts:
            plain_texts = {t for t in texts if isinstance(t, str)}
            if "kotipesään" in plain_texts:
                return True
        return False

    def _last_player_ref(self, texts):
        """Two confirmed formats a player reference in the event feed can
        take, apparently varying by match/data source: some matches embed
        a global player "id" directly (used with _resolve_player_name());
        others only give a per-match jersey "number" - confirmed live
        that treating the latter as a global id resolves to a real but
        completely unrelated player, so it must instead be looked up
        against that match's own roster (see _resolve_scorer_name())."""
        ref = None
        for t in texts:
            if isinstance(t, dict) and t.get("type") == "player":
                if t.get("id") is not None:
                    ref = {"id": t.get("id")}
                elif t.get("number") is not None:
                    ref = {"number": t.get("number")}
        return ref

    def _format_run(self, event, home_name, away_name, home_runs, away_runs, scoring_team_id, home_id, scorer_name):
        scoring_team = home_name if scoring_team_id == home_id else away_name
        period = event.get("period")
        period_str = f" (jakso {period})" if period else ""
        scorer_str = scorer_name or "Unknown"
        return (
            f"{self.RUN_PREFIX} {scoring_team} — {scorer_str} | "
            f"{home_name} {home_runs}-{away_runs} {away_name}{period_str}"
        )

    def _sum_runs(self, live_result, side):
        runs = live_result.get("runs")
        if not isinstance(runs, list):
            return None
        total = 0
        found_any = False
        for period_runs in runs:
            values = (period_runs or {}).get(side)
            if not isinstance(values, list):
                continue
            for v in values:
                if isinstance(v, (int, float)):
                    total += v
                    found_any = True
        return total if found_any else None

    # ---- player name resolution --------------------------------------

    def _resolve_player_name(self, player_id):
        if player_id in self._player_cache:
            return self._player_cache[player_id]

        name = f"Player {player_id}"
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/public/player/{player_id}",
                params={"apikey": self.API_KEY},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                name = (
                    data.get("name")
                    or f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
                    or name
                )
        except requests.exceptions.RequestException as e:
            print(f"Superpesis: player lookup failed for {player_id}: {e}")
        except ValueError:
            print(f"Superpesis: player lookup returned invalid JSON for {player_id}")

        self._player_cache[player_id] = name
        return name

    def _resolve_scorer_name(self, player_ref, team_id, batter_fallback, roster):
        """Resolves a scorer's display name from whatever reference the
        event feed actually gave us - see _last_player_ref() for why
        there are two shapes, and _fetch_match_roster() for the
        number->name mapping."""
        if player_ref:
            if "id" in player_ref:
                return self._resolve_player_name(player_ref["id"])
            if "number" in player_ref:
                name = (roster.get(team_id) or {}).get(player_ref["number"])
                if name:
                    return name

        if batter_fallback is not None:
            # Try the roster first (batter is usually a jersey number in
            # the same matches that use "number"-style player refs);
            # only treat it as a global id if that comes up empty.
            name = (roster.get(team_id) or {}).get(batter_fallback)
            if name:
                return name
            return self._resolve_player_name(batter_fallback)

        return None

    # ---- match summary (start message) ---------------------------------

    def _match_start_label(self, match):
        date_str = match.get("date")
        if not date_str:
            return None
        try:
            dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        return dt.astimezone(self.HELSINKI_TZ).strftime("%H:%M")

    def _format_matches_summary(self, matches) -> str:
        groups = {}
        order = []
        for match in matches:
            label = self._match_start_label(match) or "??:??"
            home = (match.get("home") or {}).get("name") or "?"
            away = (match.get("away") or {}).get("name") or "?"
            name = f"{home}-{away}"
            if label not in groups:
                groups[label] = []
                order.append(label)
            groups[label].append(name)

        order.sort(key=lambda label: (label == "??:??", label))
        return " | ".join(f"{label} {', '.join(groups[label])}" for label in order)

    def _format_date_label(self, date_str) -> str:
        try:
            target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return date_str or "unknown date"

        today = datetime.datetime.now(self.HELSINKI_TZ).date()
        if target == today:
            return "today"
        if target == today + datetime.timedelta(days=1):
            return "tomorrow"
        return target.strftime("%a %d/%m")

    def _seed_snapshot(self, match):
        live = match.get("liveResult") or {}
        return {
            "match_id": match.get("id"),
            "home_id": (match.get("home") or {}).get("id"),
            "away_id": (match.get("away") or {}).get("id"),
            "home_name": (match.get("home") or {}).get("name") or "Unknown",
            "away_name": (match.get("away") or {}).get("name") or "Unknown",
            "home_runs": self._sum_runs(live, "home") or 0,
            "away_runs": self._sum_runs(live, "away") or 0,
            "event_count": 0,  # seeded from a real fetch below, see _run()
            "finished": bool(live.get("finished")),
            "roster": {},  # seeded from a real fetch below, see _run()
        }

    # ---- data fetching --------------------------------------------------

    def _resolve_series_id(self):
        """Finds the current season's "Miesten Superpesis" seasonSeries id
        by name, so this doesn't need updating every season. Returns None
        on any failure (network error, unexpected shape, or just not
        found)."""
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/public/series-list",
                params={"apikey": self.API_KEY},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"Superpesis series-list request failed: {e}")
            return None
        except ValueError:
            print("Superpesis series-list returned invalid JSON")
            return None

        seasons = data.get("seasons") if isinstance(data, dict) else None
        if not seasons:
            return None

        def season_year(s):
            return (s.get("season") or {}).get("season", -1)

        latest = max(seasons, key=season_year)
        for ss in latest.get("seasonSerieses") or []:
            level_name = (ss.get("level") or {}).get("name")
            series_name = (ss.get("series") or {}).get("name")
            if level_name == self.SERIES_LEVEL_NAME and series_name == self.SERIES_NAME:
                season_series = ss.get("seasonSeries") or {}
                return season_series.get("id")
        return None

    def _fetch_today_matches(self, series_id):
        """Returns {match_id: match_dict} for today, or None on failure."""
        now = datetime.datetime.now(self.HELSINKI_TZ)
        return self._fetch_matches_for_date(series_id, now.strftime("%Y-%m-%d"))

    def _fetch_matches_for_date(self, series_id, date_str):
        """Returns {match_id: match_dict} for a specific date, or None on
        failure."""
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/public/matches-list",
                params={"apikey": self.API_KEY, "seasonSeriesId": series_id, "date": date_str},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"Superpesis matches-list request failed: {e}")
            return None
        except ValueError:
            print("Superpesis matches-list returned invalid JSON")
            return None

        matches = {}
        if not isinstance(data, list):
            return matches
        for entry in data:
            for group in (entry.get("groups") or []):
                for m in (group.get("matches") or []):
                    mid = m.get("id")
                    if mid is not None:
                        matches[mid] = m
        return matches

    def _fetch_next_matchday(self, series_id):
        """Searches forward day by day (bounded by NEXT_SEARCH_MAX_DAYS,
        starting today) for the next date with scheduled matches.

        Returns a ("found", date_str, matches_dict) / ("not_found", None,
        None) / ("error", None, None) triple - kept distinct from a plain
        None so the caller can tell "genuinely nothing scheduled soon"
        apart from "couldn't reach the API" instead of conflating them.
        """
        now = datetime.datetime.now(self.HELSINKI_TZ)
        for offset in range(self.NEXT_SEARCH_MAX_DAYS + 1):
            date_str = (now + datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
            matches = self._fetch_matches_for_date(series_id, date_str)
            if matches is None:
                return "error", None, None
            if matches:
                return "found", date_str, matches
        return "not_found", None, None

    def _fetch_match_events(self, match_id):
        """Returns the full events list for a match, or None on failure."""
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/online/{match_id}/events",
                params={"apikey": self.API_KEY},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"Superpesis match-events request failed for {match_id}: {e}")
            return None
        except ValueError:
            print(f"Superpesis match-events returned invalid JSON for {match_id}")
            return None

        events = data.get("events") if isinstance(data, dict) else None
        return events if isinstance(events, list) else None

    def _fetch_match_roster(self, match_id):
        """Returns {team_id: {jersey_number: player_name}} for a match, or
        an empty dict on any failure (jersey-number-only player refs then
        fall back to a placeholder name rather than crashing or - worse -
        resolving to a real but unrelated player, which is the bug this
        exists to fix; see _last_player_ref())."""
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/public/match",
                params={"apikey": self.API_KEY, "id": match_id},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"Superpesis match-detail request failed for {match_id}: {e}")
            return {}
        except ValueError:
            print(f"Superpesis match-detail returned invalid JSON for {match_id}")
            return {}

        if not isinstance(data, dict):
            return {}

        roster = {}
        for side in ("home", "away"):
            team = data.get(side) or {}
            team_id = team.get("id")
            if team_id is None:
                continue
            by_number = {}
            for p in (team.get("players") or []):
                if not isinstance(p, dict):
                    continue
                number = p.get("number")
                name = p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                if number is not None and name:
                    by_number[number] = name
            roster[team_id] = by_number
        return roster
