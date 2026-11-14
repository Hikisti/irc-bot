"""A contract check of the unofficial feeds the live trackers read (NHL api-web.nhle.com, liiga.fi). Read-only.

The test suite mocks every request, so it cannot see a feed change shape; the bot's defensive code then degrades to
missing or wrong lines in a channel. This fetches the feeds the way the trackers do and checks that every field the
bot reads is still there with the same type. Values are never checked: a day without games passes.

It looks back at yesterday's games (US Eastern for the NHL, which buckets its dates that way; Helsinki for Liiga),
because a finished game carries every field the trackers read (scores, the outcome, goal events, the roster).

Exit status: 0 all fields as expected; 1 a field missing or of another type, or an endpoint gone (HTTP 404/410);
2 a feed could not be reached (timeout, connection error, other HTTP error): an outage, not a change of shape.

Usage:
    python tools/feed_contract.py
Run by .github/workflows/feed-contract.yml on a schedule. Pesis is not covered yet (no matches out of season).
"""
import datetime
import os
import sys

import requests

import probe_common  # noqa: F401  (puts src/ on the path)
from liiga_command import LiigaCommand  # noqa: E402
from nhl_command import NHLCommand  # noqa: E402

FINISHED_NHL_STATES = ("FINAL", "OFF")
GONE_STATUSES = (404, 410)
LIIGA_LOOKBACK_DAYS = 14  # Liiga plays most days in season; two weeks cover an international break

# Each spec is a list of (path, types). A path is dot-separated keys; "key[]" walks every item of a list;
# a trailing "?" makes the field optional (only its type is checked when it is there).
NHL_SCHEDULE = [
    ("gameWeek[]", dict),
    ("gameWeek[].date", str),
    ("gameWeek[].games[]", dict),
    ("gameWeek[].games[].id", int),
    ("gameWeek[].games[].gameType", int),
    ("gameWeek[].games[].gameState", str),
    ("gameWeek[].games[].gameScheduleState", str),
    ("gameWeek[].games[].startTimeUTC", str),
    *((f"gameWeek[].games[].{side}.{field}", kind) for side in ("homeTeam", "awayTeam")
      for field, kind in (("id", int), ("abbrev", str), ("placeName.default", str), ("commonName.default", str))),
    ("nextStartDate?", str),
]
NHL_SCORE_GAME = [
    ("id", int),
    ("gameDate", str),
    ("gameState", str),
    ("gameScheduleState", str),
    ("startTimeUTC", str),
    ("homeTeam.abbrev", str),
    ("awayTeam.abbrev", str),
]
NHL_SCORE_FINISHED = [
    ("homeTeam.score", int),
    ("awayTeam.score", int),
    ("gameOutcome.lastPeriodType", str),
    ("periodDescriptor.number", int),
    ("periodDescriptor.periodType", str),
]
NHL_PLAY_BY_PLAY = [
    ("id", int),
    ("season", int),
    ("gameState", str),
    ("startTimeUTC", str),
    *((f"{side}.{field}", kind) for side in ("homeTeam", "awayTeam")
      for field, kind in (("id", int), ("abbrev", str), ("score", int), ("commonName.default", str))),
    ("gameOutcome.lastPeriodType", str),
    ("plays[]", dict),
    ("plays[].eventId", int),
    ("plays[].typeDescKey", str),
    ("plays[].timeInPeriod", str),
    ("plays[].periodDescriptor.number", int),
    ("plays[].periodDescriptor.periodType", str),
    ("rosterSpots[]", dict),
    ("rosterSpots[].playerId", int),
    ("rosterSpots[].teamId", int),
    ("rosterSpots[].firstName.default", str),
    ("rosterSpots[].lastName.default", str),
]
NHL_GOAL = [
    ("details.scoringPlayerId", int),
    ("details.eventOwnerTeamId", int),
    ("details.homeScore", int),
    ("details.awayScore", int),
    ("situationCode", str),
    ("details.assist1PlayerId?", int),
]
NHL_STOPPAGE = [
    ("details.reason", str),
    ("details.secondaryReason?", str),
]
LIIGA_GAME = [
    ("id", int),
    ("start", str),
    ("started", bool),
    ("ended", bool),
    ("finishedType", str),
    ("end?", str),
    *((f"{side}.{field}", kind) for side in ("homeTeam", "awayTeam")
      for field, kind in (("teamId", str), ("teamName", str), ("goals", int), ("goalEvents[]", dict))),
]
LIIGA_ENDED = [
    ("gameTime", int),
    ("currentPeriod", int),
    ("spectators", int),
    ("periods[]", dict),
    ("periods[].index", int),
    ("periods[].category?", str),
    ("periods[].startTime", int),
    ("periods[].endTime", int),
]
# Only events with a scorer: the feed also lists a goal taken off by video review, without one, and the bot skips it.
LIIGA_GOAL_EVENT = [
    ("eventId", int),
    ("period", int),
    ("gameTime", int),
    ("scorerPlayerId", int),
    ("scorerPlayer.firstName", str),
    ("scorerPlayer.lastName", str),
    ("goalTypes[]", str),
    ("homeTeamScore", int),
    ("awayTeamScore", int),
    ("assistantPlayers[].lastName", str),
]


