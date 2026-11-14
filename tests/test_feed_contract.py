"""The pure parts of tools/feed_contract.py: the field checks against payloads trimmed from the live feeds
(2026-11-10/11), the outage / gone / changed classification, and the exit status. No network."""
import copy
import os
import sys
from unittest.mock import MagicMock

import pytest
import requests

TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import feed_contract as fc  # noqa: E402


def team(team_id, abbrev, score=None):
    t = {"id": team_id, "abbrev": abbrev, "placeName": {"default": "Place"}, "commonName": {"default": "Name"}}
    if score is not None:
        t["score"] = score
    return t


SCHEDULE = {
    "nextStartDate": "2026-11-17",
    "gameWeek": [{"date": "2026-11-10", "games": [
        {"id": 2026020250, "gameType": 2, "gameState": "OFF", "gameScheduleState": "OK",
         "startTimeUTC": "2026-11-11T00:00:00Z", "homeTeam": team(10, "TOR", 3), "awayTeam": team(21, "COL", 2)},
    ]}, {"date": "2026-11-11", "games": []}],
}
SCORE_GAMES = [
    {"id": 2026020250, "gameDate": "2026-11-10", "gameState": "OFF", "gameScheduleState": "OK",
     "startTimeUTC": "2026-11-11T00:00:00Z", "homeTeam": {"abbrev": "TOR", "score": 3},
     "awayTeam": {"abbrev": "COL", "score": 2}, "gameOutcome": {"lastPeriodType": "REG"},
     "periodDescriptor": {"number": 3, "periodType": "REG"}},
    {"id": 2026020251, "gameDate": "2026-11-10", "gameState": "FUT", "gameScheduleState": "PPD",
     "startTimeUTC": "2026-11-11T02:00:00Z", "homeTeam": {"abbrev": "SEA"}, "awayTeam": {"abbrev": "VAN"}},
]
PERIOD = {"number": 1, "periodType": "REG"}
PBP = {
    "id": 2026020250, "season": 20262027, "gameState": "OFF", "startTimeUTC": "2026-11-11T00:00:00Z",
    "homeTeam": team(10, "TOR", 1), "awayTeam": team(21, "COL", 0), "gameOutcome": {"lastPeriodType": "REG"},
    "plays": [
        {"eventId": 6, "typeDescKey": "stoppage", "timeInPeriod": "00:06", "periodDescriptor": PERIOD,
         "details": {"reason": "icing"}},
        {"eventId": 110, "typeDescKey": "goal", "timeInPeriod": "03:33", "periodDescriptor": PERIOD,
         "situationCode": "1551", "details": {"scoringPlayerId": 8478178, "eventOwnerTeamId": 10,
                                              "homeScore": 1, "awayScore": 0, "assist1PlayerId": 8470613}},
    ],
    "rosterSpots": [{"teamId": 10, "playerId": 8478178, "firstName": {"default": "A"}, "lastName": {"default": "B"}}],
}
LIIGA_EVENT = {"eventId": 102, "period": 3, "gameTime": 2535, "scorerPlayerId": 40373820,
               "scorerPlayer": {"playerId": 40373820, "firstName": "A", "lastName": "B"}, "goalTypes": ["YV"],
               "homeTeamScore": 1, "awayTeamScore": 0, "assistantPlayers": [{"playerId": 1, "lastName": "C"}]}
LIIGA_GAMES = [
    {"id": 1, "start": "2026-11-09T16:30:00Z", "started": True, "ended": True, "finishedType": "ENDED_DURING_REGULAR_GAME_TIME",
     "gameTime": 3600, "currentPeriod": 3, "spectators": 4200,
     "periods": [{"index": 1, "category": "REGULAR", "startTime": 0, "endTime": 1200}],
     "homeTeam": {"teamId": "123:hifk", "teamName": "HIFK", "goals": 1, "goalEvents": [LIIGA_EVENT]},
     "awayTeam": {"teamId": "456:tps", "teamName": "TPS", "goals": 0, "goalEvents": []}},
    {"id": 2, "start": "2026-11-09T16:30:00Z", "started": False, "ended": False, "finishedType": "ACTIVE_OR_NOT_STARTED",
     "homeTeam": {"teamId": "7:a", "teamName": "A", "goals": 0, "goalEvents": []},
     "awayTeam": {"teamId": "8:b", "teamName": "B", "goals": 0, "goalEvents": []}},
]


