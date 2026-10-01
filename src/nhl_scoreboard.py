import datetime
import threading
import time

import requests


class NHLScoreboardMixin:
    """`!nhl now` and `!nhl results` - one-shot looks at the scoreboard, as
    opposed to the start/stop tracker. Both read api-web.nhle.com's
    /score/{date}, which (unlike the schedule the tracker polls) carries
    each game's current period and clock, and returns every game of a date
    in one request.

    Dates are US Eastern, same as the rest of NHLCommand: a late game that
    runs past Eastern midnight still belongs to the previous date, so
    `now` also looks at yesterday's date for games still being played.

    Mixed into NHLCommand (it relies on the session, _score_pair,
    _team_abbrev, _is_ended and the date/summary helpers defined there and
    in LiveTrackerCommand).
    """

    LIVE_STATES = ("LIVE", "CRIT")
    # /score/{date} is cheap, but anyone can spam the command - a short
    # cache keeps that from becoming a request per message.
    SCORE_CACHE_SECONDS = 15
    # How many Eastern dates back `results` looks for a slate with a
    # finished game (covers an off day or two).
    RESULTS_LOOKBACK_DAYS = 4
    # Well under IRC's 512-byte line limit, which the server's own framing
    # also eats into.
    MAX_LINE_BYTES = 380
    SECTION_SEPARATOR = " || "

    # ---- entry point --------------------------------------------------------

    def _scoreboard(self, irc_bot, channel, kind):
        """Starts the lookup on a background thread (a slow API can't stall
        the bot - same reasoning as `next`) and returns at once. The
        result is sent as one or more channel messages, not returned."""
        if irc_bot is None or channel is None:
            return "Error: this command needs channel context."
        threading.Thread(target=self._run_scoreboard, args=(irc_bot, channel, kind), daemon=True).start()
        return None

    def _run_scoreboard(self, irc_bot, channel, kind):
        try:
            messages = self._board_messages() if kind == "now" else self._results_messages()
        except Exception as e:
            print(f"NHL {kind} lookup error: {e}")
            messages = None
        if not messages:
            messages = [f"Error: could not reach the {self.DISPLAY_NAME} API."]
        for message in messages:
            self._safe_send(irc_bot, channel, message)

    # ---- data ---------------------------------------------------------------

    def _fetch_scores(self, date_str):
        """{game_id: game} for one Eastern date, or None on failure.
        Briefly cached per date; failures are never cached."""
        cache = self.__dict__.setdefault("_score_cache", {})
        hit = cache.get(date_str)
        if hit and time.monotonic() - hit[0] < self.SCORE_CACHE_SECONDS:
            return hit[1]
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/score/{date_str}",
                timeout=self.REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
            games = {
                g["id"]: g for g in (data.get("games") or [])
                if g.get("id") is not None and g.get("gameDate", date_str) == date_str
            }
        except (ValueError, AttributeError, TypeError) as e:
            print(f"NHL API returned unexpected score data for date={date_str}: {e}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"NHL API score request failed for date={date_str}: {e}")
            return None
        cache[date_str] = (time.monotonic(), games)
        return games

    def _eastern_dates_back(self, count):
        now = datetime.datetime.now(self.EASTERN_TZ)
        return [(now - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(count)]

    def _is_live(self, game) -> bool:
        return game.get("gameState") in self.LIVE_STATES

    def _by_start(self, games):
        return sorted(games, key=lambda g: g.get("startTimeUTC") or "")

    # ---- `!nhl now` ---------------------------------------------------------

    def _board_messages(self):
        """Today's games as Live / Final / Upcoming, or None if the API
        can't be reached."""
        today_str, yesterday_str = self._eastern_dates_back(2)
        today = self._fetch_scores(today_str)
        if today is None:
            return None
        games = dict(today)
        # A game from yesterday's slate still in progress (started late
        # evening Eastern) - see the class docstring.
        for gid, game in (self._fetch_scores(yesterday_str) or {}).items():
            if self._is_live(game):
                games[gid] = game

        if not games:
            return [self._no_games_message()]

        live = self._by_start(g for g in games.values() if self._is_live(g))
        ended = self._by_start(g for g in games.values() if self._is_ended(g))
        upcoming = self._by_start(
            g for g in games.values() if not self._is_live(g) and not self._is_ended(g)
        )

        sections = []
        if live:
            sections += self._chunk("Live: ", [self._live_label(g) for g in live], ", ")
        if ended:
            sections += self._chunk("Final: ", [self._final_label(g) for g in ended], ", ")
        if upcoming:
            summary = self._format_games_summary(upcoming)
            sections += self._chunk("Upcoming: ", summary.split(" | "), " | ")
        return self._pack(sections)

    def _no_games_message(self) -> str:
        try:
            date_str, games = self._fetch_next_gameday()
        except Exception as e:
            print(f"NHL next-gameday lookup error: {e}")
            date_str, games = None, None
        if not games:
            return "No NHL games today."
        label = self._format_date_label(date_str)
        return f"No NHL games today. Next NHL gameday ({label}): {self._format_games_summary(games.values())}"

    # ---- `!nhl results` -----------------------------------------------------

    def _results_messages(self):
        """Finished games of the most recent slate that has any, or None if
        the API can't be reached. Games still being played are not listed -
        only counted, with a pointer to `now`."""
        dates = self._eastern_dates_back(self.RESULTS_LOOKBACK_DAYS)
        today = self._fetch_scores(dates[0])
        if today is None:
            return None

        chosen = None
        for i, date in enumerate(dates):
            games = today if i == 0 else self._fetch_scores(date)
            if games is None:
                return None
            if any(self._is_ended(g) for g in games.values()):
                chosen = (date, games)
                break
        if chosen is None:
            return ["No recent NHL results found."]

        date, games = chosen
        finished = self._by_start(g for g in games.values() if self._is_ended(g))
        label = self._result_date_label(games, date)
        messages = self._pack(self._chunk(f"NHL results {label}: ", [self._final_label(g) for g in finished], ", "))

        still_on = {gid for gid, g in {**today, **games}.items() if self._is_live(g)}
        if still_on:
            messages[-1] += f" ({len(still_on)} game(s) still on: {self.COMMAND_NAME} now)"
        return messages

    def _result_date_label(self, games, eastern_date) -> str:
        """The slate's date as Helsinki shows it, 'D.M.' - an Eastern
        evening slate lands on the following Helsinki date, same mislabel
        trap _helsinki_date_label() already handles for `next`."""
        helsinki_date = self._helsinki_date_label(games, eastern_date)
        try:
            d = datetime.datetime.strptime(helsinki_date, "%Y-%m-%d")
        except ValueError:
            return helsinki_date
        return f"{d.day}.{d.month}."

    # ---- labels -------------------------------------------------------------

    def _teams_and_score(self, game):
        home, away = self._team_abbrev(game, "homeTeam"), self._team_abbrev(game, "awayTeam")
        score = self._score_pair(game)
        return home, away, score

    def _live_label(self, game) -> str:
        home, away, score = self._teams_and_score(game)
        status = self._live_status(game)
        text = f"{home}-{away}" if score is None else f"{home} {score[0]}-{score[1]} {away}"
        return f"{text} {status}".strip()

    def _final_label(self, game) -> str:
        home, away, score = self._teams_and_score(game)
        text = f"{home}-{away}" if score is None else f"{home} {score[0]}-{score[1]} {away}"
        last_period = (game.get("gameOutcome") or {}).get("lastPeriodType")
        return f"{text} ({last_period})" if last_period in ("OT", "SO") else text

    def _live_status(self, game) -> str:
        """'2nd 12:34 left', '1st int.', 'OT 3:20 left' or 'SO'. Empty if the
        feed has nothing usable."""
        period = game.get("periodDescriptor") or {}
        clock = game.get("clock") or {}
        number, period_type = period.get("number"), period.get("periodType")
        if period_type == "SO":
            return "SO"
        if period_type == "OT" and isinstance(number, int):
            overtimes = number - 3  # regular season has one (number 4); playoffs can have several
            name = "OT" if overtimes <= 1 else f"{overtimes}OT"
        else:
            name = {1: "1st", 2: "2nd", 3: "3rd"}.get(number, "")
        if clock.get("inIntermission"):
            return f"{name} int.".strip()
        remaining = clock.get("timeRemaining")
        return f"{name} {remaining} left".strip() if remaining else name

    # ---- message packing ----------------------------------------------------

    def _chunk(self, prefix, items, sep):
        """Splits `items` into strings that each start with `prefix` and
        stay within MAX_LINE_BYTES, breaking only between items."""
        chunks, current = [], []
        for item in items:
            candidate = prefix + sep.join(current + [item])
            if current and len(candidate.encode("utf-8")) > self.MAX_LINE_BYTES:
                chunks.append(prefix + sep.join(current))
                current = []
            current.append(item)
        if current:
            chunks.append(prefix + sep.join(current))
        return chunks

    def _pack(self, parts):
        """Joins as many parts as fit into each message (never splitting a
        part), so a short board is one line and a long one a few."""
        messages = []
        for part in parts:
            if messages and len((messages[-1] + self.SECTION_SEPARATOR + part).encode("utf-8")) <= self.MAX_LINE_BYTES:
                messages[-1] += self.SECTION_SEPARATOR + part
            else:
                messages.append(part)
        return messages