class FeedUnreachable(Exception):
    """The feed did not answer (an outage), as opposed to answering in another shape."""


class EndpointGone(Exception):
    """The endpoint answered 404/410: it has moved or been removed."""


# ---- the checks (pure) --------------------------------------------------------------------------------------------

def _is_type(value, kind) -> bool:
    if isinstance(value, bool) and kind is not bool:
        return False  # bool is an int to Python; a flag where a number was is a change
    return isinstance(value, kind)


def _walk(value, keys):
    """Yields every value at `keys` below `value`, or a _Missing marker with the key where the path broke off."""
    if not keys:
        yield value
        return
    key, rest = keys[0], keys[1:]
    each = key.endswith("[]")
    name = key[:-2] if each else key
    if not isinstance(value, dict) or value.get(name) is None:
        yield _Missing(name)
        return
    child = value[name]
    if not each:
        yield from _walk(child, rest)
    elif not isinstance(child, list):
        yield _WrongType(name, child)
    else:
        for item in child:
            yield from _walk(item, rest)


class _Missing:
    def __init__(self, key):
        self.key = key


class _WrongType:
    def __init__(self, key, value):
        self.key, self.value = key, value


def _tally(payload, spec, totals=None):
    """{path: [missing, of another type, found, a wrong value's type name]} for every field of `spec`, added to
    `totals` when given (to sum over several items)."""
    totals = {} if totals is None else totals
    for raw_path, kind in spec:
        optional = raw_path.endswith("?")
        path = raw_path.rstrip("?")
        entry = totals.setdefault(path, [0, 0, 0, None, optional, kind])
        for value in _walk(payload, path.split(".")):
            entry[2] += 1
            if isinstance(value, _Missing):
                entry[0] += 1
            elif isinstance(value, _WrongType) or not _is_type(value, kind):
                entry[1] += 1
                entry[3] = entry[3] or type(value.value if isinstance(value, _WrongType) else value).__name__
    return totals


def _format(totals, where):
    problems = []
    for path, (missing, wrong, found, wrong_type, optional, kind) in totals.items():
        if missing and not optional:
            problems.append(f"{where}: {path}: missing in {missing} of {found}")
        if wrong:
            problems.append(f"{where}: {path}: expected {kind.__name__}, got {wrong_type} in {wrong} of {found}")
    return problems


def check(payload, spec, where):
    """Problems ("<where>: <path>: missing in 2 of 14") for every field of `spec` that `payload` lacks or holds
    with another type, one line per path."""
    return _format(_tally(payload, spec), where)


def check_each(items, spec, where):
    """`check` over several items, with the counts summed per path."""
    totals = {}
    for item in items:
        _tally(item, spec, totals)
    return _format(totals, where)


def check_nhl(schedule, score_games, pbp):
    """Problems in the NHL feeds: the schedule window, one day's score games, and one finished game's plays (or
    None when no finished game was found, which is not a problem)."""
    problems = check(schedule, NHL_SCHEDULE, "nhl schedule")
    if not isinstance(score_games, list):
        return problems + ["nhl score: games: expected list"]
    problems += check_each(score_games, NHL_SCORE_GAME, "nhl score")
    finished = [g for g in score_games if isinstance(g, dict) and g.get("gameState") in FINISHED_NHL_STATES]
    problems += check_each(finished, NHL_SCORE_FINISHED, "nhl score (finished)")
    if pbp is not None:
        problems += check(pbp, NHL_PLAY_BY_PLAY, "nhl play-by-play")
        plays = [p for p in pbp.get("plays") or [] if isinstance(p, dict)]
        goals = [p for p in plays if p.get("typeDescKey") == "goal"]
        stoppages = [p for p in plays if p.get("typeDescKey") == "stoppage"]
        problems += check_each(goals, NHL_GOAL, "nhl play-by-play goal")
        problems += check_each(stoppages, NHL_STOPPAGE, "nhl play-by-play stoppage")
        if not goals and pbp.get("gameState") in FINISHED_NHL_STATES and _final_score_total(pbp):
            problems.append("nhl play-by-play: a finished game with goals on the scoreboard lists no goal play")
    return problems


def _final_score_total(pbp):
    scores = [(pbp.get(side) or {}).get("score") for side in ("homeTeam", "awayTeam")]
    return sum(s for s in scores if _is_type(s, int))


