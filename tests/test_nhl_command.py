import datetime
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from irc_format import BOLD, RESET
from tests.conftest import join_channel_thread, make_json_response as make_response
from tests.nhl_test_helpers import (
    goal_play, make_pbp, make_schedule_game, make_schedule_payload, roster_spot,
    seeded,
)


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

    def test_shootout_goal_shows_the_game_score_and_the_shootout_tally(self, nhl_command):
        """Issue #38: the game score stays at the tie, the tally says why the final reads 3-2."""
        goal = goal_play(event_id=7, event_owner_team_id=10, scoring_player_id=100, period_number=5, period_type="SO",
                         time_in_period="00:00", home_score=2, away_score=2)
        pbp = make_pbp(home_id=10, away_id=20, plays=[goal], roster=[roster_spot(100)])
        line = nhl_command._format_goal(pbp, goal)
        assert f"{BOLD}Carolina Hurricanes 2-2 Florida Panthers{RESET} SO 1-0 | Carolina Hurricanes — " in line
        assert "00:00" not in line

    def test_overtime_goal_shows_ot_label(self, nhl_command):
        pbp = make_pbp(home_id=10, roster=[roster_spot(100)])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, period_number=4, period_type="OT")
        assert " OT |" in nhl_command._format_goal(pbp, goal)


class TestShootoutTally:
    """Issue #38: the shootout tally is counted from the play list, in play order, up to the goal being announced."""

    def _so_goal(self, event_id, team, scorer=100):
        return goal_play(event_id=event_id, event_owner_team_id=team, scoring_player_id=scorer, period_number=5,
                         period_type="SO", time_in_period="00:00", home_score=2, away_score=2)

    def _pbp(self, *plays):
        regulation = goal_play(event_id=1, event_owner_team_id=10, home_score=1, away_score=0, scoring_player_id=100)
        return make_pbp(home_id=10, away_id=20, plays=[regulation, *plays], roster=[roster_spot(100)])

    def test_each_shootout_goal_shows_the_tally_so_far(self, nhl_command):
        goals = [self._so_goal(10, 10), self._so_goal(11, 20), self._so_goal(12, 20), self._so_goal(13, 10)]
        pbp = self._pbp(*goals)
        tallies = [nhl_command._format_goal(pbp, g).split(" | ")[0].split(" SO ")[1] for g in goals]
        assert tallies == ["1-0", "1-1", "1-2", "2-2"]

    def test_the_tally_is_home_then_away_whichever_team_scores(self, nhl_command):
        away_goal = self._so_goal(10, 20)
        assert " SO 0-1 | " in nhl_command._format_goal(self._pbp(away_goal), away_goal)

    def test_regulation_and_overtime_goals_are_not_counted_in_the_tally(self, nhl_command):
        overtime = goal_play(event_id=2, event_owner_team_id=20, period_number=4, period_type="OT", time_in_period="01:30",
                             home_score=1, away_score=1, scoring_player_id=100)
        goal = self._so_goal(10, 10)
        pbp = self._pbp(overtime, goal)
        assert " SO 1-0 | " in nhl_command._format_goal(pbp, goal)

    def test_the_tally_follows_play_order_not_event_ids(self, nhl_command):
        later = self._so_goal(5, 20)      # a lower id, but played second
        first = self._so_goal(99, 10)
        pbp = self._pbp(first, later)
        assert " SO 1-1 | " in nhl_command._format_goal(pbp, later)
        assert " SO 1-0 | " in nhl_command._format_goal(pbp, first)

    def test_a_goal_that_is_no_longer_in_the_play_list_gets_the_whole_tally(self, nhl_command):
        gone = self._so_goal(50, 10)
        assert " SO 1-0 | " in nhl_command._format_goal(self._pbp(self._so_goal(10, 10)), gone)

    def test_a_shootout_goal_without_an_event_id_is_found_by_identity(self, nhl_command):
        first, second = self._so_goal(None, 10), self._so_goal(None, 20)
        pbp = self._pbp(first, second)
        assert " SO 1-1 | " in nhl_command._format_goal(pbp, second)

    def test_other_goal_lines_are_unchanged(self, nhl_command):
        goal = goal_play(event_id=3, event_owner_team_id=10, period_number=4, period_type="OT", time_in_period="03:23",
                         home_score=2, away_score=1, scoring_player_id=100)
        line = nhl_command._format_goal(self._pbp(goal), goal)
        assert f"{RESET} 03:23 OT | " in line and " SO " not in line


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
             patch.object(nhl_command, "_fetch_play_by_play"):
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
