import datetime
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from irc_format import BOLD, RESET, GREEN, ORANGE
from nhl_command import NHLCommand
from tests.conftest import join_channel_thread, make_json_response as make_response


def roster_spot(player_id, first="John", last="Doe"):
    return {
        "playerId": player_id,
        "firstName": {"default": first},
        "lastName": {"default": last},
    }


def goal_play(event_id=1, period_number=1, period_type="REG", time_in_period="04:31",
              scoring_player_id=100, event_owner_team_id=10, home_score=1, away_score=0,
              assist1=None, assist2=None, situation_code=None):
    details = {
        "scoringPlayerId": scoring_player_id,
        "eventOwnerTeamId": event_owner_team_id,
        "homeScore": home_score,
        "awayScore": away_score,
    }
    if assist1 is not None:
        details["assist1PlayerId"] = assist1
    if assist2 is not None:
        details["assist2PlayerId"] = assist2
    play = {
        "eventId": event_id,
        "typeDescKey": "goal",
        "periodDescriptor": {"number": period_number, "periodType": period_type},
        "timeInPeriod": time_in_period,
        "details": details,
    }
    if situation_code is not None:
        play["situationCode"] = situation_code  # a play-level field in the real feed, not in details
    return play


def make_pbp(game_id=2026010026, home_id=10, away_id=20, home_abbrev="CAR", away_abbrev="FLA",
             home_place="Carolina", home_common="Hurricanes", away_place="Florida",
             away_common="Panthers", home_score=0, away_score=0, game_state="LIVE",
             last_period_type=None, plays=None, roster=None, season=None):
    pbp = {
        "id": game_id,
        "gameState": game_state,
        "homeTeam": {
            "id": home_id, "abbrev": home_abbrev, "score": home_score,
            "placeName": {"default": home_place}, "commonName": {"default": home_common},
        },
        "awayTeam": {
            "id": away_id, "abbrev": away_abbrev, "score": away_score,
            "placeName": {"default": away_place}, "commonName": {"default": away_common},
        },
        "gameOutcome": {"lastPeriodType": last_period_type} if last_period_type else {},
        "plays": plays or [],
        "rosterSpots": roster or [],
    }
    if season is not None:
        pbp["season"] = season
    return pbp


def seeded(*ids):
    """The record of a goal that was already in the feed when tracking
    started: remembered, but nothing was posted for it."""
    return {"ids": list(ids), "posted": None, "missing": 0, "retracted": False}


def make_schedule_game(gid=2026010026, home_id=10, away_id=20, home_abbrev="CAR", away_abbrev="FLA",
                        start_time_utc="2026-09-29T21:00:00Z", game_state="FUT",
                        home_score=None, away_score=None):
    home = {"id": home_id, "abbrev": home_abbrev}
    away = {"id": away_id, "abbrev": away_abbrev}
    if home_score is not None:  # the real schedule payload only carries scores once a game has started
        home["score"] = home_score
    if away_score is not None:
        away["score"] = away_score
    return {
        "id": gid,
        "startTimeUTC": start_time_utc,
        "gameState": game_state,
        "homeTeam": home,
        "awayTeam": away,
    }


def make_schedule_payload(date_to_games, next_start_date=None):
    """date_to_games: {date_str: [game_dict, ...]}. Every date not
    present gets an empty (0-game) entry, matching the real API always
    returning a 7-day window regardless of which days have games."""
    return {
        "nextStartDate": next_start_date,
        "gameWeek": [
            {"date": date, "numberOfGames": len(games), "games": games}
            for date, games in date_to_games.items()
        ],
    }


def html_response(text, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


@pytest.fixture
def nhl_command():
    command = NHLCommand()
    # A FINAL: announcement fetches nhl.com's game report for attendance, so
    # by default every GET here answers 404 (no attendance) - a test that
    # cares patches session.get itself. Nothing in this file may reach the
    # real network.
    command.session.get = MagicMock(return_value=html_response("", status_code=404))
    return command


class TestEndToEnd:
    """Exercises execute() -> _run()/_run_next() end-to-end, covering the
    thin _fetch_next_period/_format_period_summary/_fetch_today_items
    hooks that the more targeted tests above call around rather than
    through."""

    def test_start_tracks_and_reports(self, nhl_command):
        bot = MagicMock()
        past_start = (
            datetime.datetime.now(nhl_command.HELSINKI_TZ) - datetime.timedelta(hours=1)
        ).isoformat()
        schedule_game = make_schedule_game(
            gid=1, home_id=10, away_id=20, game_state="LIVE", start_time_utc=past_start,
        )
        pbp = make_pbp(game_id=1, home_id=10, away_id=20)

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
             patch.object(nhl_command, "_poll_loop"):
            result = nhl_command.execute("start", irc_bot=bot, channel="#nhl.fi")
            join_channel_thread(nhl_command, "#nhl.fi")

        assert "Checking today's NHL games" in result
        message = bot.send_message.call_args[0][1]
        assert "Tracking 1 NHL game(s) today" in message
        assert "CAR-FLA" in message

    def test_next_reports_upcoming_gameday(self, nhl_command):
        bot = MagicMock()
        schedule_game = make_schedule_game(gid=1, home_abbrev="TOR", away_abbrev="MTL")

        with patch.object(nhl_command, "_fetch_next_gameday", return_value=("2026-09-29", {1: schedule_game})):
            result = nhl_command.execute("next", irc_bot=bot, channel="#nhl.fi")
            time.sleep(0.2)  # let the one-shot background thread finish

        message = bot.send_message.call_args[0][1]
        assert "Checking the next NHL gameday" in result
        assert "Next NHL gameday" in message
        assert "TOR-MTL" in message


class TestFormatGamesSummary:
    def test_groups_by_start_time_using_abbreviations(self, nhl_command):
        games = [
            make_schedule_game(gid=1, home_abbrev="CAR", away_abbrev="FLA", start_time_utc="2026-09-29T21:00:00Z"),
            make_schedule_game(gid=2, home_abbrev="TOR", away_abbrev="MTL", start_time_utc="2026-09-29T21:00:00Z"),
        ]
        summary = nhl_command._format_games_summary(games)
        assert "CAR-FLA" in summary
        assert "TOR-MTL" in summary


class TestTeamName:
    def test_combines_place_and_common_name(self, nhl_command):
        team = {"placeName": {"default": "Florida"}, "commonName": {"default": "Panthers"}}
        assert nhl_command._team_name(team) == "Florida Panthers"

    def test_missing_fields_falls_back_to_unknown(self, nhl_command):
        assert nhl_command._team_name({}) == "Unknown"


class TestRealGoals:
    def test_filters_by_team_and_requires_a_scorer(self, nhl_command):
        pbp = make_pbp(home_id=10, away_id=20, plays=[
            goal_play(event_owner_team_id=10, scoring_player_id=100),
            goal_play(event_owner_team_id=20, scoring_player_id=200),
            # Not a goal at all.
            {"typeDescKey": "shot-on-goal", "details": {"eventOwnerTeamId": 10}},
            # A goal-typed play with no scorer - shouldn't count as real.
            {"typeDescKey": "goal", "details": {"eventOwnerTeamId": 10}},
        ])
        home_goals = nhl_command._real_goals(pbp, "homeTeam")
        away_goals = nhl_command._real_goals(pbp, "awayTeam")
        assert len(home_goals) == 1
        assert len(away_goals) == 1

    def test_shootout_goals_are_included(self, nhl_command):
        pbp = make_pbp(home_id=10, plays=[
            goal_play(period_number=5, period_type="SO", event_owner_team_id=10, home_score=2, away_score=2),
        ])
        assert len(nhl_command._real_goals(pbp, "homeTeam")) == 1


class TestResolvePlayerName:
    def test_resolves_from_embedded_roster(self, nhl_command):
        pbp = make_pbp(roster=[roster_spot(100, "Aleksander", "Barkov")])
        assert nhl_command._resolve_player_name(pbp, 100) == "Aleksander Barkov"

    def test_unresolvable_id_falls_back_to_unknown(self, nhl_command):
        pbp = make_pbp(roster=[])
        assert nhl_command._resolve_player_name(pbp, 999) == "Unknown"

    def test_none_id_falls_back_to_unknown(self, nhl_command):
        assert nhl_command._resolve_player_name(make_pbp(), None) == "Unknown"


class TestFormatGoal:
    def test_regular_goal_with_assists(self, nhl_command):
        pbp = make_pbp(
            home_id=10, away_id=20, home_score=1, away_score=0,
            roster=[roster_spot(100, "Bradly", "Nadeau"), roster_spot(101, "Mike", "Reilly")],
        )
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, assist1=101, home_score=1, away_score=0)

        message = nhl_command._format_goal(pbp, goal)

        assert message == (
            f"{nhl_command.GOAL_PREFIX} {BOLD}Carolina Hurricanes 1-0 Florida Panthers{RESET}"
            " 04:31 1st | Carolina Hurricanes — Bradly Nadeau (assists: Mike Reilly)"
        )

    def test_away_goal_credits_away_team(self, nhl_command):
        pbp = make_pbp(home_id=10, away_id=20, roster=[roster_spot(200, "Sam", "Reinhart")])
        goal = goal_play(event_owner_team_id=20, scoring_player_id=200)

        message = nhl_command._format_goal(pbp, goal)
        assert "Florida Panthers — Sam Reinhart" in message

    def test_unresolvable_scorer_shows_unknown(self, nhl_command):
        pbp = make_pbp(home_id=10, roster=[])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=999)
        assert "— Unknown" in nhl_command._format_goal(pbp, goal)

    def test_no_assists_omits_the_assists_clause(self, nhl_command):
        pbp = make_pbp(home_id=10, roster=[roster_spot(100)])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100)
        assert "assists" not in nhl_command._format_goal(pbp, goal)

    def test_shootout_goal_shows_so_label(self, nhl_command):
        pbp = make_pbp(home_id=10, roster=[roster_spot(100)])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, period_number=5, period_type="SO")
        assert " SO |" in nhl_command._format_goal(pbp, goal)

    def test_overtime_goal_shows_ot_label(self, nhl_command):
        pbp = make_pbp(home_id=10, roster=[roster_spot(100)])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, period_number=4, period_type="OT")
        assert " OT |" in nhl_command._format_goal(pbp, goal)


class TestAnnounceEnd:
    def test_regulation_final_has_no_suffix(self, nhl_command):
        bot = MagicMock()
        pbp = make_pbp(home_score=5, away_score=4, last_period_type=None)
        nhl_command._announce_end(bot, "#nhl.fi", pbp)
        message = bot.send_message.call_args[0][1]
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 5-4 Florida Panthers"

    def test_overtime_final_gets_ot_suffix(self, nhl_command):
        bot = MagicMock()
        pbp = make_pbp(home_score=3, away_score=2, last_period_type="OT")
        nhl_command._announce_end(bot, "#nhl.fi", pbp)
        assert bot.send_message.call_args[0][1].endswith("(OT)")

    def test_shootout_final_gets_so_suffix(self, nhl_command):
        bot = MagicMock()
        pbp = make_pbp(home_score=3, away_score=2, last_period_type="SO")
        nhl_command._announce_end(bot, "#nhl.fi", pbp)
        assert bot.send_message.call_args[0][1].endswith("(SO)")


