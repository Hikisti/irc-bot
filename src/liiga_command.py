import datetime
import threading
import traceback

import pytz
import requests


class LiigaCommand:
    """
    Live-tracks today's Finnish Liiga (ice hockey) games in a channel,
    announcing goals and final scores as they happen. Uses the unofficial
    liiga.fi JSON API (the same one that powers the liiga.fi site).

    Usage:
      !liiga start  -> start polling today's games in this channel
      !liiga stop   -> stop polling in this channel
      !liiga next   -> show the next upcoming gameday's games and times

    All network I/O (the initial lookup and every later poll) happens on a
    background thread, never on the caller's thread, so a slow or hanging
    liiga.fi response can't stall the bot's main IRC loop.
    """

    # Tells CommandHandler to pass irc_bot/channel into execute().
    needs_irc_context = True

    BASE_URL = "https://www.liiga.fi/api/v2/games"
    TOURNAMENTS = ["runkosarja", "playoffs", "playout", "qualifications", "valmistavat_ottelut"]
    HELSINKI_TZ = pytz.timezone("Europe/Helsinki")
    POLL_INTERVAL_SECONDS = 30
    REQUEST_TIMEOUT_SECONDS = 10

    # Fallback bound for !liiga next's day-by-day search when today had
    # games but they're all already finished (so the API's own
    # "nextGameDate" hint isn't available - it's only given when a date
    # has no games at all, see _fetch_next_gameday()).
    NEXT_SEARCH_MAX_DAYS = 21

    PERIOD_LABELS = {1: "1st", 2: "2nd", 3: "3rd", 4: "OT", 5: "SO"}

    # mIRC formatting codes. Bold + a mid-saturation color so the prefix
    # stays legible regardless of the viewer's background (black/white/blue/etc).
    BOLD = "\x02"
    COLOR_RESET = "\x0F"
    GREEN = "\x0303"
    ORANGE = "\x0307"
    GOAL_PREFIX = f"{BOLD}{GREEN}GOAL:{COLOR_RESET}"
    FINAL_PREFIX = f"{BOLD}{ORANGE}FINAL:{COLOR_RESET}"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "KukistiBot-Liiga/1.0",
            "Accept": "application/json",
        })
        self._lock = threading.Lock()
        self._channels = {}  # channel -> {"stop_event", "thread", "games"}

    def execute(self, args=None, irc_bot=None, channel=None, **kwargs) -> str:
        arg = (args or "").strip().lower()

        if arg == "start":
            return self._start(irc_bot, channel)
        elif arg == "stop":
            return self._stop(channel)
        elif arg == "next":
            return self._next(irc_bot, channel)
        return "Usage: !liiga start | !liiga stop | !liiga next"

    # ---- start / stop -----------------------------------------------

    def _start(self, irc_bot, channel):
        if irc_bot is None or channel is None:
            return "Error: live tracking is unavailable without channel context."

        with self._lock:
            if channel in self._channels:
                return "Already tracking live Liiga games in this channel."
            # Reserve the slot up front (before any network I/O) so a second
            # !liiga start can't race in while the first lookup is in flight.
            stop_event = threading.Event()
            self._channels[channel] = {"stop_event": stop_event, "thread": None, "games": {}}

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

        return "Checking today's Liiga games..."

    def _stop(self, channel):
        with self._lock:
            entry = self._channels.pop(channel, None)
        if not entry:
            return "Not currently tracking Liiga games in this channel."
        entry["stop_event"].set()
        return "Stopped live Liiga tracking."

    def _next(self, irc_bot, channel):
        if irc_bot is None or channel is None:
            return "Error: this command needs channel context."

        # A one-shot lookup, not persistent tracking - no need to reserve a
        # channel slot the way !liiga start does. Still runs on a background
        # thread so a slow API can't stall the bot.
        threading.Thread(
            target=self._run_next,
            args=(irc_bot, channel),
            daemon=True,
        ).start()

        return "Checking the next Liiga gameday..."

    def _run_next(self, irc_bot, channel):
        try:
            date_str, games = self._fetch_next_gameday()
        except Exception as e:
            print(f"Liiga next-gameday fetch error: {e}")
            date_str, games = None, None

        if games is None:
            self._safe_send(irc_bot, channel, "Error: could not reach the Liiga API.")
            return
        if not games:
            self._safe_send(irc_bot, channel, "No upcoming Liiga games found.")
            return

        label = self._format_date_label(date_str)
        summary = self._format_games_summary(games.values())
        self._safe_send(irc_bot, channel, f"Next Liiga gameday ({label}): {summary}")

    # ---- background thread entry point --------------------------------

    def _run(self, irc_bot, channel, stop_event):
        """Runs entirely on a background thread: does the initial lookup,
        reports what's being tracked (or bails out), then polls until the
        games are done or !liiga stop is called."""
        try:
            games = self._fetch_today_games()
        except Exception as e:
            print(f"Liiga initial fetch error: {e}")
            games = None

        if games is None:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, "Error: could not reach the Liiga API.")
            return

        if not games:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, "No Liiga games scheduled today.")
            return

        with self._lock:
            entry = self._channels.get(channel)
            if entry is None or entry["stop_event"] is not stop_event:
                return  # stopped (or superseded) before the lookup finished
            entry["games"] = {gid: self._snapshot(g) for gid, g in games.items()}

        summary = self._format_games_summary(games.values())
        self._safe_send(irc_bot, channel, f"Tracking {len(games)} Liiga game(s) today: {summary}")

        self._poll_loop(irc_bot, channel, stop_event)

    def _drop_if_current(self, channel, stop_event):
        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None and entry["stop_event"] is stop_event:
                del self._channels[channel]

    def _safe_send(self, irc_bot, channel, message):
        """Never let a broken connection/socket take the polling thread down."""
        try:
            irc_bot.send_message(channel, message)
        except Exception as e:
            print(f"Liiga: failed to send message to {channel}: {e}")

    # ---- polling loop -------------------------------------------------

    def _poll_loop(self, irc_bot, channel, stop_event):
        while not stop_event.is_set():
            try:
                all_ended = self._poll_once(irc_bot, channel)
            except Exception as e:
                # Last line of defense against anything not anticipated by
                # a narrower handler below - print the traceback too, not
                # just str(e), since nothing more specific caught this one.
                print(f"Liiga poll error in {channel}: {e}")
                traceback.print_exc()
                all_ended = False

            if all_ended:
                self._drop_if_current(channel, stop_event)
                self._safe_send(
                    irc_bot, channel, "All of today's Liiga games have finished. Live tracking stopped."
                )
                return

            stop_event.wait(self.POLL_INTERVAL_SECONDS)

    def _poll_once(self, irc_bot, channel) -> bool:
        """Fetch current game states and announce diffs since the last poll.

        Returns True once every tracked game has ended (nothing left to watch).
        """
        games = self._fetch_today_games()
        if games is None:
            return False

        with self._lock:
            entry = self._channels.get(channel)
            if entry is None:
                return True
            prev_state = entry["games"]

        all_ended = bool(games)
        new_state = {}

        for gid, game in games.items():
            try:
                prev = prev_state.get(gid)
                if prev is None:
                    # A game we weren't tracking yet (e.g. added after start).
                    # Seed a baseline silently instead of replaying old goals.
                    new_state[gid] = self._snapshot(game)
                    if not game.get("ended"):
                        all_ended = False
                    continue

                self._announce_new_goals(irc_bot, channel, game, prev, "homeTeam")
                self._announce_new_goals(irc_bot, channel, game, prev, "awayTeam")

                if game.get("ended") and not prev["ended"]:
                    self._announce_end(irc_bot, channel, game)

                new_state[gid] = self._snapshot(game)
                if not game.get("ended"):
                    all_ended = False
            except Exception as e:
                # Don't let one malformed game entry take down the whole poll
                # cycle (or the ones after it) - keep the previous state for
                # this game and try again next cycle. Traceback included
                # since str(e) alone often isn't enough to pinpoint where
                # in the processing this actually broke.
                print(f"Liiga: failed to process game {gid} in {channel}: {e}")
                traceback.print_exc()
                new_state[gid] = prev_state.get(gid) or self._snapshot(game)
                all_ended = False

        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None:
                entry["games"] = new_state

        return all_ended

    # ---- start-time summary -------------------------------------------

    def _game_start_label(self, game):
        """Scheduled start time in Helsinki local time as 'HH:MM', or None
        if the game has no usable start timestamp."""
        start = game.get("start")
        if not start:
            return None
        try:
            dt = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        return dt.astimezone(self.HELSINKI_TZ).strftime("%H:%M")

    def _format_games_summary(self, games) -> str:
        """Groups games by scheduled start time, e.g.
        '17:00 HIFK-Ilves, Tappara-Kärpät | 18:30 JYP-Lukko'."""
        groups = {}
        order = []
        for game in games:
            label = self._game_start_label(game) or "??:??"
            name = f"{self._team_name(game, 'homeTeam')}-{self._team_name(game, 'awayTeam')}"
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

    # ---- announcements --------------------------------------------------

    def _team_name(self, game, side) -> str:
        return (game.get(side) or {}).get("teamName") or "Unknown"

    def _team_goals(self, game, side):
        return (game.get(side) or {}).get("goals", "?")

    def _announce_new_goals(self, irc_bot, channel, game, prev, side):
        events = (game.get(side) or {}).get("goalEvents") or []
        key = "home_goals" if side == "homeTeam" else "away_goals"
        for event in events[prev[key]:]:
            self._safe_send(irc_bot, channel, self._format_goal(game, side, event))

    def _format_goal(self, game, side, event) -> str:
        home = self._team_name(game, "homeTeam")
        away = self._team_name(game, "awayTeam")
        scoring_team = home if side == "homeTeam" else away
        home_score = event.get("homeTeamScore", "?")
        away_score = event.get("awayTeamScore", "?")

        scorer = event.get("scorerPlayer") or {}
        scorer_name = f"{scorer.get('firstName', '')} {scorer.get('lastName', '')}".strip() or "Unknown"

        assists = event.get("assistantPlayers") or []
        assist_names = ", ".join(
            f"{a.get('firstName', '')} {a.get('lastName', '')}".strip() for a in assists
        )
        assist_str = f" (assists: {assist_names})" if assist_names else ""

        tags = event.get("goalTypes") or []
        tag_str = f" ({'/'.join(tags)})" if tags else ""

        period_label = self.PERIOD_LABELS.get(event.get("period"), "")
        clock = self._format_clock(game, event)
        time_str = f" {clock} {period_label}".rstrip() if clock else f" {period_label}".rstrip()

        return (
            f"{self.GOAL_PREFIX} {scoring_team} — {scorer_name}{tag_str}{assist_str} | "
            f"{home} {home_score}-{away_score} {away}{time_str}"
        )

    def _format_clock(self, game, event) -> str:
        game_time = event.get("gameTime")
        period = event.get("period")
        if game_time is None or period is None:
            return ""
        period_start = 0
        for p in game.get("periods") or []:
            if p.get("index") == period:
                period_start = p.get("startTime", 0)
                break
        elapsed = max(0, game_time - period_start)
        minutes, seconds = divmod(elapsed, 60)
        return f"{minutes:02d}:{seconds:02d}"

    def _announce_end(self, irc_bot, channel, game):
        home = self._team_name(game, "homeTeam")
        away = self._team_name(game, "awayTeam")
        home_goals = self._team_goals(game, "homeTeam")
        away_goals = self._team_goals(game, "awayTeam")

        finished = game.get("finishedType") or ""
        if "SHOOTOUT" in finished or "WINNING_SHOT" in finished:
            suffix_from_type = " (SO)"
        elif "OVERTIME" in finished:
            suffix_from_type = " (OT)"
        else:
            suffix_from_type = ""

        # Corroborating source, independent of finishedType's exact enum
        # wording: the periods list itself carries a "category" per
        # period, confirmed live (2701280, Sport-Jokerit,
        # 2026-09-01) to include "OVERTIME"/"WINNING_SHOT_COMPETITION"
        # entries. Checked in addition to finishedType (not instead of)
        # since a decided-but-scoreless overtime period can still appear
        # in the list even when nothing happened in it.
        suffix_from_periods = ""
        for period in game.get("periods") or []:
            category = period.get("category") or ""
            goals = (period.get("homeTeamGoals") or 0) + (period.get("awayTeamGoals") or 0)
            if category == "WINNING_SHOT_COMPETITION" and goals:
                suffix_from_periods = " (SO)"
                break
            if category == "OVERTIME" and goals:
                suffix_from_periods = " (OT)"

        suffix = suffix_from_type or suffix_from_periods
        if suffix_from_type != suffix_from_periods:
            # Diagnostic for a reported case where a real shootout final
            # was announced with no suffix at all - couldn't reproduce
            # the exact stale snapshot after the fact (by the time this
            # was investigated, a fresh fetch of that same game already
            # had a correct/consistent finishedType), so this is here to
            # capture hard evidence if it recurs rather than guessing.
            print(
                f"Liiga: end-suffix mismatch for game {game.get('id')} in {channel}: "
                f"finishedType={finished!r} (-> {suffix_from_type!r}) vs periods (-> {suffix_from_periods!r})"
            )

        self._safe_send(
            irc_bot, channel, f"{self.FINAL_PREFIX} {home} {home_goals}-{away_goals} {away}{suffix}"
        )

    # ---- data fetching --------------------------------------------------

    def _current_season(self, now_helsinki) -> int:
        # Liiga seasons start in September and are labelled by the year
        # they run into (Sept 2024 -> season 2025).
        return now_helsinki.year + 1 if now_helsinki.month >= 7 else now_helsinki.year

    def _fetch_today_games(self):
        """Returns {game_id: game_dict} for today, merged across tournaments.
        None only if every tournament request failed."""
        now = datetime.datetime.now(self.HELSINKI_TZ)
        games, _next_date = self._fetch_games_and_next_date(
            now.strftime("%Y-%m-%d"), self._current_season(now)
        )
        return games

    def _fetch_games_and_next_date(self, date_str, season):
        """Fetch every tournament's games for one date, merged together.

        Each tournament is fetched and error-handled independently, so a
        single failing/slow endpoint doesn't discard data already fetched
        from the others. Returns (games_dict, next_game_date):
          - games_dict is None only if every tournament request failed
            (i.e. we have no idea what's happening that day) - callers
            treat that as "API unreachable".
          - next_game_date is the earliest "nextGameDate" reported by any
            tournament for this date (liiga.fi returns this even when a
            date has no games, pointing at the next date that does), or
            None if none of them reported one.

        Confirmed live: once a tournament's own schedule is exhausted
        (e.g. valmistavat_ottelut/preseason ends and runkosarja takes
        over), its "nextGameDate" doesn't go null - it wraps back to
        that tournament's very first date, which can be weeks in the
        past relative to what was actually queried. A hint that isn't
        later than the queried date is exactly that wraparound, not a
        real "next date", so it's discarded rather than fed into
        min(next_dates) alongside a different tournament's real one.
        """
        games = {}
        next_dates = []
        any_success = False

        for tournament in self.TOURNAMENTS:
            try:
                resp = self.session.get(
                    self.BASE_URL,
                    params={"tournament": tournament, "season": season, "date": date_str},
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                data = resp.json()
                for g in (data.get("games") or []):
                    gid = g.get("id")
                    if gid is not None:
                        games[gid] = g
                next_game_date = data.get("nextGameDate")
                if next_game_date and next_game_date > date_str:
                    next_dates.append(next_game_date)
                any_success = True
            except requests.exceptions.RequestException as e:
                print(f"Liiga API request failed for tournament={tournament}: {e}")
            except (ValueError, AttributeError, TypeError) as e:
                print(f"Liiga API returned unexpected data for tournament={tournament}: {e}")

        if not any_success:
            return None, None
        return games, (min(next_dates) if next_dates else None)

    def _fetch_next_gameday(self):
        """Returns (date_str, games_dict) for the closest date (today or
        later) that has games *not all already finished*, or (None, None)
        if that can't be determined (API unreachable, or genuinely
        nothing scheduled)."""
        now = datetime.datetime.now(self.HELSINKI_TZ)
        today_str = now.strftime("%Y-%m-%d")

        games, next_date = self._fetch_games_and_next_date(today_str, self._current_season(now))
        if games is None:
            return None, None
        if games and not self._all_games_ended(games):
            return today_str, games

        # Today has no games, or only ones that have already finished
        # (e.g. checking !liiga next hours after today's games ended) -
        # look forward. Prefer the API's own "nextGameDate" hint when
        # available - it's only given when today's query had no games at
        # all, but can jump weeks ahead (e.g. across an off-season gap),
        # farther than the bounded day-by-day fallback below would reach.
        if next_date:
            try:
                next_dt = self.HELSINKI_TZ.localize(datetime.datetime.strptime(next_date, "%Y-%m-%d"))
                hinted_games, _ = self._fetch_games_and_next_date(next_date, self._current_season(next_dt))
                if hinted_games:
                    return next_date, hinted_games
            except ValueError:
                pass

        # No hint (today did have games, just all finished already), or
        # the hinted date turned out empty - fall back to a bounded
        # day-by-day search.
        for offset in range(1, self.NEXT_SEARCH_MAX_DAYS + 1):
            date = now + datetime.timedelta(days=offset)
            date_str = date.strftime("%Y-%m-%d")
            candidate_games, _ = self._fetch_games_and_next_date(date_str, self._current_season(date))
            if candidate_games:
                return date_str, candidate_games

        return None, None

    def _all_games_ended(self, games) -> bool:
        return bool(games) and all(bool(g.get("ended")) for g in games.values())

    def _snapshot(self, game) -> dict:
        return {
            "home_goals": len((game.get("homeTeam") or {}).get("goalEvents") or []),
            "away_goals": len((game.get("awayTeam") or {}).get("goalEvents") or []),
            "ended": bool(game.get("ended")),
        }