class TestCheck:
    def test_paths_through_lists_and_dicts(self):
        assert fc.check({"a": [{"b": {"c": 1}}, {"b": {"c": 2}}]}, [("a[].b.c", int)], "x") == []

    def test_a_missing_field_is_counted_per_path(self):
        problems = fc.check({"a": [{"b": 1}, {}, {"b": None}]}, [("a[].b", int)], "x")
        assert problems == ["x: a[].b: missing in 2 of 3"]

    def test_a_changed_type_is_named(self):
        assert fc.check({"a": "1"}, [("a", int)], "x") == ["x: a: expected int, got str in 1 of 1"]

    def test_a_flag_is_not_a_number(self):
        assert fc.check({"a": True}, [("a", int)], "x") == ["x: a: expected int, got bool in 1 of 1"]
        assert fc.check({"a": True}, [("a", bool)], "x") == []

    def test_a_list_that_became_something_else(self):
        assert fc.check({"a": {"b": 1}}, [("a[].b", int)], "x") == ["x: a[].b: expected int, got dict in 1 of 1"]

    def test_an_optional_field_may_be_absent_but_not_of_another_type(self):
        assert fc.check({}, [("a?", str)], "x") == []
        assert fc.check({"a": 3}, [("a?", str)], "x") == ["x: a: expected str, got int in 1 of 1"]

    def test_an_empty_list_is_fine(self):
        assert fc.check({"a": []}, [("a[].b", int)], "x") == []

    def test_not_json_at_all(self):
        assert fc.check(None, [("a", int)], "x") == ["x: a: missing in 1 of 1"]

    def test_counts_are_summed_over_items(self):
        assert fc.check_each([{"a": 1}, {}, {}], [("a", int)], "x") == ["x: a: missing in 2 of 3"]


class TestNHL:
    def test_the_live_shapes_pass(self):
        assert fc.check_nhl(SCHEDULE, SCORE_GAMES, PBP) == []

    def test_a_day_without_a_finished_game_passes(self):
        assert fc.check_nhl({"gameWeek": []}, [], None) == []

    def test_an_unplayed_game_needs_no_score(self):
        assert fc.check_nhl(SCHEDULE, [SCORE_GAMES[1]], None) == []

    @pytest.mark.parametrize("where, path, value, expected", [
        ("schedule", ("gameWeek", 0, "games", 0, "startTimeUTC"), None, "nhl schedule: gameWeek[].games[].startTimeUTC: missing"),
        ("schedule", ("gameWeek", 0, "games", 0, "homeTeam", "abbrev"), 10, "homeTeam.abbrev: expected str, got int"),
        ("score", (0, "gameOutcome"), None, "nhl score (finished): gameOutcome.lastPeriodType: missing in 1 of 1"),
        ("score", (0, "homeTeam", "score"), "3", "nhl score (finished): homeTeam.score: expected int, got str"),
        ("pbp", ("plays", 1, "details", "homeScore"), None, "nhl play-by-play goal: details.homeScore: missing in 1 of 1"),
        ("pbp", ("plays", 1, "details", "scoringPlayerId"), "8478178", "details.scoringPlayerId: expected int"),
        ("pbp", ("plays", 0, "details", "reason"), None, "nhl play-by-play stoppage: details.reason: missing"),
        ("pbp", ("rosterSpots", 0, "firstName"), "A", "rosterSpots[].firstName.default: missing"),
        ("pbp", ("plays", 1, "typeDescKey"), "Goal", "a finished game with goals on the scoreboard lists no goal play"),
    ])
    def test_a_broken_field_is_reported(self, where, path, value, expected):
        payloads = {"schedule": copy.deepcopy(SCHEDULE), "score": copy.deepcopy(SCORE_GAMES), "pbp": copy.deepcopy(PBP)}
        target = payloads[where]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        problems = fc.check_nhl(payloads["schedule"], payloads["score"], payloads["pbp"])
        assert any(expected in p for p in problems), problems

    def test_score_games_that_are_not_a_list(self):
        assert fc.check_nhl(SCHEDULE, None, None) == ["nhl score: games: expected list"]


class TestLiiga:
    def test_the_live_shapes_pass(self):
        assert fc.check_liiga(LIIGA_GAMES) == []

    def test_a_goal_taken_off_by_video_review_has_no_scorer_and_is_not_checked(self):
        games = copy.deepcopy(LIIGA_GAMES)
        games[0]["homeTeam"]["goalEvents"].append({"eventId": 103, "period": 3, "gameTime": 2600, "goalTypes": ["VT0"]})
        assert fc.check_liiga(games) == []

    @pytest.mark.parametrize("change, expected", [
        (lambda g: g[0].pop("ended"), "liiga game: ended: missing in 1 of 2"),
        (lambda g: g[1].update(started="false"), "liiga game: started: expected bool, got str"),
        (lambda g: g[0]["homeTeam"].update(teamId=123), "homeTeam.teamId: expected str, got int"),
        (lambda g: g[0].pop("spectators"), "liiga game (ended): spectators: missing in 1 of 1"),
        (lambda g: g[0]["homeTeam"]["goalEvents"][0].pop("homeTeamScore"), "liiga goal event: homeTeamScore: missing"),
        (lambda g: g[0]["homeTeam"]["goalEvents"][0].update(gameTime="42:15"), "liiga goal event: gameTime: expected int"),
        (lambda g: g[0]["homeTeam"]["goalEvents"][0].update(goalTypes="YV"), "liiga goal event: goalTypes[]: expected str, got str"),
        (lambda g: g[0]["homeTeam"]["goalEvents"][0]["scorerPlayer"].update(scorer="A B", firstName=None),
         "liiga goal event: scorerPlayer.firstName: missing"),
        (lambda g: g[0]["homeTeam"]["goalEvents"][0].pop("scorerPlayer"),
         "ended games with goals on the scoreboard list no goal event with a scorer"),
    ])
    def test_a_broken_field_is_reported(self, change, expected):
        games = copy.deepcopy(LIIGA_GAMES)
        change(games)
        problems = fc.check_liiga(games)
        assert any(expected in p for p in problems), problems

    def test_games_that_are_not_a_list(self):
        assert fc.check_liiga({"error": "x"}) == ["liiga games: expected list"]