class TestFailedFinalFetch:
    """Issue #26: a game that turns FINAL in a poll whose play-by-play request
    fails is not marked as ended, so the next poll still sends its FINAL: line."""

    def _poll(self, nhl_command, bot, pbp):
        game = make_schedule_game(gid=1, game_state="FINAL", home_score=3, away_score=2)
        with patch.object(nhl_command, "_fetch_tracked_games", return_value={1: game}), \
                patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
                patch.object(nhl_command, "_fetch_attendance", return_value=18000):
            return nhl_command._poll_once(bot, "#nhl.fi")

    def _finals(self, bot):
        return [c[0][1] for c in bot.send_message.call_args_list if "FINAL:" in c[0][1]]

    def test_the_final_line_is_sent_by_the_next_poll_and_only_once(self, nhl_command):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        }}
        pbp = make_pbp(game_id=1, home_score=3, away_score=2, game_state="FINAL")
        assert self._poll(nhl_command, bot, None) is False  # the tracker must not stop on the failed poll
        assert self._finals(bot) == []
        assert self._poll(nhl_command, bot, pbp) is True
        assert len(self._finals(bot)) == 1
        self._poll(nhl_command, bot, pbp)
        assert len(self._finals(bot)) == 1


class TestFetchPlayByPlay:
    def test_happy_path_returns_json(self, nhl_command):
        with patch.object(nhl_command.session, "get", return_value=make_response(make_pbp())):
            result = nhl_command._fetch_play_by_play(2026010026)
        assert result is not None
        assert result["id"] == 2026010026

    def test_invalid_json_returns_none_not_raises(self, nhl_command):
        resp = make_response({})
        resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "not json", 0)
        with patch.object(nhl_command.session, "get", return_value=resp):
            assert nhl_command._fetch_play_by_play(1) is None

    def test_http_error_returns_none_not_raises(self, nhl_command):
        with patch.object(nhl_command.session, "get", return_value=make_response({}, status_code=404)):
            assert nhl_command._fetch_play_by_play(1) is None

    def test_connection_error_returns_none_not_raises(self, nhl_command):
        with patch.object(nhl_command.session, "get", side_effect=requests.exceptions.ConnectionError):
            assert nhl_command._fetch_play_by_play(1) is None


class TestFetchSchedule:
    def test_happy_path_returns_json(self, nhl_command):
        payload = make_schedule_payload({"2026-09-29": [make_schedule_game()]})
        with patch.object(nhl_command.session, "get", return_value=make_response(payload)):
            result = nhl_command._fetch_schedule("2026-09-29")
        assert result["gameWeek"][0]["date"] == "2026-09-29"

    def test_invalid_json_returns_none_not_raises(self, nhl_command):
        resp = make_response({})
        resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "not json", 0)
        with patch.object(nhl_command.session, "get", return_value=resp):
            assert nhl_command._fetch_schedule("2026-09-29") is None

    def test_timeout_returns_none_not_raises(self, nhl_command):
        with patch.object(nhl_command.session, "get", side_effect=requests.exceptions.Timeout):
            assert nhl_command._fetch_schedule("2026-09-29") is None


class TestFetchTodayGames:
    def test_returns_games_for_todays_eastern_date(self, nhl_command):
        today = nhl_command._today_eastern_str()
        payload = make_schedule_payload({today: [make_schedule_game(gid=1)]})
        with patch.object(nhl_command, "_fetch_schedule", return_value=payload):
            games = nhl_command._fetch_today_games()
        assert list(games.keys()) == [1]

    def test_no_games_today_returns_empty_dict(self, nhl_command):
        today = nhl_command._today_eastern_str()
        payload = make_schedule_payload({today: []})
        with patch.object(nhl_command, "_fetch_schedule", return_value=payload):
            assert nhl_command._fetch_today_games() == {}

    def test_fetch_failure_returns_none(self, nhl_command):
        with patch.object(nhl_command, "_fetch_schedule", return_value=None):
            assert nhl_command._fetch_today_games() is None

    def test_response_missing_todays_date_entirely_returns_empty_dict(self, nhl_command):
        # Defensive fallback for a response shape that doesn't include
        # today's own date at all - not observed live (the real API
        # always echoes the requested date back as the first entry), but
        # this must degrade to "nothing today" rather than raise/crash.
        payload = make_schedule_payload({"1999-01-01": [make_schedule_game(gid=1)]})
        with patch.object(nhl_command, "_fetch_schedule", return_value=payload):
            assert nhl_command._fetch_today_games() == {}


class TestHelsinkiDateLabel:
    def test_uses_the_earliest_games_helsinki_date(self, nhl_command):
        games = {
            1: make_schedule_game(gid=1, start_time_utc="2026-09-29T23:00:00Z"),
            2: make_schedule_game(gid=2, start_time_utc="2026-09-30T02:00:00Z"),
        }
        # Both convert to Helsinki 2026-09-30 (+03:00) - earliest wins,
        # but they agree here anyway; see test_finds_the_earliest below
        # for a case where they genuinely differ.
        assert nhl_command._helsinki_date_label(games, "2026-09-29") == "2026-09-30"

    def test_finds_the_earliest_when_games_span_two_helsinki_dates(self, nhl_command):
        games = {
            1: make_schedule_game(gid=1, start_time_utc="2026-09-29T13:00:00Z"),  # same Helsinki day
            2: make_schedule_game(gid=2, start_time_utc="2026-09-29T23:00:00Z"),  # next Helsinki day
        }
        assert nhl_command._helsinki_date_label(games, "2026-09-29") == "2026-09-29"

    def test_falls_back_to_eastern_date_when_no_start_time_parses(self, nhl_command):
        games = {1: {"startTimeUTC": "not-a-timestamp"}}
        assert nhl_command._helsinki_date_label(games, "2026-09-29") == "2026-09-29"

    def test_empty_games_falls_back_to_eastern_date(self, nhl_command):
        assert nhl_command._helsinki_date_label({}, "2026-09-29") == "2026-09-29"


class TestFetchNextGameday:
    def test_finds_next_day_with_unfinished_games_in_same_week(self, nhl_command):
        today = nhl_command._today_eastern_str()
        tomorrow = (
            datetime.datetime.now(nhl_command.EASTERN_TZ) + datetime.timedelta(days=1)
        ).strftime("%Y-%m-%d")
        # Early UTC time (13:00Z) so the Helsinki-converted date lines up
        # with the Eastern bucket date for this test's purposes - the
        # deliberate Eastern-evening-crosses-into-next-Helsinki-day case
        # is its own dedicated test below.
        payload = make_schedule_payload({
            today: [],
            tomorrow: [make_schedule_game(gid=1, game_state="FUT", start_time_utc=f"{tomorrow}T13:00:00Z")],
        })
        with patch.object(nhl_command, "_fetch_schedule", return_value=payload):
            date_str, games = nhl_command._fetch_next_gameday()
        assert date_str == tomorrow
        assert list(games.keys()) == [1]

    def test_skips_a_day_whose_games_are_all_already_finished(self, nhl_command):
        today = nhl_command._today_eastern_str()
        tomorrow = (
            datetime.datetime.now(nhl_command.EASTERN_TZ) + datetime.timedelta(days=1)
        ).strftime("%Y-%m-%d")
        payload = make_schedule_payload({
            today: [make_schedule_game(gid=1, game_state="FINAL")],
            tomorrow: [make_schedule_game(gid=2, game_state="FUT", start_time_utc=f"{tomorrow}T13:00:00Z")],
        })
        with patch.object(nhl_command, "_fetch_schedule", return_value=payload):
            date_str, games = nhl_command._fetch_next_gameday()
        assert date_str == tomorrow

    def test_hops_via_next_start_date_when_whole_window_is_empty(self, nhl_command):
        today = nhl_command._today_eastern_str()
        far_date = "2099-01-01"
        first_week = make_schedule_payload({today: []}, next_start_date=far_date)
        second_week = make_schedule_payload({
            far_date: [make_schedule_game(gid=1, game_state="FUT", start_time_utc=f"{far_date}T13:00:00Z")],
        })

        with patch.object(nhl_command, "_fetch_schedule", side_effect=[first_week, second_week]):
            date_str, games = nhl_command._fetch_next_gameday()
        assert date_str == far_date
        assert list(games.keys()) == [1]

    def test_eastern_evening_game_is_labeled_by_its_own_helsinki_date_not_the_api_bucket(self, nhl_command):
        # Regression test for a real incident: the API's own gameWeek
        # "date" is an Eastern calendar date, but !nhl next used to hand
        # that straight to the shared "today"/"tomorrow" labeling logic,
        # which compares it against Helsinki's own "today" - an Eastern
        # evening game actually lands on the *following* Helsinki
        # calendar date, so a slate the API bucketed under Eastern
        # "2026-09-29" (with every game in the evening, Helsinki-local
        # 2026-09-30) was mislabeled "tomorrow" instead of the correct,
        # later date.
        eastern_bucket_date = "2026-09-29"
        payload = make_schedule_payload({
            nhl_command._today_eastern_str(): [],
            eastern_bucket_date: [
                make_schedule_game(gid=1, game_state="FUT", start_time_utc="2026-09-29T21:00:00Z"),
            ],
        })
        with patch.object(nhl_command, "_fetch_schedule", return_value=payload):
            date_str, games = nhl_command._fetch_next_gameday()

        # 2026-09-29T21:00:00Z is 2026-09-30 00:00 Helsinki time (+03:00).
        assert date_str == "2026-09-30"
        assert date_str != eastern_bucket_date

    def test_fetch_failure_returns_none_none(self, nhl_command):
        with patch.object(nhl_command, "_fetch_schedule", return_value=None):
            assert nhl_command._fetch_next_gameday() == (None, None)

    def test_bounded_search_gives_up_eventually(self, nhl_command):
        today = nhl_command._today_eastern_str()
        empty_week = make_schedule_payload({today: []}, next_start_date="2099-01-01")
        with patch.object(nhl_command, "_fetch_schedule", return_value=empty_week):
            assert nhl_command._fetch_next_gameday() == (None, None)


class TestBuildInitialState:
    def test_seeds_the_announced_goals_from_play_by_play(self, nhl_command):
        items = {1: make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")}
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, plays=[
            goal_play(event_id=1, event_owner_team_id=10, scoring_player_id=100, home_score=1, away_score=0),
            goal_play(event_id=2, event_owner_team_id=20, scoring_player_id=200, home_score=1, away_score=1),
            goal_play(event_id=3, event_owner_team_id=20, scoring_player_id=201, home_score=1, away_score=2),
        ])
        with patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp):
            state = nhl_command._build_initial_state(items)

        assert state[1]["announced"] == {
            ("score", 10, 1, 0): seeded(1), ("score", 20, 1, 1): seeded(2), ("score", 20, 1, 2): seeded(3),
        }
        assert state[1]["ended"] is False

    def test_play_by_play_failure_leaves_the_announced_record_empty(self, nhl_command):
        items = {1: make_schedule_game(gid=1)}
        with patch.object(nhl_command, "_fetch_play_by_play", return_value=None):
            state = nhl_command._build_initial_state(items)
        assert state[1]["announced"] == {}

    def test_empty_items_short_circuits(self, nhl_command):
        assert nhl_command._build_initial_state({}) == {}