def check_liiga(games):
    """Problems in one day's Liiga games (every tournament merged)."""
    if not isinstance(games, list):
        return ["liiga games: expected list"]
    problems = check_each(games, LIIGA_GAME, "liiga game")
    ended = [g for g in games if isinstance(g, dict) and g.get("ended") is True]
    problems += check_each(ended, LIIGA_ENDED, "liiga game (ended)")
    scored = [e for g in ended for side in ("homeTeam", "awayTeam")
              for e in ((g.get(side) or {}).get("goalEvents") or []) if isinstance(e, dict) and e.get("scorerPlayer")]
    problems += check_each(scored, LIIGA_GOAL_EVENT, "liiga goal event")
    goals_on_board = sum(g for game in ended for side in ("homeTeam", "awayTeam")
                         for g in [(game.get(side) or {}).get("goals")] if _is_type(g, int))
    if goals_on_board and not scored:
        problems.append("liiga goal event: ended games with goals on the scoreboard list no goal event with a scorer")
    return problems


# ---- fetching -----------------------------------------------------------------------------------------------------

def get_json(session, url, params=None, timeout=15):
    """The parsed JSON at `url`. Raises EndpointGone for 404/410, FeedUnreachable for any other failure to get an
    answer; an answer that is not JSON is a change of shape and comes back as None."""
    try:
        resp = session.get(url, params=params, timeout=timeout, allow_redirects=True)
    except requests.exceptions.RequestException as e:
        raise FeedUnreachable(f"{url}: {type(e).__name__}") from e
    if resp.status_code in GONE_STATUSES:
        raise EndpointGone(f"{url}: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise FeedUnreachable(f"{url}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        return None


def fetch_nhl(nhl):
    """(schedule, score games, a finished game's play-by-play or None) for yesterday's US-Eastern slate."""
    yesterday = (datetime.datetime.now(nhl.EASTERN_TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    schedule = get_json(nhl.session, f"{nhl.BASE_URL}/schedule/{yesterday}")
    score = get_json(nhl.session, f"{nhl.BASE_URL}/score/{yesterday}")
    games = score.get("games") if isinstance(score, dict) else None
    finished = [g for g in games or [] if isinstance(g, dict) and g.get("gameState") in FINISHED_NHL_STATES
                and _is_type(g.get("id"), int)]
    pbp = None
    if finished:
        pbp = get_json(nhl.session, f"{nhl.BASE_URL}/gamecenter/{finished[0]['id']}/play-by-play")
    return schedule, games, pbp, yesterday


def fetch_liiga(liiga):
    """(the games of the latest Helsinki date in the last week that has an ended game, that date), or ([], None)."""
    today = datetime.datetime.now(liiga.HELSINKI_TZ)
    season = liiga._current_season(today)
    for back in range(1, LIIGA_LOOKBACK_DAYS + 1):
        date = (today - datetime.timedelta(days=back)).strftime("%Y-%m-%d")
        games = []
        for tournament in liiga.TOURNAMENTS:
            data = get_json(liiga.session, liiga.BASE_URL,
                            params={"tournament": tournament, "season": season, "date": date})
            if not isinstance(data, dict) or not isinstance(data.get("games"), list):
                return data, date  # not the expected wrapper: let the check report it
            games += data["games"]
        if any(isinstance(g, dict) and g.get("ended") is True for g in games):
            return games, date
    return [], None


# ---- running ------------------------------------------------------------------------------------------------------

def run(nhl=None, liiga=None, out=print):
    """Checks both feeds and returns the exit status (see the module docstring)."""
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    problems, outages = [], []
    for name, step in (("NHL", lambda: _run_nhl(nhl or NHLCommand(), out)),
                       ("Liiga", lambda: _run_liiga(liiga or LiigaCommand(), out))):
        try:
            problems += step()
        except EndpointGone as e:
            problems.append(f"{name.lower()}: endpoint gone: {e}")
        except FeedUnreachable as e:
            outages.append(f"{name.lower()}: {e}")
    for problem in problems:
        out(f"::error title=Feed shape changed::{problem}" if in_actions else f"SHAPE  {problem}")
    for outage in outages:
        out(f"::warning title=Feed unreachable::{outage}" if in_actions else f"DOWN   {outage}")
    if problems:
        return 1
    if outages:
        return 2
    out("All checked fields are present with the expected types.")
    return 0


def _run_nhl(nhl, out):
    schedule, games, pbp, date = fetch_nhl(nhl)
    finished = sum(1 for g in games or [] if isinstance(g, dict) and g.get("gameState") in FINISHED_NHL_STATES)
    out(f"NHL {date}: {len(games or [])} game(s), {finished} finished, play-by-play "
        f"{'checked' if pbp is not None else 'not checked (no finished game)'}")
    return check_nhl(schedule, games, pbp)


def _run_liiga(liiga, out):
    games, date = fetch_liiga(liiga)
    if date is None:
        out(f"Liiga: no ended game in the last {LIIGA_LOOKBACK_DAYS} days, nothing to check")
        return []
    out(f"Liiga {date}: {len(games) if isinstance(games, list) else '?'} game(s)")
    return check_liiga(games)


if __name__ == "__main__":
    sys.exit(run())
