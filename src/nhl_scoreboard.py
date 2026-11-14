import datetime
import time

import requests

from scoreboard import ScoreboardMixin


class NHLScoreboardMixin(ScoreboardMixin):
    """`!nhl now` and `!nhl results` - one-shot looks at the scoreboard, as
    opposed to the start/stop tracker. Both read api-web.nhle.com's
    /score/{date}, which (unlike the schedule the tracker polls) carries
    each game's current period and clock, and returns every game of a date
    in one request.

    Dates are US Eastern, same as the rest of NHLCommand: a late game that
    runs past Eastern midnight still belongs to the previous date, so
    `now` also looks at yesterday's date for games still being played.

    The sport-neutral part (dispatch, thread, board layout, packing) is
    ScoreboardMixin. Mixed into NHLCommand (it relies on the session,
    _score_pair, _team_abbrev, _is_ended and the date/summary helpers
    defined there and in LiveTrackerCommand).
    """

    LIVE_STATES = ("LIVE", "CRIT")
    UPCOMING_STATES = ("FUT", "PRE")
    # gameScheduleState values for a game that will not be played as scheduled.
    NOT_PLAYED_SCHEDULE_STATES = ("PPD", "CNCL", "TBD")
    # How many Eastern dates back `results` looks for a slate with a
    # finished game (covers an off day or two).
    RESULTS_LOOKBACK_DAYS = 4

    # ---- data ---------------------------------------------------------------

    def _fetch_scores(self, date_str, max_age=None):
        """{game_id: game} for one Eastern date, or None on failure.
        Briefly cached per date (SCORE_CACHE_SECONDS, or a shorter `max_age`
        for a caller that needs a fresher copy); failures are never cached."""
        cache = self.__dict__.setdefault("_score_cache", {})
        hit = cache.get(date_str)
        if hit and time.monotonic() - hit[0] < (self.SCORE_CACHE_SECONDS if max_age is None else max_age):
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

    def _is_upcoming(self, game) -> bool:
        return (game.get("gameState") in self.UPCOMING_STATES
                or game.get("gameScheduleState") in self.NOT_PLAYED_SCHEDULE_STATES)

    def _is_live(self, game) -> bool:
        """Neither not-yet-played nor finished: LIVE, CRIT, the short OVER
        state between the end of play and FINAL (seen lasting 20 s to 4 min,
        and once listed as 'upcoming' because only LIVE/CRIT counted), and
        any state not seen before - which must never look like 'not started'."""
        return not self._is_upcoming(game) and not self._is_ended(game)

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

        return self._board_from(games)

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
        still_on = {gid for gid, g in {**today, **games}.items() if self._is_live(g)}
        return self._results_from(label, finished, len(still_on))

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
        """'2nd 12:34 left', '1st int.', '1st end' (clock at 00:00 and stopped,
        before the intermission flag is set), 'OT 3:20 left', 'SO', 'over' (the
        state between the end of play and FINAL) or, for a state never seen
        before, the state itself in brackets. Empty if the feed has nothing usable."""
        state = game.get("gameState")
        if state == "OVER":
            return "over"
        if state not in self.LIVE_STATES:
            return f"[{state or '?'}]"
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
        if remaining == "00:00" and clock.get("running") is False:
            return f"{name} end".strip()
        return f"{name} {remaining} left".strip() if remaining else name