def response(status=200, json_data=None, not_json=False):
    resp = MagicMock(status_code=status)
    if not_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_data
    return resp


class TestGetJson:
    def test_an_answer_is_returned(self):
        session = MagicMock(get=MagicMock(return_value=response(json_data={"a": 1})))
        assert fc.get_json(session, "u") == {"a": 1}

    @pytest.mark.parametrize("status", [404, 410])
    def test_a_gone_endpoint_is_a_change(self, status):
        session = MagicMock(get=MagicMock(return_value=response(status)))
        with pytest.raises(fc.EndpointGone):
            fc.get_json(session, "u")

    @pytest.mark.parametrize("status", [403, 429, 500, 502, 503])
    def test_another_error_status_is_an_outage(self, status):
        session = MagicMock(get=MagicMock(return_value=response(status)))
        with pytest.raises(fc.FeedUnreachable):
            fc.get_json(session, "u")

    @pytest.mark.parametrize("error", [requests.exceptions.Timeout(), requests.exceptions.ConnectionError()])
    def test_no_answer_is_an_outage(self, error):
        session = MagicMock(get=MagicMock(side_effect=error))
        with pytest.raises(fc.FeedUnreachable):
            fc.get_json(session, "u")

    def test_an_answer_that_is_not_json_is_a_change_of_shape(self):
        session = MagicMock(get=MagicMock(return_value=response(not_json=True)))
        assert fc.get_json(session, "u") is None


class TestRun:
    """run() with both fetch steps replaced: the exit status and the lines it prints."""

    def _run(self, monkeypatch, nhl, liiga, actions=False):
        monkeypatch.setattr(fc, "_run_nhl", lambda _nhl, _out: nhl())
        monkeypatch.setattr(fc, "_run_liiga", lambda _liiga, _out: liiga())
        if actions:
            monkeypatch.setenv("GITHUB_ACTIONS", "true")
        else:
            monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        lines = []
        return fc.run(nhl=object(), liiga=object(), out=lines.append), lines

    @staticmethod
    def _raise(error):
        def step():
            raise error
        return step

    def test_all_good(self, monkeypatch):
        status, lines = self._run(monkeypatch, lambda: [], lambda: [])
        assert status == 0 and lines[-1].startswith("All checked fields")

    def test_a_shape_change_fails_with_one_line_per_problem(self, monkeypatch):
        status, lines = self._run(monkeypatch, lambda: ["nhl score: a: missing in 1 of 1"], lambda: [])
        assert status == 1 and lines == ["SHAPE  nhl score: a: missing in 1 of 1"]

    def test_an_outage_is_told_apart_from_a_change(self, monkeypatch):
        status, lines = self._run(monkeypatch, self._raise(fc.FeedUnreachable("u: HTTP 503")), lambda: [])
        assert status == 2 and lines == ["DOWN   nhl: u: HTTP 503"]

    def test_a_gone_endpoint_is_a_change(self, monkeypatch):
        status, lines = self._run(monkeypatch, lambda: [], self._raise(fc.EndpointGone("u: HTTP 404")))
        assert status == 1 and lines == ["SHAPE  liiga: endpoint gone: u: HTTP 404"]

    def test_a_change_wins_over_an_outage_and_both_are_reported(self, monkeypatch):
        status, lines = self._run(monkeypatch, self._raise(fc.FeedUnreachable("u: Timeout")), lambda: ["liiga game: x"])
        assert status == 1 and len(lines) == 2

    def test_in_github_actions_the_lines_are_annotations(self, monkeypatch):
        _, lines = self._run(monkeypatch, lambda: ["nhl score: x"], self._raise(fc.FeedUnreachable("u: Timeout")),
                             actions=True)
        assert lines == ["::error title=Feed shape changed::nhl score: x", "::warning title=Feed unreachable::liiga: u: Timeout"]