class TestPollOnce:
    def _seed(self, nhl_command, channel, games_state):
        nhl_command._channels[channel] = {"stop_event": MagicMock(), "thread": None, "games": games_state}

    def test_new_goal_is_announced(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, roster=[roster_spot(100, "Bradly", "Nadeau")], plays=[
            goal_play(event_owner_team_id=10, scoring_player_id=100),
        ])

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp):
            nhl_command._poll_once(bot, "#nhl.fi")

        message = bot.send_message.call_args[0][1]
        assert "GOAL:" in message
        assert "Bradly Nadeau" in message

    def test_no_new_goals_sends_nothing(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "announced": {("score", 10, 1, 0): seeded(1)}, "ended": False},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, plays=[
            goal_play(event_owner_team_id=10, scoring_player_id=100),
        ])

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp):
            nhl_command._poll_once(bot, "#nhl.fi")

        bot.send_message.assert_not_called()

    def test_game_end_is_announced(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="FINAL")
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, home_score=5, away_score=2, game_state="FINAL")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
             patch.object(nhl_command, "_fetch_attendance", return_value=18000):
            all_ended = nhl_command._poll_once(bot, "#nhl.fi")

        message = bot.send_message.call_args[0][1]
        assert "FINAL:" in message
        assert all_ended is True

    def test_already_ended_game_is_not_touched_again(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": True},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="FINAL")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play") as mock_pbp:
            all_ended = nhl_command._poll_once(bot, "#nhl.fi")

        bot.send_message.assert_not_called()
        assert all_ended is True

    def test_new_game_appearing_mid_tracking_is_seeded_silently(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {})
        schedule_game = make_schedule_game(gid=2, home_id=10, away_id=20, game_state="LIVE")
        pbp = make_pbp(game_id=2, home_id=10, away_id=20, plays=[
            goal_play(event_owner_team_id=10, scoring_player_id=100),
        ])

        with patch.object(nhl_command, "_fetch_today_games", return_value={2: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp):
            all_ended = nhl_command._poll_once(bot, "#nhl.fi")

        bot.send_message.assert_not_called()
        assert all_ended is False
        assert nhl_command._channels["#nhl.fi"]["games"][2]["announced"] == {("score", 10, 1, 0): seeded(1)}

    def test_play_by_play_failure_carries_forward_the_announced_record(self, nhl_command):
        bot = MagicMock()
        record = {("score", 10, 1, 0): seeded(1), ("score", 20, 1, 1): seeded(2)}
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "announced": dict(record), "ended": False},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=None):
            nhl_command._poll_once(bot, "#nhl.fi")

        bot.send_message.assert_not_called()
        assert nhl_command._channels["#nhl.fi"]["games"][1]["announced"] == record

    def test_fetch_failure_returns_false_without_crashing(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False}})

        with patch.object(nhl_command, "_fetch_today_games", return_value=None):
            assert nhl_command._poll_once(bot, "#nhl.fi") is False

    def test_stopped_channel_returns_true(self, nhl_command):
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_today_games", return_value={}):
            assert nhl_command._poll_once(bot, "#nhl.fi") is True

    def test_unexpected_fetch_exception_for_one_game_does_not_crash_the_poll(self, nhl_command):
        # The concurrent play-by-play fetch itself raising (not just
        # returning None) for one game must not prevent every other
        # tracked game in the same batch from being processed.
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", side_effect=RuntimeError("boom")):
            all_ended = nhl_command._poll_once(bot, "#nhl.fi")  # must not raise

        assert all_ended is False

    def test_unexpected_exception_processing_one_game_does_not_crash_the_poll(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")
        pbp = make_pbp(game_id=1, home_id=10, away_id=20)

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
             patch.object(nhl_command, "_announce_new_goals", side_effect=RuntimeError("boom")):
            all_ended = nhl_command._poll_once(bot, "#nhl.fi")  # must not raise

        assert all_ended is False


class TestGuardWiring:
    """Confirms NHLCommand's own START_TIME_KEY ("startTimeUTC") and
    ENDED_STATE_KEY ("ended") wiring into the shared early-start/
    already-finished guards - the guards' own logic is covered
    exhaustively in test_live_tracker_command.py."""

    def test_refuses_to_start_more_than_15_minutes_before_first_game(self, nhl_command):
        bot = MagicMock()
        stop_event = threading.Event()
        nhl_command._channels["#nhl.fi"] = {"stop_event": stop_event, "thread": None, "games": {}}

        future_start = (
            datetime.datetime.now(nhl_command.HELSINKI_TZ) + datetime.timedelta(minutes=30)
        ).isoformat()
        with patch.object(
            nhl_command, "_fetch_today_games",
            return_value={1: make_schedule_game(gid=1, start_time_utc=future_start)},
        ):
            nhl_command._run(bot, "#nhl.fi", stop_event)

        message = bot.send_message.call_args[0][1]
        assert "Too early to track" in message
        assert "#nhl.fi" not in nhl_command._channels

    def test_all_games_already_ended_sends_one_message_not_tracking_then_stopped(self, nhl_command):
        bot = MagicMock()
        stop_event = threading.Event()
        nhl_command._channels["#nhl.fi"] = {"stop_event": stop_event, "thread": None, "games": {}}

        pbp = make_pbp(home_id=10, away_id=20)
        past_start = (
            datetime.datetime.now(nhl_command.HELSINKI_TZ) - datetime.timedelta(hours=3)
        ).isoformat()
        with patch.object(
            nhl_command, "_fetch_today_games",
            return_value={1: make_schedule_game(
                gid=1, home_id=10, away_id=20, game_state="FINAL", start_time_utc=past_start,
            )},
        ), patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp):
            nhl_command._run(bot, "#nhl.fi", stop_event)

        bot.send_message.assert_called_once_with(
            "#nhl.fi", "All of today's NHL games have already finished.",
        )
        assert "#nhl.fi" not in nhl_command._channels


class TestTrackingSummary:
    """The "Tracking N games today: ..." list adds scores for games already
    underway (goals scored before !nhl start are deliberately never
    announced, so without this a mid-game start leaves the channel
    guessing); "!nhl next" shows the same scores for a day that is partly played."""

    def _one(self, **kwargs):
        return [make_schedule_game(home_abbrev="TOR", away_abbrev="MTL", **kwargs)]

    def test_future_and_pregame_games_show_no_score(self, nhl_command):
        for state in ("FUT", "PRE"):
            assert nhl_command._format_tracking_summary(self._one(game_state=state)).endswith("TOR-MTL")

    def test_live_game_shows_its_score(self, nhl_command):
        games = self._one(game_state="LIVE", home_score=1, away_score=0)
        assert nhl_command._format_tracking_summary(games).endswith("TOR 1-0 MTL")

    def test_zero_zero_live_game_still_shows_its_score(self, nhl_command):
        games = self._one(game_state="LIVE", home_score=0, away_score=0)
        assert nhl_command._format_tracking_summary(games).endswith("TOR 0-0 MTL")

    def test_finished_game_is_marked_final(self, nhl_command):
        for state in ("FINAL", "OFF"):
            games = self._one(game_state=state, home_score=3, away_score=2)
            assert nhl_command._format_tracking_summary(games).endswith("TOR 3-2 MTL (final)")

    def test_started_game_without_numeric_scores_shows_no_score(self, nhl_command):
        games = self._one(game_state="LIVE")  # no score keys at all
        assert nhl_command._format_tracking_summary(games).endswith("TOR-MTL")

    def test_mixed_slate_keeps_time_grouping(self, nhl_command):
        games = [
            make_schedule_game(gid=1, home_abbrev="CAR", away_abbrev="FLA", game_state="FINAL",
                               home_score=0, away_score=1, start_time_utc="2026-09-29T21:00:00Z"),
            make_schedule_game(gid=2, home_abbrev="EDM", away_abbrev="VAN", game_state="FUT",
                               start_time_utc="2026-09-30T02:00:00Z"),
        ]
        assert nhl_command._format_tracking_summary(games) == "00:00 CAR 0-1 FLA (final) | 05:00 EDM-VAN"

    def test_next_shows_the_score_of_a_started_game(self, nhl_command):
        games = self._one(game_state="LIVE", home_score=1, away_score=0)
        assert nhl_command._format_period_summary(games).endswith("TOR 1-0 MTL")

    def test_next_on_a_day_nothing_has_started_on_is_the_plain_list(self, nhl_command):
        assert nhl_command._format_period_summary(self._one(game_state="FUT")).endswith("TOR-MTL")

    def test_next_on_a_partly_played_day_marks_the_final_and_the_live_game(self, nhl_command):
        games = [
            make_schedule_game(gid=1, home_abbrev="DET", away_abbrev="WPG", game_state="FINAL",
                               home_score=2, away_score=3, start_time_utc="2026-10-04T23:00:00Z"),
            make_schedule_game(gid=2, home_abbrev="NYR", away_abbrev="UTA", game_state="LIVE",
                               home_score=0, away_score=0, start_time_utc="2026-10-05T01:00:00Z"),
            make_schedule_game(gid=3, home_abbrev="VAN", away_abbrev="VGK", game_state="FUT",
                               start_time_utc="2026-10-05T02:00:00Z"),
        ]
        assert nhl_command._format_period_summary(games) == (
            "02:00 DET 2-3 WPG (final) | 04:00 NYR 0-0 UTA | 05:00 VAN-VGK"
        )

    def test_start_message_includes_the_live_score(self, nhl_command):
        bot = MagicMock()
        past_start = (datetime.datetime.now(nhl_command.HELSINKI_TZ) - datetime.timedelta(hours=1)).isoformat()
        game = make_schedule_game(gid=1, home_id=10, away_id=20, home_abbrev="TOR", away_abbrev="MTL",
                                  game_state="LIVE", start_time_utc=past_start, home_score=1, away_score=1)

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=make_pbp(game_id=1, home_id=10, away_id=20)), \
             patch.object(nhl_command, "_poll_loop"):
            nhl_command.execute("start", irc_bot=bot, channel="#nhl.fi")
            join_channel_thread(nhl_command, "#nhl.fi")

        assert "TOR 1-1 MTL" in bot.send_message.call_args[0][1]


class TestGoalTags:
    """PP / SH / EN, decoded from the goal play's situationCode (away goalie,
    away skaters, home skaters, home goalie) - validated against NHL's own
    per-goal labels on 250 real goals with 0 mismatches. Home team id is 10,
    away 20 in make_pbp()."""

    def _tags(self, nhl_command, code, scored_by_home, period_type="REG"):
        pbp = make_pbp(home_id=10, away_id=20)
        goal = goal_play(event_owner_team_id=10 if scored_by_home else 20,
                         period_type=period_type, situation_code=code)
        return nhl_command._goal_tags(pbp, goal)

    @pytest.mark.parametrize("code, by_home, period_type, expected", [
        ("1551", True, "REG", []),            # five on five
        ("1451", False, "REG", ["SH"]),       # real EDM-VAN goal: away scored 4-on-5
        ("1541", False, "REG", ["PP"]),       # real EDM-VAN goal: away scored 5-on-4
        ("1541", True, "REG", ["SH"]),        # same code, scored by the team with 4 skaters
        ("1460", True, "REG", ["PP"]),        # real: home goalie pulled, 6-on-4 - NHL calls it a power play
        ("1331", False, "OT", []),            # three on three overtime
        ("1441", True, "REG", []),            # four on four
        ("0651", True, "REG", ["EN"]),        # away pulled the goalie; home scores into the empty net
        ("0641", True, "REG", ["SH", "EN"]),  # ...while shorthanded
        ("0651", False, "REG", []),           # the pulled-goalie team itself scoring 6-on-5: even strength
        ("1541", True, "SO", []),             # shootout goals get no tag whatever the code says
    ])
    def test_decoding(self, nhl_command, code, by_home, period_type, expected):
        assert self._tags(nhl_command, code, by_home, period_type) == expected

    def test_numeric_code_that_lost_its_leading_zero_is_restored(self, nhl_command):
        assert self._tags(nhl_command, 651, True) == ["EN"]  # int 651 is really "0651"

    @pytest.mark.parametrize("code", [None, "", "abc", "155", "15511", "0000", True, 1.5])
    def test_missing_or_malformed_code_means_no_tag(self, nhl_command, code):
        assert self._tags(nhl_command, code, True) == []

    def test_a_goal_without_any_code_is_untagged(self, nhl_command):
        pbp = make_pbp(home_id=10)
        assert nhl_command._goal_tags(pbp, goal_play(event_owner_team_id=10)) == []

    def test_tag_appears_between_scorer_and_assists(self, nhl_command):
        pbp = make_pbp(home_id=10, away_id=20, roster=[roster_spot(100, "Evan", "Bouchard"), roster_spot(101, "Connor", "McDavid")])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, assist1=101, situation_code="1460")
        assert nhl_command._format_goal(pbp, goal).endswith("— Evan Bouchard (PP) (assists: Connor McDavid)")

    def test_combined_tags_are_slash_joined_like_liiga(self, nhl_command):
        pbp = make_pbp(home_id=10, away_id=20, roster=[roster_spot(100, "Evan", "Bouchard")])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, situation_code="0641")
        assert nhl_command._format_goal(pbp, goal).endswith("— Evan Bouchard (SH/EN)")


