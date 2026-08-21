import datetime
import threading

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

    All network I/O (the initial lookup and every later poll) happens on a
    background thread, never on the caller's thread, so a slow or hanging
    liiga.fi response can't stall the bot's main IRC loop.
    """

    # Tells CommandHandler to pass irc_bot/channel into execute().
    needs_irc_context = True

    BASE_URL = "https://www.liiga.fi/api/v2/games"
    TOURNAMENTS = ["runkosarja", "playoffs", "playout", "qualifications"]
    HELSINKI_TZ = pytz.timezone("Europe/Helsinki")
    POLL_INTERVAL_SECONDS = 30
    REQUEST_TIMEOUT_SECONDS = 10

    PERIOD_LABELS = {1: "1st", 2: "2nd", 3: "3rd", 4: "OT", 5: "SO"}

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
        return "Usage: !liiga start | !liiga stop"

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

        names = ", ".join(
            f"{self._team_name(g, 'homeTeam')}-{self._team_name(g, 'awayTeam')}"
            for g in games.values()
        )
        self._safe_send(irc_bot, channel, f"Tracking {len(games)} Liiga game(s) today: {names}")

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
                print(f"Liiga poll error: {e}")
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
                # this game and try again next cycle.
                print(f"Liiga: failed to process game {gid}: {e}")
                new_state[gid] = prev_state.get(gid) or self._snapshot(game)
                all_ended = False

        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None:
                entry["games"] = new_state

        return all_ended

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
            f"GOAL: {scoring_team} — {scorer_name}{tag_str}{assist_str} | "
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
            suffix = " (SO)"
        elif "OVERTIME" in finished:
            suffix = " (OT)"
        else:
            suffix = ""

        self._safe_send(irc_bot, channel, f"FINAL: {home} {home_goals}-{away_goals} {away}{suffix}")

    # ---- data fetching --------------------------------------------------

    def _current_season(self, now_helsinki) -> int:
        # Liiga seasons start in September and are labelled by the year
        # they run into (Sept 2024 -> season 2025).
        return now_helsinki.year + 1 if now_helsinki.month >= 7 else now_helsinki.year

    def _fetch_today_games(self):
        """Returns {game_id: game_dict} for today, merged across tournaments.

        Each tournament is fetched and error-handled independently, so a
        single failing/slow endpoint doesn't discard data already fetched
        from the others. Returns None only if every tournament request
        failed (i.e. we have no idea what's happening today), which the
        caller treats as "API unreachable".
        """
        now = datetime.datetime.now(self.HELSINKI_TZ)
        season = self._current_season(now)
        date_str = now.strftime("%Y-%m-%d")

        games = {}
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
                any_success = True
            except requests.exceptions.RequestException as e:
                print(f"Liiga API request failed for tournament={tournament}: {e}")
            except (ValueError, AttributeError, TypeError) as e:
                print(f"Liiga API returned unexpected data for tournament={tournament}: {e}")

        if not any_success:
            return None
        return games

    def _snapshot(self, game) -> dict:
        return {
            "home_goals": len((game.get("homeTeam") or {}).get("goalEvents") or []),
            "away_goals": len((game.get("awayTeam") or {}).get("goalEvents") or []),
            "ended": bool(game.get("ended")),
        }
