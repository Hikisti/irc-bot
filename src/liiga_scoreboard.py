import datetime
import time

from scoreboard import ScoreboardMixin


def snapshot_rank(game) -> tuple:
    """How far along a copy of a game is: ended, then goals on the board,
    then the clock. The feed can answer with an older copy of a game
    (score back to 0-0, clock back a few seconds; see #16), and the higher
    rank is the newer one."""
    goals = 0
    for side in ("homeTeam", "awayTeam"):
        value = (game.get(side) or {}).get("goals")
        goals += value if isinstance(value, int) else 0
    clock = game.get("gameTime")
    return bool(game.get("ended")), goals, clock if isinstance(clock, int) else 0


def period_status(game, period_labels) -> str:
    """The live game's place in it: "2nd 12'" (minutes played in the
    period: the feed's clock moves in coarse, noisy steps, so no seconds),
    "1st int." / "2nd int." (the clock stops at exactly the period's end and
    the period number stays until the next one starts), "3rd end" / "OT end"
    (the same after the last period, until the feed sets `ended`, which can
    take up to 18 minutes), "SO" during a shootout. Empty when the feed has
    nothing usable (a game just started, before the first period is set)."""
    number, clock = game.get("currentPeriod"), game.get("gameTime")
    if not isinstance(number, int) or number < 1 or not isinstance(clock, int):
        return ""
    periods = [p for p in (game.get("periods") or []) if isinstance(p, dict)]
    period = next((p for p in periods if p.get("index") == number), {})
    category = period.get("category")
    if category == "WINNING_SHOT_COMPETITION":
        return "SO"
    if category == "OVERTIME":
        overtimes = sum(1 for p in periods if p.get("category") == "OVERTIME"
                        and isinstance(p.get("index"), int) and p["index"] <= number)
        name = "OT" if overtimes <= 1 else f"{overtimes}OT"
    else:
        name = period_labels.get(number, "")
    if name == "SO":
        return "SO"
    start, end = period.get("startTime"), period.get("endTime")
    if isinstance(end, int) and clock >= end:
        return f"{name} int." if category == "NORMAL" and number < 3 else f"{name} end"
    if not isinstance(start, int):
        return name
    return f"{name} {max(0, clock - start) // 60}'"


class LiigaScoreboardMixin(ScoreboardMixin):
    """`!liiga now` and `!liiga results`, the Liiga side of ScoreboardMixin.
    Both read the same liiga.fi games list the tracker polls (one request
    per tournament per date); it already carries each game's score, period
    and clock, so no per-game call is needed. Dates are Helsinki dates:
    Liiga games end before midnight.

    Mixed into LiigaCommand (it relies on _fetch_games_with_hints,
    _current_season, _team_name, _score_pair, _end_suffix, _not_played,
    _now and the summary helpers defined there and in LiveTrackerCommand).
    """

    # How long a copy of a game seen by `now`/`results` is preferred over an
    # older copy the feed answers with afterwards (the feed's backward
    # jumps last seconds, see #16); after that the feed is trusted again,
    # so a goal taken off the board does not stay on it.
    NEWEST_SNAPSHOT_SECONDS = 60

    # ---- data ---------------------------------------------------------------

    def _fetch_day(self, date_str):
        """(games, previous gameday) for one Helsinki date, or None when no
        tournament answered. Briefly cached per date; failures are never
        cached."""
        cache = self.__dict__.setdefault("_day_cache", {})
        hit = cache.get(date_str)
        if hit and time.monotonic() - hit[0] < self.SCORE_CACHE_SECONDS:
            return hit[1]
        try:
            day = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=self.HELSINKI_TZ)
        except (ValueError, TypeError):
            return None
        games, _next, previous = self._fetch_games_with_hints(date_str, self._current_season(day))
        if games is None:
            return None
        result = ({gid: self._newest_copy(gid, game) for gid, game in games.items()}, previous)
        cache[date_str] = (time.monotonic(), result)
        return result

    def _newest_copy(self, gid, game):
        seen = self.__dict__.setdefault("_newest_seen", {})
        now = time.monotonic()
        for old in [k for k, (at, _) in seen.items() if now - at >= self.NEWEST_SNAPSHOT_SECONDS]:
            del seen[old]
        kept = seen.get(gid)
        if kept and snapshot_rank(kept[1]) > snapshot_rank(game):
            return kept[1]
        seen[gid] = (now, game)
        return game

    def _today_str(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _is_ended(self, game) -> bool:
        return bool(game.get("ended"))

    def _is_upcoming(self, game) -> bool:
        """Not started, including a game that never will be (see _not_played,
        labelled as such)."""
        return not game.get("started") and not self._is_ended(game)

    def _is_live(self, game) -> bool:
        return not self._is_upcoming(game) and not self._is_ended(game)

    # ---- `!liiga now` -------------------------------------------------------

    def _board_messages(self):
        """Today's games as Live / Final / Upcoming, or None if the API
        can't be reached."""
        today = self._fetch_day(self._today_str())
        if today is None:
            return None
        return self._board_from(today[0])

    def _upcoming_summary(self, games) -> str:
        def label(game):
            text = f"{self._team_name(game, 'homeTeam')}-{self._team_name(game, 'awayTeam')}"
            return f"{text} (not played)" if self._not_played(game) else text

        return self._format_start_time_summary(games, self.START_TIME_KEY, label)

    # ---- `!liiga results` ---------------------------------------------------

    def _results_messages(self):
        """Finished games of today, or else of the previous gameday, or None
        if the API can't be reached. Games still being played are not listed
        - only counted, with a pointer to `now`."""
        today_str = self._today_str()
        today = self._fetch_day(today_str)
        if today is None:
            return None
        today_games, previous = today
        date, games = today_str, today_games
        if not any(self._is_ended(g) for g in games.values()) and previous:
            earlier = self._fetch_day(previous)
            if earlier is None:
                return None
            date, games = previous, earlier[0]
        finished = self._by_start(g for g in games.values() if self._is_ended(g))
        if not finished:
            return ["No recent Liiga results found."]
        still_on = {gid for gid, g in {**today_games, **games}.items() if self._is_live(g)}
        return self._results_from(self._day_label(date), finished, len(still_on))

    def _day_label(self, date_str) -> str:
        try:
            d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return date_str
        return f"{d.day}.{d.month}."

    # ---- labels -------------------------------------------------------------

    def _score_text(self, game) -> str:
        home, away = self._team_name(game, "homeTeam"), self._team_name(game, "awayTeam")
        score = self._score_pair(game)
        return f"{home}-{away}" if score is None else f"{home} {score[0]}-{score[1]} {away}"

    def _live_label(self, game) -> str:
        return f"{self._score_text(game)} {period_status(game, self.PERIOD_LABELS)}".strip()

    def _final_label(self, game) -> str:
        return self._score_text(game) + self._end_suffix(game)[0]