class TestFetchAttendance:
    REAL_MARKUP = '<td align="center" style="font-size: 10px;font-weight:bold">Attendance 19,250&nbsp;at&nbsp;Lenovo Center</td>'

    def _pbp(self):
        return make_pbp(game_id=2026020004, season=20262027)

    def test_parses_the_real_report_markup(self, nhl_command):
        with patch.object(nhl_command.session, "get", return_value=html_response(self.REAL_MARKUP)):
            assert nhl_command._fetch_attendance(self._pbp()) == 19250

    def test_builds_the_report_url_from_season_and_game_id(self, nhl_command):
        with patch.object(nhl_command.session, "get", return_value=html_response(self.REAL_MARKUP)) as mock_get:
            nhl_command._fetch_attendance(self._pbp())
        assert mock_get.call_args.args[0] == "https://www.nhl.com/scores/htmlreports/20262027/GS020004.HTM"
        assert mock_get.call_args.kwargs["headers"] == {"Accept": "text/html"}  # the session default is JSON

    @pytest.mark.parametrize("html, expected", [
        ("Attendance 17,850 at TD Garden", 17850),
        ("<b>Attendance</b> <b>10,226</b>", 10226),
        ("attendance 9,999", 9999),
    ])
    def test_tolerates_markup_and_spacing_variants(self, nhl_command, html, expected):
        with patch.object(nhl_command.session, "get", return_value=html_response(html)):
            assert nhl_command._fetch_attendance(self._pbp()) == expected

    def test_report_not_published_yet_returns_none(self, nhl_command, capsys):
        with patch.object(nhl_command.session, "get", return_value=html_response("", status_code=404)):
            assert nhl_command._fetch_attendance(self._pbp()) is None
        assert "no attendance" in capsys.readouterr().out

    def test_request_failure_returns_none(self, nhl_command):
        with patch.object(nhl_command.session, "get", side_effect=requests.exceptions.ConnectionError):
            assert nhl_command._fetch_attendance(self._pbp()) is None

    @pytest.mark.parametrize("html", ["<html>no figure here</html>", "Attendance TBD", "Attendance 0"])
    def test_page_without_a_usable_figure_returns_none(self, nhl_command, html):
        with patch.object(nhl_command.session, "get", return_value=html_response(html)):
            assert nhl_command._fetch_attendance(self._pbp()) is None

    def test_missing_season_or_id_makes_no_request(self, nhl_command):
        nhl_command.session.get.reset_mock()
        assert nhl_command._fetch_attendance(make_pbp(game_id=2026020004)) is None  # no season
        assert nhl_command._fetch_attendance({"season": 20262027}) is None  # no id
        nhl_command.session.get.assert_not_called()


class TestFinalWithAttendance:
    def test_final_line_includes_attendance_like_liiga(self, nhl_command):
        bot = MagicMock()
        pbp = make_pbp(home_score=3, away_score=2, last_period_type="OT")
        with patch.object(nhl_command, "_fetch_attendance", return_value=18347):
            nhl_command._announce_end(bot, "#nhl.fi", pbp)
        assert bot.send_message.call_args[0][1] == (
            f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers (OT) | Yleisöä: 18347"
        )

    def test_final_line_is_unchanged_when_attendance_is_unavailable(self, nhl_command):
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", make_pbp(home_score=5, away_score=4))
        message = bot.send_message.call_args[0][1]
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 5-4 Florida Panthers"

    def test_a_game_ending_during_a_poll_announces_it_with_attendance(self, nhl_command):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        }}
        game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="FINAL")
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, home_score=5, away_score=2, game_state="FINAL")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
             patch.object(nhl_command, "_fetch_attendance", return_value=19088):
            nhl_command._poll_once(bot, "#nhl.fi")

        assert bot.send_message.call_args[0][1].endswith("| Yleisöä: 19088")


class TestTrackedSlate:
    """Regression for a real night (2026-09-29, #nhl.fi): the schedule is
    bucketed by Eastern date, and Eastern midnight falls in the middle of
    the late games. The poll used to fetch "today's" slate, so at midnight
    the tracked games vanished from it - every later goal and FINAL was
    lost and the tracker quietly moved on to the next day's games."""

    def _eastern_days(self, nhl_command):
        now = datetime.datetime.now(nhl_command.EASTERN_TZ)
        return now.strftime("%Y-%m-%d"), (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    def _tracked(self, slate, gid=1, announced=None, ended=False):
        return {gid: {"home_id": 10, "away_id": 20, "announced": announced or {},
                      "ended": ended, "slate": slate}}

    def _seed(self, nhl_command, state):
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": state}

    def test_slates_by_date_tags_each_game_with_its_own_date(self, nhl_command):
        payload = make_schedule_payload({
            "2026-09-29": [make_schedule_game(gid=1)],
            "2026-09-30": [make_schedule_game(gid=2)],
        })
        slates = nhl_command._slates_by_date(payload)
        assert slates["2026-09-29"][1]["slateDate"] == "2026-09-29"
        assert slates["2026-09-30"][2]["slateDate"] == "2026-09-30"

    def test_snapshot_remembers_the_slate_and_falls_back_to_todays_eastern_date(self, nhl_command):
        game = make_schedule_game(gid=1)
        assert nhl_command._seed_snapshot({**game, "slateDate": "2026-09-29"})["slate"] == "2026-09-29"
        assert nhl_command._seed_snapshot(game)["slate"] == nhl_command._today_eastern_str()

    def test_start_includes_a_game_still_live_from_the_previous_eastern_day(self, nhl_command):
        today, yesterday = self._eastern_days(nhl_command)
        payload = make_schedule_payload({
            yesterday: [
                make_schedule_game(gid=1, game_state="LIVE"),   # a 10 pm start, still being played
                make_schedule_game(gid=2, game_state="CRIT"),
                make_schedule_game(gid=3, game_state="FINAL"),  # over - not worth tracking
                make_schedule_game(gid=4, game_state="FUT"),    # e.g. postponed - not in progress
            ],
            today: [make_schedule_game(gid=5, game_state="FUT")],
        })
        with patch.object(nhl_command, "_fetch_schedule", return_value=payload) as mock_fetch:
            games = nhl_command._fetch_today_games()

        assert sorted(games) == [1, 2, 5]
        assert games[1]["slateDate"] == yesterday and games[5]["slateDate"] == today
        mock_fetch.assert_called_once_with(yesterday)  # one call covers both days

    def test_tracked_games_fetch_starts_from_the_earliest_tracked_slate(self, nhl_command):
        payload = make_schedule_payload({
            "2026-09-29": [make_schedule_game(gid=1)],
            "2026-09-30": [make_schedule_game(gid=2)],
            "2026-10-01": [make_schedule_game(gid=3)],  # a day nobody tracks
        })
        state = {**self._tracked("2026-09-29", gid=1), **self._tracked("2026-09-30", gid=2)}
        with patch.object(nhl_command, "_fetch_schedule", return_value=payload) as mock_fetch:
            games = nhl_command._fetch_tracked_games(state)

        mock_fetch.assert_called_once_with("2026-09-29")
        assert sorted(games) == [1, 2]

    def test_tracked_games_fetch_falls_back_to_today_without_slate_info(self, nhl_command):
        with patch.object(nhl_command, "_fetch_today_games", return_value={7: {}}) as mock_today:
            assert nhl_command._fetch_tracked_games({1: {"ended": False}}) == {7: {}}
        mock_today.assert_called_once()

    def test_tracked_games_fetch_failure_returns_none(self, nhl_command):
        with patch.object(nhl_command, "_fetch_schedule", return_value=None):
            assert nhl_command._fetch_tracked_games(self._tracked("2026-09-29")) is None

    def test_poll_after_eastern_midnight_keeps_announcing_the_tracked_game(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, self._tracked("2026-09-29"))
        # By now the schedule's "today" is the 30th - the tracked game is
        # only in the 29th's slate.
        payload = make_schedule_payload({
            "2026-09-29": [make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")],
            "2026-09-30": [make_schedule_game(gid=9, home_id=30, away_id=40, game_state="FUT")],
        })
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, roster=[roster_spot(100, "Evan", "Bouchard")],
                       plays=[goal_play(event_owner_team_id=10, scoring_player_id=100)])

        with patch.object(nhl_command, "_fetch_schedule", return_value=payload), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp):
            all_ended = nhl_command._poll_once(bot, "#nhl.fi")

        assert "Evan Bouchard" in bot.send_message.call_args[0][1]
        assert all_ended is False
        # ...and the next day's game was NOT quietly adopted in its place.
        assert list(nhl_command._channels["#nhl.fi"]["games"]) == [1]

    def test_tracker_ends_when_its_own_slate_finishes_even_though_tomorrow_has_games(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, self._tracked("2026-09-29"))
        payload = make_schedule_payload({
            "2026-09-29": [make_schedule_game(gid=1, home_id=10, away_id=20, game_state="FINAL")],
            "2026-09-30": [make_schedule_game(gid=9, game_state="FUT")],
        })
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, home_score=6, away_score=5,
                       game_state="FINAL", last_period_type="OT")

        with patch.object(nhl_command, "_fetch_schedule", return_value=payload), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
             patch.object(nhl_command, "_fetch_attendance", return_value=18000):
            all_ended = nhl_command._poll_once(bot, "#nhl.fi")

        assert "FINAL:" in bot.send_message.call_args[0][1]
        assert all_ended is True  # not held open by the 30th's FUT game

    def test_a_game_added_late_to_the_tracked_day_is_still_picked_up_silently(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, self._tracked("2026-09-29"))
        payload = make_schedule_payload({"2026-09-29": [
            make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE"),
            make_schedule_game(gid=2, home_id=30, away_id=40, game_state="LIVE"),
        ]})
        pbp_by_id = {
            1: make_pbp(game_id=1, home_id=10, away_id=20),
            2: make_pbp(game_id=2, home_id=30, away_id=40, plays=[goal_play(event_owner_team_id=30, scoring_player_id=7)]),
        }

        with patch.object(nhl_command, "_fetch_schedule", return_value=payload), \
             patch.object(nhl_command, "_fetch_play_by_play", side_effect=lambda gid: pbp_by_id[gid]):
            nhl_command._poll_once(bot, "#nhl.fi")

        bot.send_message.assert_not_called()  # seeded as a baseline, not replayed
        assert nhl_command._channels["#nhl.fi"]["games"][2]["slate"] == "2026-09-29"
        assert nhl_command._channels["#nhl.fi"]["games"][2]["announced"] == {("score", 30, 1, 0): seeded(1)}


