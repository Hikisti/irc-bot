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
    """

    # Tells CommandHandler to pass irc_bot/channel into execute().
    needs_irc_context = True

    BASE_URL = "https://www.liiga.fi/api/v2/games"
    TOURNAMENTS = ["runkosarja", "playoffs", "playout", "qualifications"]
    HELSINKI_TZ = pytz.timezone("Europe/Helsinki")
    POLL_INTERVAL_SECONDS = 30

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
            existing = self._channels.get(channel)
            if existing:
                return "Already tracking live Liiga games in this channel."

            games = self._fetch_today_games()
            if games is None:
                return "Error: could not reach the Liiga API."
            if not games:
                return "No Liiga games scheduled today."

            state = {gid: self._snapshot(g) for gid, g in games.items()}
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._poll_loop,
                args=(irc_bot, channel, stop_event),
                daemon=True,
            )
            self._channels[channel] = {
                "stop_event": stop_event,
                "thread": thread,
                "games": state,
            }
            thread.start()

        names = ", ".join(
            f"{g['homeTeam']['teamName']}-{g['awayTeam']['teamName']}"
            for g in games.values()
        )
        return f"Tracking {len(games)} Liiga game(s) today: {names}"

    def _stop(self, channel):
        with self._lock:
            entry = self._channels.pop(channel, None)
        if not entry:
            return "Not currently tracking Liiga games in this channel."
        entry["stop_event"].set()
        return "Stopped live Liiga tracking."

    # ---- polling loop -------------------------------------------------

    def _poll_loop(self, irc_bot, channel, stop_event):
        while not stop_event.is_set():
            try:
                all_ended = self._poll_once(irc_bot, channel)
            except Exception as e:
                print(f"Liiga poll error: {e}")
                all_ended = False

            if all_ended:
                with self._lock:
                    entry = self._channels.get(channel)
                    if entry is not None and entry["stop_event"] is stop_event:
                        del self._channels[channel]
                irc_bot.send_message(
                    channel, "All of today's Liiga games have finished. Live tracking stopped."
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

        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None:
                entry["games"] = new_state

        return all_ended

    # ---- announcements --------------------------------------------------

    def _announce_new_goals(self, irc_bot, channel, game, prev, side):
        events = (game.get(side) or {}).get("goalEvents") or []
        key = "home_goals" if side == "homeTeam" else "away_goals"
        for event in events[prev[key]:]:
            irc_bot.send_message(channel, self._format_goal(game, side, event))

    def _format_goal(self, game, side, event) -> str:
        home = game["homeTeam"]["teamName"]
        away = game["awayTeam"]["teamName"]
        scoring_team = home if side == "homeTeam" else away
        home_score = event.get("homeTeamScore")
        away_score = event.get("awayTeamScore")

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
        home = game["homeTeam"]["teamName"]
        away = game["awayTeam"]["teamName"]
        home_goals = game["homeTeam"]["goals"]
        away_goals = game["awayTeam"]["goals"]

        finished = game.get("finishedType") or ""
        if "SHOOTOUT" in finished or "WINNING_SHOT" in finished:
            suffix = " (SO)"
        elif "OVERTIME" in finished:
            suffix = " (OT)"
        else:
            suffix = ""

        irc_bot.send_message(channel, f"FINAL: {home} {home_goals}-{away_goals} {away}{suffix}")

    # ---- data fetching --------------------------------------------------

    def _current_season(self, now_helsinki) -> int:
        # Liiga seasons start in September and are labelled by the year
        # they run into (Sept 2024 -> season 2025).
        return now_helsinki.year + 1 if now_helsinki.month >= 7 else now_helsinki.year

    def _fetch_today_games(self):
        """Returns {game_id: game_dict} for today, or None on a request failure."""
        now = datetime.datetime.now(self.HELSINKI_TZ)
        season = self._current_season(now)
        date_str = now.strftime("%Y-%m-%d")

        games = {}
        try:
            for tournament in self.TOURNAMENTS:
                resp = self.session.get(
                    self.BASE_URL,
                    params={"tournament": tournament, "season": season, "date": date_str},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                for g in (data.get("games") or []):
                    gid = g.get("id")
                    if gid is not None:
                        games[gid] = g
        except requests.exceptions.RequestException:
            return None
        except (ValueError, AttributeError, TypeError):
            return None
        return games

    def _snapshot(self, game) -> dict:
        return {
            "home_goals": len((game.get("homeTeam") or {}).get("goalEvents") or []),
            "away_goals": len((game.get("awayTeam") or {}).get("goalEvents") or []),
            "ended": bool(game.get("ended")),
        }
