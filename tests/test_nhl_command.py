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
    def test_seeds_goal_counts_from_play_by_play(self, nhl_command):
        items = {1: make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")}
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, plays=[
            goal_play(event_owner_team_id=10, scoring_player_id=100),
            goal_play(event_owner_team_id=20, scoring_player_id=200),
            goal_play(event_owner_team_id=20, scoring_player_id=201),
        ])
        with patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp):
            state = nhl_command._build_initial_state(items)

        assert state[1]["home_goals"] == 1
        assert state[1]["away_goals"] == 2
        assert state[1]["ended"] is False

    def test_play_by_play_failure_leaves_goal_counts_at_zero(self, nhl_command):
        items = {1: make_schedule_game(gid=1)}
        with patch.object(nhl_command, "_fetch_play_by_play", return_value=None):
            state = nhl_command._build_initial_state(items)
        assert state[1]["home_goals"] == 0
        assert state[1]["away_goals"] == 0

    def test_empty_items_short_circuits(self, nhl_command):
        assert nhl_command._build_initial_state({}) == {}


class TestPollOnce:
    def _seed(self, nhl_command, channel, games_state):
        nhl_command._channels[channel] = {"stop_event": MagicMock(), "thread": None, "games": games_state}

    def test_new_goal_is_announced(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "home_goals": 0, "away_goals": 0, "ended": False},
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
            1: {"home_id": 10, "away_id": 20, "home_goals": 1, "away_goals": 0, "ended": False},
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
            1: {"home_id": 10, "away_id": 20, "home_goals": 0, "away_goals": 0, "ended": False},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="FINAL")
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, home_score=5, away_score=2, game_state="FINAL")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp):
            all_ended = nhl_command._poll_once(bot, "#nhl.fi")

        message = bot.send_message.call_args[0][1]
        assert "FINAL:" in message
        assert all_ended is True

    def test_already_ended_game_is_not_touched_again(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "home_goals": 3, "away_goals": 2, "ended": True},
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
        assert nhl_command._channels["#nhl.fi"]["games"][2]["home_goals"] == 1

    def test_play_by_play_failure_carries_forward_previous_goal_counts(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "home_goals": 2, "away_goals": 1, "ended": False},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=None):
            nhl_command._poll_once(bot, "#nhl.fi")

        bot.send_message.assert_not_called()
        assert nhl_command._channels["#nhl.fi"]["games"][1]["home_goals"] == 2
        assert nhl_command._channels["#nhl.fi"]["games"][1]["away_goals"] == 1

    def test_fetch_failure_returns_false_without_crashing(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {1: {"home_id": 10, "away_id": 20, "home_goals": 0, "away_goals": 0, "ended": False}})

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
            1: {"home_id": 10, "away_id": 20, "home_goals": 0, "away_goals": 0, "ended": False},
        })
        schedule_game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: schedule_game}), \
             patch.object(nhl_command, "_fetch_play_by_play", side_effect=RuntimeError("boom")):
            all_ended = nhl_command._poll_once(bot, "#nhl.fi")  # must not raise

        assert all_ended is False

    def test_unexpected_exception_processing_one_game_does_not_crash_the_poll(self, nhl_command):
        bot = MagicMock()
        self._seed(nhl_command, "#nhl.fi", {
            1: {"home_id": 10, "away_id": 20, "home_goals": 0, "away_goals": 0, "ended": False},
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
    guessing); "!nhl next" keeps the plain list."""

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

    def test_next_keeps_the_plain_list_even_for_a_started_game(self, nhl_command):
        games = self._one(game_state="LIVE", home_score=1, away_score=0)
        assert nhl_command._format_period_summary(games).endswith("TOR-MTL")

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
            1: {"home_id": 10, "away_id": 20, "home_goals": 0, "away_goals": 0, "ended": False},
        }}
        game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="FINAL")
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, home_score=5, away_score=2, game_state="FINAL")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
             patch.object(nhl_command, "_fetch_attendance", return_value=19088):
            nhl_command._poll_once(bot, "#nhl.fi")

        assert bot.send_message.call_args[0][1].endswith("| Yleisöä: 19088")