class TestGoalOrder:
    """Several goals seen in one poll must come out in the order they were
    scored, both teams interleaved. They used to be announced one team at a
    time (home's goals, then away's) - visible when a tracker recovered a
    stretch of a game and a 2-2 line landed before the 0-1 that came first.
    Home team id is 10, away 20 in make_pbp()."""

    def _pbp(self, plays):
        roster = [roster_spot(1, "Home", "First"), roster_spot(2, "Away", "Second"),
                  roster_spot(3, "Home", "Third"), roster_spot(4, "Away", "Fourth")]
        return make_pbp(game_id=1, home_id=10, away_id=20, roster=roster, plays=plays)

    def _plays(self):
        return [
            goal_play(event_id=1, event_owner_team_id=10, scoring_player_id=1, home_score=1, away_score=0),
            goal_play(event_id=2, event_owner_team_id=20, scoring_player_id=2, home_score=1, away_score=1),
            goal_play(event_id=3, event_owner_team_id=10, scoring_player_id=3, home_score=2, away_score=1),
            goal_play(event_id=4, event_owner_team_id=20, scoring_player_id=4, home_score=2, away_score=2),
        ]

    def _announced(self, nhl_command, pbp, announced):
        bot = MagicMock()
        nhl_command._announce_new_goals(bot, "#nhl.fi", pbp, announced)
        return [call.args[1] for call in bot.send_message.call_args_list]

    def test_goals_come_out_in_the_order_they_were_scored_across_both_teams(self, nhl_command):
        messages = self._announced(nhl_command, self._pbp(self._plays()), {})

        scorers = [next(name for name in ("First", "Second", "Third", "Fourth") if name in m) for m in messages]
        assert scorers == ["First", "Second", "Third", "Fourth"]  # not First, Third, Second, Fourth
        assert "1-0" in messages[0] and "1-1" in messages[1] and "2-1" in messages[2] and "2-2" in messages[3]

    def test_only_goals_not_announced_yet_are_new_and_still_ordered(self, nhl_command):
        # the first goal (home, 1-0) is already announced: new are Second (away), Third (home), Fourth (away)
        messages = self._announced(nhl_command, self._pbp(self._plays()), {("score", 10, 1, 0): seeded(1)})

        scorers = [next(name for name in ("First", "Second", "Third", "Fourth") if name in m) for m in messages]
        assert scorers == ["Second", "Third", "Fourth"]

    def test_nothing_new_announces_nothing(self, nhl_command):
        everything = nhl_command._resolve_goals(self._pbp(self._plays()), {})[2]
        assert self._announced(nhl_command, self._pbp(self._plays()), everything) == []

    def test_a_poll_announces_a_multi_goal_backlog_in_game_order(self, nhl_command):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        }}
        game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=self._pbp(self._plays())):
            nhl_command._poll_once(bot, "#nhl.fi")

        scores = [call.args[1].split("|")[0] for call in bot.send_message.call_args_list]
        assert ["1-0" in scores[0], "1-1" in scores[1], "2-1" in scores[2], "2-2" in scores[3]] == [True] * 4


class TestPlayByPlaySkipping:
    """Play-by-play is only fetched for games that can still change."""

    def _poll(self, nhl_command, prev, game_state, fail=False):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {1: prev}}
        game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state=game_state)
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, plays=[
            goal_play(event_owner_team_id=10, scoring_player_id=100)])
        with patch.object(nhl_command, "_fetch_today_games", return_value={1: game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp) as fetch:
            nhl_command._poll_once(bot, "#nhl.fi")
        return fetch, bot

    def _prev(self, ended=False, announced=None):
        return {"home_id": 10, "away_id": 20, "announced": announced or {}, "ended": ended}

    @pytest.mark.parametrize("state", ["FUT", "PRE"])
    def test_not_started_game_is_not_fetched(self, nhl_command, state):
        fetch, _ = self._poll(nhl_command, self._prev(), state)
        fetch.assert_not_called()

    def test_finished_and_already_announced_game_is_not_fetched_and_keeps_its_record(self, nhl_command):
        record = {("score", 10, 1, 0): seeded(1), ("score", 10, 2, 0): seeded(2), ("score", 10, 3, 0): seeded(3)}
        fetch, bot = self._poll(nhl_command, self._prev(ended=True, announced=dict(record)), "OFF")
        fetch.assert_not_called()
        bot.send_message.assert_not_called()
        assert nhl_command._channels["#nhl.fi"]["games"][1]["announced"] == record

    @pytest.mark.parametrize("state", ["LIVE", "CRIT"])
    def test_live_game_is_fetched(self, nhl_command, state):
        fetch, _ = self._poll(nhl_command, self._prev(), state)
        fetch.assert_called_once()

    def test_game_that_just_ended_is_still_fetched_for_last_goal_and_final(self, nhl_command):
        fetch, bot = self._poll(nhl_command, self._prev(ended=False), "OFF")
        fetch.assert_called_once()
        texts = [c.args[1] for c in bot.send_message.call_args_list]
        assert any("GOAL:" in t for t in texts) and any("FINAL:" in t for t in texts)

    def test_game_going_live_after_being_skipped_is_fetched_from_zero(self, nhl_command):
        fetch, bot = self._poll(nhl_command, self._prev(), "LIVE")
        fetch.assert_called_once()
        assert any("GOAL:" in c.args[1] for c in bot.send_message.call_args_list)


class TestAnnouncedGoalRecord:
    """Goals are remembered by (team, running score) plus event id, not by a
    count per team (issue #20). Each case below was seen in a real feed."""

    HOME, AWAY = 10, 20

    def _pbp(self, *plays):
        roster = [roster_spot(i, f"P{i}", "X") for i in range(1, 9)]
        return make_pbp(game_id=1, home_id=self.HOME, away_id=self.AWAY, plays=list(plays), roster=roster)

    def _goal(self, event_id, team, home, away, scorer=1, **kw):
        return goal_play(event_id=event_id, event_owner_team_id=team, scoring_player_id=scorer,
                         home_score=home, away_score=away, **kw)

    def _poll_sequence(self, nhl_command, feeds, all_lines=False):
        """Runs one poll per feed and returns every GOAL: line sent (every line
        sent with all_lines)."""
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": self.HOME, "away_id": self.AWAY, "announced": {}, "ended": False},
        }}
        game = make_schedule_game(gid=1, home_id=self.HOME, away_id=self.AWAY, game_state="LIVE")
        for feed in feeds:
            # _fetch_tracked_games, not _fetch_today_games: after the first poll the
            # stored games carry their slate date and the schedule is fetched by it.
            with patch.object(nhl_command, "_fetch_tracked_games", return_value={1: game}), \
                 patch.object(nhl_command, "_fetch_play_by_play", return_value=feed) as fetch:
                nhl_command._poll_once(bot, "#nhl.fi")
            assert fetch.call_count == 1  # every poll in the sequence really ran
        lines = [c.args[1] for c in bot.send_message.call_args_list]
        if all_lines:
            return lines
        return [line for line in lines if "GOAL:" in line and "NO GOAL:" not in line]

    # -- a goal that vanishes for a poll and comes back (same id) ------------

    def test_goal_that_disappears_for_a_poll_and_returns_is_announced_once(self, nhl_command):
        goal = self._goal(155, self.AWAY, 0, 1, scorer=1, assist1=2, assist2=3)
        lines = self._poll_sequence(nhl_command, [self._pbp(goal), self._pbp(), self._pbp(goal)])
        assert len(lines) == 1

    def test_the_same_with_different_assists_on_the_way_back(self, nhl_command):
        first = self._goal(155, self.AWAY, 0, 1, scorer=1, assist1=2, assist2=3)
        back = self._goal(155, self.AWAY, 0, 1, scorer=1, assist1=4)
        lines = self._poll_sequence(nhl_command, [self._pbp(first), self._pbp(), self._pbp(back)])
        assert len(lines) == 1

    # -- the same goal listed under two ids ----------------------------------

    def test_two_plays_for_one_goal_in_the_same_poll_are_announced_once_the_first_listed(self, nhl_command):
        a = self._goal(278, self.AWAY, 0, 2, scorer=5)
        b = self._goal(279, self.AWAY, 0, 2, scorer=6)
        lines = self._poll_sequence(nhl_command, [self._pbp(a, b)])
        assert len(lines) == 1 and "P5" in lines[0]

    def test_a_duplicate_listed_before_the_announced_play_is_ignored(self, nhl_command):
        # the real sequence: play 279 announced, then 278 appears *before* it in the list
        later, earlier = self._goal(279, self.AWAY, 0, 2, scorer=6), self._goal(278, self.AWAY, 0, 2, scorer=5)
        lines = self._poll_sequence(nhl_command, [self._pbp(later), self._pbp(earlier, later)])
        assert len(lines) == 1 and "P6" in lines[0]  # the old behaviour announced 279 a second time

    def test_a_duplicate_listed_after_the_announced_play_is_ignored(self, nhl_command):
        first, second = self._goal(278, self.AWAY, 0, 2, scorer=5), self._goal(279, self.AWAY, 0, 2, scorer=6)
        lines = self._poll_sequence(nhl_command, [self._pbp(first), self._pbp(first, second)])
        assert len(lines) == 1

    def test_when_the_feed_drops_the_duplicate_nothing_is_announced(self, nhl_command):
        a, b = self._goal(278, self.AWAY, 0, 2, scorer=5), self._goal(279, self.AWAY, 0, 2, scorer=6)
        lines = self._poll_sequence(nhl_command, [self._pbp(b), self._pbp(a, b), self._pbp(a)])
        assert len(lines) == 1

    # -- a removed goal followed by a real one with the same running score ----

    def test_goal_after_a_disallowed_one_with_the_same_running_score_is_announced(self, nhl_command):
        disallowed = self._goal(1, self.AWAY, 0, 2, scorer=1)   # announced, then removed
        real = self._goal(2, self.AWAY, 0, 2, scorer=2)         # the team's next goal: same running score
        lines = self._poll_sequence(nhl_command, [self._pbp(disallowed), self._pbp(), self._pbp(real)])
        assert len(lines) == 2

    def test_a_removal_and_the_next_goal_in_the_same_poll_is_not_swallowed(self, nhl_command):
        disallowed = self._goal(1, self.AWAY, 0, 1, scorer=1)
        real = self._goal(2, self.AWAY, 0, 1, scorer=2)
        lines = self._poll_sequence(nhl_command, [self._pbp(disallowed), self._pbp(real)])
        assert len(lines) == 2  # the old per-team count saw 1 -> 1 and announced nothing

    # -- identity edge cases ---------------------------------------------------

    def test_a_goal_whose_running_score_was_renumbered_but_whose_id_is_known_is_not_repeated(self, nhl_command):
        before = self._goal(7, self.AWAY, 0, 3)
        renumbered = self._goal(7, self.AWAY, 0, 2)
        lines = self._poll_sequence(nhl_command, [self._pbp(before), self._pbp(renumbered)])
        assert len(lines) == 1

    def test_shootout_goals_of_one_team_are_each_announced(self, nhl_command):
        # confirmed live: every shootout goal carries the same running score and clock 00:00
        so1 = self._goal(1253, self.HOME, 2, 2, scorer=1, period_number=5, period_type="SO", time_in_period="00:00")
        so2 = self._goal(1258, self.HOME, 2, 2, scorer=2, period_number=5, period_type="SO", time_in_period="00:00")
        lines = self._poll_sequence(nhl_command, [self._pbp(so1), self._pbp(so1, so2)])
        assert len(lines) == 2

    def test_missing_running_score_falls_back_to_the_event_id(self, nhl_command):
        one = self._goal(1, self.AWAY, None, None)
        two = self._goal(2, self.AWAY, None, None, scorer=2)
        lines = self._poll_sequence(nhl_command, [self._pbp(one), self._pbp(one, two), self._pbp(one, two)])
        assert len(lines) == 2

    def test_missing_running_score_and_id_falls_back_to_period_and_clock(self, nhl_command):
        a = self._goal(None, self.AWAY, None, None, time_in_period="05:00")
        b = self._goal(None, self.AWAY, None, None, scorer=2, time_in_period="09:00")
        del a["eventId"], b["eventId"]
        lines = self._poll_sequence(nhl_command, [self._pbp(a), self._pbp(a, b), self._pbp(a, b)])
        assert len(lines) == 2

    def test_goals_of_both_teams_with_the_same_running_score_digits_are_distinct(self, nhl_command):
        home = self._goal(1, self.HOME, 1, 0)
        away = self._goal(2, self.AWAY, 1, 1)
        assert len(self._poll_sequence(nhl_command, [self._pbp(home, away)])) == 2

    # -- record handling --------------------------------------------------------

    def test_the_stored_record_is_not_mutated_in_place(self, nhl_command):
        goal = self._goal(1, self.AWAY, 0, 1)
        stored = {}
        nhl_command._resolve_goals(self._pbp(goal), stored)
        assert stored == {}

    def test_seeding_records_both_ids_of_a_doubly_listed_goal(self, nhl_command):
        a, b = self._goal(278, self.AWAY, 0, 2), self._goal(279, self.AWAY, 0, 2)
        record = nhl_command._resolve_goals(self._pbp(a, b), {}, seed=True)[2]
        assert record == {("score", self.AWAY, 0, 2): seeded(278, 279)}

    def test_a_goal_already_in_the_feed_when_tracking_started_is_never_announced(self, nhl_command):
        goal = self._goal(1, self.AWAY, 0, 1)
        record = nhl_command._resolve_goals(self._pbp(goal), {}, seed=True)[2]
        assert nhl_command._resolve_goals(self._pbp(goal), record)[0] == []


def stoppage_play(reason, time_in_period, period_number=1):
    return {"typeDescKey": "stoppage", "periodDescriptor": {"number": period_number, "periodType": "REG"},
            "timeInPeriod": time_in_period, "details": {"reason": reason, "secondaryReason": reason}}


def penalty_play(desc_key, time_in_period, period_number=1):
    return {"typeDescKey": "penalty", "periodDescriptor": {"number": period_number, "periodType": "REG"},
            "timeInPeriod": time_in_period, "details": {"descKey": desc_key, "duration": 2}}


class TestRetraction:
    """A goal that was announced and then stays out of the feed is retracted
    (issue #13, stage 1). Cases are the real ones seen over three nights."""

    HOME, AWAY = 10, 20

    def _pbp(self, *plays):
        roster = [roster_spot(i, f"P{i}", "X") for i in range(1, 9)]
        return make_pbp(game_id=1, home_id=self.HOME, away_id=self.AWAY, plays=list(plays), roster=roster)

    def _quiet(self, *plays):
        """A feed with other plays in it but none of the goals under test."""
        return self._pbp({"typeDescKey": "faceoff", "eventId": 900, "periodDescriptor": {"number": 3, "periodType": "REG"},
                          "timeInPeriod": "01:00", "details": {}}, *plays)

    def _goal(self, event_id=1, team=None, home=0, away=1, scorer=1, **kw):
        kw.setdefault("time_in_period", "05:25")
        kw.setdefault("period_number", 3)
        return goal_play(event_id=event_id, event_owner_team_id=team or self.AWAY, scoring_player_id=scorer,
                         home_score=home, away_score=away, **kw)

    def _run(self, nhl_command, feeds, seeded_record=None):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": self.HOME, "away_id": self.AWAY, "announced": seeded_record or {}, "ended": False},
        }}
        game = make_schedule_game(gid=1, home_id=self.HOME, away_id=self.AWAY, game_state="LIVE")
        for feed in feeds:
            with patch.object(nhl_command, "_fetch_tracked_games", return_value={1: game}), \
                 patch.object(nhl_command, "_fetch_play_by_play", return_value=feed):
                nhl_command._poll_once(bot, "#nhl.fi")
        return [c.args[1] for c in bot.send_message.call_args_list]

    def _retractions(self, lines):
        return [line for line in lines if "NO GOAL:" in line]

    def _goals(self, lines):
        return [line for line in lines if "GOAL:" in line and "NO GOAL:" not in line]

    # -- when -----------------------------------------------------------------

    def test_a_goal_gone_for_three_polls_is_not_retracted_and_not_repeated_when_it_returns(self, nhl_command):
        goal = self._goal()
        lines = self._run(nhl_command, [self._pbp(goal), self._quiet(), self._quiet(), self._quiet(), self._pbp(goal)])
        assert self._retractions(lines) == [] and len(self._goals(lines)) == 1

    def test_the_miss_counter_starts_over_when_the_goal_returns(self, nhl_command):
        goal = self._goal()
        gap = [self._quiet()] * 3
        lines = self._run(nhl_command, [self._pbp(goal)] + gap + [self._pbp(goal)] + gap)
        assert self._retractions(lines) == []  # 3 + 3 missed polls, never 4 in a row

    def test_a_goal_gone_for_four_polls_is_retracted_once(self, nhl_command):
        goal = self._goal()
        lines = self._run(nhl_command, [self._pbp(goal)] + [self._quiet()] * 7)
        assert len(self._retractions(lines)) == 1

    def test_the_retraction_names_the_goal_and_the_current_score(self, nhl_command):
        goal = self._goal(scorer=1)
        lines = self._run(nhl_command, [self._pbp(goal)] + [self._quiet()] * 4)
        text = self._retractions(lines)[0]
        assert "NO GOAL:" in text
        assert "Carolina Hurricanes 0-0 Florida Panthers" in text
        assert "the 05:25 3rd goal by P1 X (Florida Panthers) was disallowed" in text
        assert "(" not in text.split("disallowed", 1)[1]  # no explanation without a stoppage

    def test_a_goal_that_returns_after_being_retracted_is_announced_again(self, nhl_command):
        goal = self._goal()
        lines = self._run(nhl_command, [self._pbp(goal)] + [self._quiet()] * 4 + [self._pbp(goal)])
        assert len(self._retractions(lines)) == 1 and len(self._goals(lines)) == 2

    def test_fetch_failures_do_not_count_as_missing(self, nhl_command):
        goal = self._goal()
        lines = self._run(nhl_command, [self._pbp(goal), self._quiet(), self._quiet(), None, None, None, None, self._quiet()])
        assert self._retractions(lines) == []  # only three real misses

    def test_goals_already_in_the_feed_when_tracking_started_are_never_retracted(self, nhl_command):
        record = {("score", self.AWAY, 0, 1): seeded(1)}
        assert self._retractions(self._run(nhl_command, [self._quiet()] * 8, seeded_record=record)) == []

    def test_the_resolver_reports_nothing_gone_for_goals_that_were_only_seeded(self, nhl_command):
        record = {("score", self.AWAY, 0, 1): seeded(1)}
        for _ in range(8):
            _, gone, record = nhl_command._resolve_goals(self._quiet(), record)
            assert gone == []

    def test_a_renumbered_goal_is_not_mistaken_for_a_missing_one(self, nhl_command):
        before, after = self._goal(7, away=3), self._goal(7, away=2)
        lines = self._run(nhl_command, [self._pbp(before)] + [self._pbp(after)] * 6)
        assert self._retractions(lines) == [] and len(self._goals(lines)) == 1

    # -- replaced by the next goal -----------------------------------------------

    def test_a_goal_replaced_at_once_is_announced_without_calling_the_old_one_disallowed(self, nhl_command):
        # real case: a first entry by player A, removed 20 s later, and 20 s after that the same goal
        # (same team, same running score) under player B: a correction, not a disallowed goal
        old, new = self._goal(1, scorer=1), self._goal(2, scorer=2)
        lines = self._run(nhl_command, [self._pbp(old), self._pbp(new)])
        assert len(lines) == 2 and "P1 X" in lines[0] and "P2 X" in lines[1]
        assert self._retractions(lines) == []

    def test_a_replacement_after_a_miss_or_two_is_still_not_a_retraction(self, nhl_command):
        old, new = self._goal(1, scorer=1), self._goal(2, scorer=2)
        lines = self._run(nhl_command, [self._pbp(old), self._quiet(), self._quiet(), self._pbp(new)])
        assert self._retractions(lines) == [] and len(self._goals(lines)) == 2

    def test_a_goal_after_one_that_was_retracted_on_its_own_is_announced_and_nothing_else_is_said(self, nhl_command):
        disallowed, next_goal = self._goal(1, scorer=1), self._goal(2, scorer=2, time_in_period="15:00")
        feeds = [self._pbp(disallowed)] + [self._quiet()] * 4 + [self._pbp(next_goal)]
        lines = self._run(nhl_command, feeds)
        assert len(self._retractions(lines)) == 1 and len(self._goals(lines)) == 2  # one retraction, from the misses

    def test_two_goals_gone_at_once_while_another_goal_is_still_there_give_two_retractions(self, nhl_command):
        a, b = self._goal(1, away=1), self._goal(2, home=1, away=1, team=self.HOME, scorer=2, time_in_period="08:00")
        keep = self._goal(3, home=1, away=2, scorer=3, time_in_period="12:00")
        lines = self._run(nhl_command, [self._pbp(a, b, keep)] + [self._quiet(keep)] * 4)
        assert len(self._retractions(lines)) == 2

    # -- a broken feed is not a run of disallowed goals ---------------------------------

    def test_an_empty_feed_does_not_retract_anything(self, nhl_command, capsys):
        a, b = self._goal(1, away=1), self._goal(2, home=1, away=1, team=self.HOME, scorer=2, time_in_period="08:00")
        lines = self._run(nhl_command, [self._pbp(a, b)] + [self._pbp()] * 8)
        assert self._retractions(lines) == []
        assert "the feed shows no goal for 2 announced one(s), not counting this poll as a miss" in capsys.readouterr().out

    def test_an_empty_feed_does_not_retract_a_single_goal_either(self, nhl_command):
        assert self._retractions(self._run(nhl_command, [self._pbp(self._goal())] + [self._pbp()] * 8)) == []

    def test_two_goals_vanishing_together_with_no_goal_left_are_not_retracted(self, nhl_command):
        a, b = self._goal(1, away=1), self._goal(2, home=1, away=1, team=self.HOME, scorer=2, time_in_period="08:00")
        lines = self._run(nhl_command, [self._pbp(a, b)] + [self._quiet()] * 8 + [self._pbp(a, b)])
        assert self._retractions(lines) == [] and len(self._goals(lines)) == 2  # nothing repeated when they return

    def test_polls_skipped_as_broken_do_not_reset_the_count(self, nhl_command):
        goal = self._goal()
        feeds = [self._pbp(goal)] + [self._quiet()] * 2 + [self._pbp()] * 3 + [self._quiet()] * 2
        assert len(self._retractions(self._run(nhl_command, feeds))) == 1  # 2 + 2 real misses

    def test_the_label_of_an_overtime_goal(self, nhl_command):
        goal = self._goal(period_number=4, period_type="OT", time_in_period="03:18")
        text = self._retractions(self._run(nhl_command, [self._pbp(goal)] + [self._quiet()] * 4))[0]
        assert "the 03:18 OT goal" in text

    # -- the score shown ------------------------------------------------------------

    def test_the_score_is_the_running_score_of_the_last_remaining_goal(self, nhl_command):
        first = self._goal(1, home=1, away=0, team=self.HOME, scorer=2, time_in_period="03:00")
        removed = self._goal(2, home=1, away=1, scorer=1)
        later = self._goal(3, home=2, away=1, team=self.HOME, scorer=3, time_in_period="09:00")
        feeds = [self._pbp(first, removed)] + [self._quiet(first)] * 4
        assert "Carolina Hurricanes 1-0 Florida Panthers" in self._retractions(self._run(nhl_command, feeds))[0]
        # a later goal is still there when the earlier one is retracted: the score shown is that goal's
        feeds = [self._pbp(first, removed, later)] + [self._pbp(first, later)] * 4
        assert "Carolina Hurricanes 2-1 Florida Panthers" in self._retractions(self._run(nhl_command, feeds))[0]

    def test_without_a_running_score_the_header_score_is_used(self, nhl_command):
        goal = self._goal()
        other = self._goal(2, home=None, away=None, scorer=2, time_in_period="08:00")
        pbp = self._pbp(other)
        pbp["homeTeam"]["score"], pbp["awayTeam"]["score"] = 4, 5
        feeds = [self._pbp(goal, other)] + [pbp] * 4
        assert "Carolina Hurricanes 4-5 Florida Panthers" in self._retractions(self._run(nhl_command, feeds))[0]

    # -- the explanation ----------------------------------------------------------------

    def _reason_text(self, nhl_command, *extra_plays):
        goal = self._goal(time_in_period="05:25", period_number=3)
        return self._retractions(self._run(nhl_command, [self._pbp(goal)] + [self._pbp(*extra_plays)] * 4))[0]

    def test_offside_challenge_even_half_a_minute_before_the_goals_clock(self, nhl_command):
        # real case: the stoppage sat at 04:54, the goal at 05:25
        text = self._reason_text(nhl_command, stoppage_play("chlg-vis-off-side", "04:54", 3))
        assert text.endswith("was disallowed (offside challenge)")

    def test_goaltender_interference_challenge_just_after_the_goals_clock(self, nhl_command):
        text = self._reason_text(nhl_command, stoppage_play("chlg-hm-goal-interference", "05:27", 3))
        assert text.endswith("(goaltender interference challenge)")

    def test_an_unknown_challenge_reason_is_just_a_challenge(self, nhl_command):
        text = self._reason_text(nhl_command, stoppage_play("chlg-hm-something-new", "05:25", 3))
        assert text.endswith("(challenge)")

    def test_a_stoppage_that_is_not_a_challenge_explains_nothing(self, nhl_command):
        assert not self._reason_text(nhl_command, stoppage_play("icing", "05:25", 3)).endswith(")")

    def test_a_challenge_too_far_from_the_goal_or_in_another_period_explains_nothing(self, nhl_command):
        assert not self._reason_text(nhl_command, stoppage_play("chlg-hm-off-side", "03:00", 3)).endswith(")")
        assert not self._reason_text(nhl_command, stoppage_play("chlg-hm-off-side", "05:00", 2)).endswith(")")
        assert not self._reason_text(nhl_command, stoppage_play("chlg-hm-off-side", "05:40", 3)).endswith(")")

    def test_a_failed_challenge_is_not_the_reason(self, nhl_command):
        # a failed challenge costs the challenging team a bench penalty and the goal stands
        text = self._reason_text(nhl_command, stoppage_play("chlg-vis-off-side", "05:25", 3),
                                 penalty_play("delaying-game-unsuccessful-challenge", "05:25", 3))
        assert not text.endswith(")")

    def test_the_nearest_challenge_wins(self, nhl_command):
        text = self._reason_text(nhl_command, stoppage_play("chlg-hm-goal-interference", "04:40", 3),
                                 stoppage_play("chlg-hm-off-side", "05:20", 3))
        assert text.endswith("(offside challenge)")

    def test_a_goal_with_an_unreadable_clock_gets_no_explanation(self, nhl_command):
        goal = self._goal(time_in_period="")
        text = self._retractions(self._run(nhl_command, [self._pbp(goal)] + [self._pbp(
            stoppage_play("chlg-hm-off-side", "05:25", 3))] * 4))[0]
        assert not text.endswith(")")

    # -- what is kept for the corrections that build on this -------------------------------

    def test_the_posted_scorer_assists_and_label_are_stored_with_the_goal(self, nhl_command):
        goal = self._goal(scorer=1, assist1=2, assist2=3)
        self._run(nhl_command, [self._pbp(goal)])
        record = nhl_command._channels["#nhl.fi"]["games"][1]["announced"][("score", self.AWAY, 0, 1)]
        assert record["posted"] == {"team": "Florida Panthers", "scorer": 1, "scorer_name": "P1 X",
                                    "assists": [2, 3], "period": 3, "clock": "05:25", "label": "05:25 3rd"}
        assert record["ids"] == [1] and record["missing"] == 0 and record["retracted"] is False


class TestJournalLinesForIgnoredRepeats:
    """A line in the bot's log whenever a repeated goal is ignored, so the server journal shows the fix acting."""

    def _pbp(self, *plays):
        return make_pbp(game_id=77, home_id=10, away_id=20, plays=list(plays))

    def _goal(self, event_id, home=0, away=1, **kw):
        return goal_play(event_id=event_id, event_owner_team_id=20, scoring_player_id=1, home_score=home, away_score=away, **kw)

    def test_a_goal_that_comes_back_after_missed_polls_is_logged(self, nhl_command, capsys):
        first = nhl_command._resolve_goals(self._pbp(self._goal(1)), {})[2]
        quiet = self._pbp({"typeDescKey": "faceoff", "eventId": 900, "periodDescriptor": {"number": 1, "periodType": "REG"},
                           "timeInPeriod": "01:00", "details": {}})
        for _ in range(2):
            _, _, first = nhl_command._resolve_goals(quiet, first)
        capsys.readouterr()
        nhl_command._resolve_goals(self._pbp(self._goal(1)), first)
        assert "NHL: game 77: goal (team 20, 0-1) is back after 2 missed poll(s), not announced again" in capsys.readouterr().out

    def test_a_second_play_for_an_announced_goal_is_logged(self, nhl_command, capsys):
        record = nhl_command._resolve_goals(self._pbp(self._goal(279)), {})[2]
        capsys.readouterr()
        nhl_command._resolve_goals(self._pbp(self._goal(278), self._goal(279)), record)
        assert "NHL: game 77: goal (team 20, 0-1) listed again as event [278], not announced again" in capsys.readouterr().out

    def test_a_renumbered_goal_is_logged(self, nhl_command, capsys):
        record = nhl_command._resolve_goals(self._pbp(self._goal(7, away=3)), {})[2]
        capsys.readouterr()
        nhl_command._resolve_goals(self._pbp(self._goal(7, away=2)), record)
        assert "is now (team 20, 0-2), not announced again" in capsys.readouterr().out

    def test_ordinary_polls_and_seeding_log_nothing(self, nhl_command, capsys):
        goal = self._goal(1)
        record = nhl_command._resolve_goals(self._pbp(goal), {}, seed=True)[2]
        nhl_command._resolve_goals(self._pbp(goal), record)
        nhl_command._resolve_goals(self._pbp(goal, self._goal(2, away=2)), record)
        assert capsys.readouterr().out == ""

    def test_seeding_a_feed_with_a_doubly_listed_goal_logs_nothing(self, nhl_command, capsys):
        nhl_command._resolve_goals(self._pbp(self._goal(278), self._goal(279)), {}, seed=True)
        assert capsys.readouterr().out == ""

    def test_the_key_text_of_a_shootout_goal(self, nhl_command):
        assert nhl_command._key_text(("id", 1253)) == "(id, 1253)"


class TestFinalScoreFromScoreEndpoint:
    """Issue #12: the play-by-play header can lag the plays at the end of a
    game (posted 2-2 and no OT for a 3-2 overtime win); the score endpoint
    already had the right result, so the FINAL: line reads it."""

    GID = 2026010026
    SLATE = "2026-10-02"

    def _official(self, home, away, last_period="OT", state="OVER", gid=GID):
        game = {"id": gid, "gameDate": self.SLATE, "gameState": state,
                "homeTeam": {"abbrev": "CAR", "score": home}, "awayTeam": {"abbrev": "FLA", "score": away}}
        if last_period:
            game["gameOutcome"] = {"lastPeriodType": last_period}
        return game

    def _final(self, nhl_command, pbp, scores, slate=SLATE):
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_scores", return_value=scores), \
                patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", pbp, slate)
        return bot.send_message.call_args[0][1]

    def test_stale_header_is_replaced_by_the_official_score_and_ot_mark(self, nhl_command):
        stale = make_pbp(home_score=2, away_score=2, last_period_type=None)  # the real shape of the incident
        message = self._final(nhl_command, stale, {self.GID: self._official(3, 2)})
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers (OT)"

    def test_shootout_winner_comes_from_the_official_score(self, nhl_command):
        stale = make_pbp(home_score=2, away_score=2, last_period_type=None)
        message = self._final(nhl_command, stale, {self.GID: self._official(3, 2, last_period="SO")})
        assert message.endswith("Carolina Hurricanes 3-2 Florida Panthers (SO)")

    def test_a_regulation_game_has_no_suffix(self, nhl_command):
        message = self._final(nhl_command, make_pbp(home_score=5, away_score=4),
                              {self.GID: self._official(5, 4, last_period="REG")})
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 5-4 Florida Panthers"

    @pytest.mark.parametrize("scores", [
        None,                                            # request failed
        {},                                              # game not listed
        {GID + 1: {"id": GID + 1}},                      # another game only
        {GID: {"id": GID, "gameState": "OVER"}},         # no numeric score
    ])
    def test_falls_back_to_the_header_when_the_official_result_is_unavailable(self, nhl_command, scores):
        pbp = make_pbp(home_score=3, away_score=2, last_period_type="OT")
        message = self._final(nhl_command, pbp, scores)
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers (OT)"

    def test_an_endpoint_game_without_game_outcome_keeps_the_headers_ot_mark(self, nhl_command, capsys):
        """Issue #27: no gameOutcome is not a statement that it was regulation."""
        pbp = make_pbp(game_id=self.GID, home_score=3, away_score=2, last_period_type="OT")
        message = self._final(nhl_command, pbp, {self.GID: self._official(3, 2, last_period=None)})
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers (OT)"
        assert capsys.readouterr().out == ""

    def test_the_endpoint_score_is_still_used_when_it_has_no_game_outcome(self, nhl_command):
        stale = make_pbp(game_id=self.GID, home_score=2, away_score=2, last_period_type=None)
        message = self._final(nhl_command, stale, {self.GID: self._official(3, 2, last_period=None)})
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers"

    def test_a_header_that_disagrees_is_logged_with_both_values(self, nhl_command, capsys):
        stale = make_pbp(game_id=self.GID, home_score=2, away_score=2, last_period_type=None)
        self._final(nhl_command, stale, {self.GID: self._official(3, 2)})
        assert (f"NHL: game {self.GID}: the play-by-play header says 2-2 REG at FINAL, "
                "the score endpoint says 3-2 OT; using the endpoint's") in capsys.readouterr().out

    def test_a_header_that_agrees_logs_nothing(self, nhl_command, capsys):
        for header_type, official_type in ((None, "REG"), ("OT", "OT")):
            pbp = make_pbp(game_id=self.GID, home_score=3, away_score=2, last_period_type=header_type)
            self._final(nhl_command, pbp, {self.GID: self._official(3, 2, last_period=official_type)})
        assert capsys.readouterr().out == ""

    def test_a_missing_official_result_is_logged(self, nhl_command, capsys):
        self._final(nhl_command, make_pbp(game_id=self.GID, home_score=3, away_score=2), {})
        assert (f"NHL: game {self.GID}: no result from the score endpoint for the FINAL line, "
                "using the play-by-play header") in capsys.readouterr().out

    def test_without_a_slate_nothing_is_logged(self, nhl_command, capsys):
        nhl_command._announce_end(MagicMock(), "#nhl.fi", make_pbp(home_score=3, away_score=2))
        assert "score endpoint" not in capsys.readouterr().out

    def test_without_a_slate_no_request_is_made(self, nhl_command):
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_scores") as fetch, \
                patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", make_pbp(home_score=3, away_score=2))
        fetch.assert_not_called()
        assert "3-2" in bot.send_message.call_args[0][1]

    def test_a_cached_copy_is_never_used(self, nhl_command):
        """!nhl now may have cached the pre-goal board a few seconds ago."""
        old = {"games": [{**self._official(2, 2, last_period=None, state="LIVE")}]}
        new = {"games": [self._official(3, 2)]}
        nhl_command.session.get.side_effect = [make_response(old), make_response(new)]
        assert nhl_command._fetch_scores(self.SLATE)[self.GID]["homeTeam"]["score"] == 2
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", make_pbp(home_score=2, away_score=2), self.SLATE)
        assert "3-2" in bot.send_message.call_args[0][1]

    def test_the_poll_looks_the_result_up_on_the_games_own_slate(self, nhl_command):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False, "slate": self.SLATE},
        }}
        game = make_schedule_game(gid=1, game_state="FINAL", home_score=2, away_score=2)
        with patch.object(nhl_command, "_fetch_tracked_games", return_value={1: game}), \
                patch.object(nhl_command, "_fetch_play_by_play", return_value=make_pbp(game_id=1, home_score=2, away_score=2)), \
                patch.object(nhl_command, "_fetch_scores", return_value={1: self._official(3, 2, gid=1)}) as fetch, \
                patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._poll_once(bot, "#nhl.fi")
        fetch.assert_called_once_with(self.SLATE)
        assert "3-2" in bot.send_message.call_args[0][1]


class TestAttendanceSummary:
    """Issue #8: the game report's attendance figure shows up 1.5-3 minutes
    after the final horn in about two games of three, so a FINAL: line often
    goes out without it. A missing figure is retried for a few minutes, and
    one results list with every figure follows when the slate is over."""

    SLATE = "2026-10-05"

    def _tracker(self, nhl_command, ended=(False, False)):
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            gid: {"home_id": 10, "away_id": 20, "announced": {}, "ended": was_ended, "slate": self.SLATE}
            for gid, was_ended in zip((1, 2), ended)
        }}

    def _poll(self, nhl_command, bot, states, attendance, pbps=None):
        """One poll; `attendance` answers _fetch_attendance by game id."""
        games = {gid: make_schedule_game(gid=gid, game_state=state, home_score=3, away_score=2)
                 for gid, state in states.items()}
        pbps = pbps or {gid: make_pbp(game_id=gid, home_score=3, away_score=2, game_state="FINAL",
                                      season=20262027, home_abbrev=f"H{gid}", away_abbrev=f"A{gid}",
                                      last_period_type="OT" if gid == 2 else None)
                        for gid in states}
        for gid, pbp in pbps.items():
            pbp["startTimeUTC"] = f"2026-10-05T23:{gid:02d}:00Z"
        with patch.object(nhl_command, "_fetch_tracked_games", return_value=games), \
                patch.object(nhl_command, "_fetch_play_by_play", side_effect=lambda gid: pbps[gid]), \
                patch.object(nhl_command, "_fetch_attendance", side_effect=lambda pbp: attendance.get(pbp["id"])):
            return nhl_command._poll_once(bot, "#nhl.fi")

    def _sent(self, bot):
        return [c[0][1] for c in bot.send_message.call_args_list]

    def test_a_late_figure_is_found_by_a_later_poll_and_the_slate_summary_has_it(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command, ended=(False, True))
        nhl_command._channels["#nhl.fi"]["games"][2]["final"] = {
            "text": "H2 3-2 A2 (OT)", "attendance": 17100, "late": False,
            "report": {"season": 20262027, "id": 2}, "start": "2026-10-05T23:30:00Z", "at": time.monotonic(),
        }
        # the last game ends without its figure: the tracker stays open
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {}) is False
        assert [m for m in self._sent(bot) if "NHL results" in m] == []
        # 30 s later the report has it: summary goes out and the tracker may stop
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {1: 18347}) is True
        assert self._sent(bot)[-1] == "NHL results 6.10.: H1 3-2 A1 (18347), H2 3-2 A2 (OT) (17100)"

    def test_figures_that_were_on_every_final_line_get_no_summary(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {1: 18000, 2: 17000}) is True
        assert not [m for m in self._sent(bot) if "NHL results" in m]

    def test_a_figure_that_never_arrives_is_left_out_after_the_retry_window(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {2: 17000}) is False  # game 1 still pending
        for snap in nhl_command._channels["#nhl.fi"]["games"].values():
            snap["final"]["at"] -= nhl_command.ATTENDANCE_RETRY_SECONDS + 1
        nhl_command._channels["#nhl.fi"]["games"][2]["final"]["late"] = True  # 2's figure arrived after its line
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {}) is True
        assert self._sent(bot)[-1] == "NHL results 6.10.: H1 3-2 A1, H2 3-2 A2 (OT) (17000)"

    def test_retrying_stops_after_the_window(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {})
        for snap in nhl_command._channels["#nhl.fi"]["games"].values():
            snap["final"]["at"] -= nhl_command.ATTENDANCE_RETRY_SECONDS + 1
        with patch.object(nhl_command, "_fetch_attendance") as fetch:
            assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {}) is True
        fetch.assert_not_called()

    def test_games_not_announced_by_the_tracker_are_not_in_the_summary(self, nhl_command):
        """A game that was already over when tracking began has no FINAL: record."""
        bot = MagicMock()
        self._tracker(nhl_command, ended=(True, False))
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {}) is False
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {2: 17000}) is True
        assert self._sent(bot)[-1] == "NHL results 6.10.: H2 3-2 A2 (OT) (17000)"

    def test_a_long_slate_is_split_into_several_lines(self, nhl_command):
        bot = MagicMock()
        state = {}
        for gid in range(1, 17):
            state[gid] = {"home_id": 10, "away_id": 20, "announced": {}, "ended": True, "slate": self.SLATE,
                          "final": {"text": f"TEAM{gid} 3-2 OTHER{gid}", "attendance": 17000 + gid, "late": True,
                                    "report": {}, "start": f"2026-10-05T23:{gid:02d}:00Z", "at": time.monotonic()}}
        nhl_command._announce_slate_summary(bot, "#nhl.fi", state)
        messages = self._sent(bot)
        assert len(messages) > 1
        assert all(m.startswith("NHL results 6.10.: ") and len(m.encode()) <= nhl_command.MAX_LINE_BYTES for m in messages)
        assert "TEAM1 3-2 OTHER1 (17001)" in messages[0] and "OTHER16 (17016)" in messages[-1]

    def test_the_summary_lists_games_by_start_time(self, nhl_command):
        bot = MagicMock()

        def snap(text, start):
            return {"slate": self.SLATE, "final": {"text": text, "attendance": 17000, "late": True,
                                                   "report": {}, "start": start, "at": time.monotonic()}}
        state = {1: snap("LATE 1-0 GAME", "2026-10-06T00:00:00Z"), 2: snap("EARLY 2-1 GAME", "2026-10-05T23:00:00Z")}
        nhl_command._announce_slate_summary(bot, "#nhl.fi", state)
        assert self._sent(bot)[-1] == "NHL results 6.10.: EARLY 2-1 GAME (17000), LATE 1-0 GAME (17000)"

    def test_a_failure_in_the_follow_up_never_stops_the_poll(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        with patch.object(nhl_command, "_retry_missing_attendance", side_effect=RuntimeError("boom")):
            assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {1: 18000, 2: 17000}) is True
        assert any("FINAL:" in m for m in self._sent(bot))


class TestRenumberedGoals:
    """Issue #25: a disallowed goal renumbers the same team's later goals into
    its place. The later goal is not announced again, and it is the goal that
    is gone that gets retracted."""

    HOME, AWAY = TestRetraction.HOME, TestRetraction.AWAY
    _pbp, _quiet, _goal, _run = TestRetraction._pbp, TestRetraction._quiet, TestRetraction._goal, TestRetraction._run
    _retractions, _goals = TestRetraction._retractions, TestRetraction._goals

    def _scenario(self):
        first = self._goal(1, team=self.HOME, home=1, away=0, scorer=1, time_in_period="04:00")
        second = self._goal(2, team=self.HOME, home=2, away=0, scorer=2, time_in_period="06:00")
        renumbered = self._goal(2, team=self.HOME, home=1, away=0, scorer=2, time_in_period="06:00")
        return first, second, renumbered

    def test_the_later_goal_is_not_announced_again(self, nhl_command):
        first, second, renumbered = self._scenario()
        lines = self._run(nhl_command, [self._pbp(first), self._pbp(first, second)] + [self._pbp(renumbered)] * 3)
        assert len(self._goals(lines)) == 2

    def test_the_disallowed_goal_is_retracted_and_the_valid_one_is_not(self, nhl_command):
        first, second, renumbered = self._scenario()
        lines = self._run(nhl_command, [self._pbp(first), self._pbp(first, second)] + [self._pbp(renumbered)] * 8)
        retractions = self._retractions(lines)
        assert len(retractions) == 1
        assert "04:00" in retractions[0] and "P1" in retractions[0] and "P2" not in retractions[0]

    def test_a_displaced_goal_nobody_saw_posted_is_never_retracted(self, nhl_command):
        first, second, renumbered = self._scenario()
        record = {("score", self.HOME, 1, 0): seeded(1)}
        lines = self._run(nhl_command, [self._pbp(first, second)] + [self._pbp(renumbered)] * 8, seeded_record=record)
        assert len(self._goals(lines)) == 1 and self._retractions(lines) == []
