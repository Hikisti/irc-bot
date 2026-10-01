import datetime
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from nhl_command import NHLCommand
from tests.conftest import make_json_response

TODAY = "2026-10-01"
YESTERDAY = "2026-09-30"


def score_game(gid=1, home="CAR", away="FLA", state="FUT", home_score=None, away_score=None,
               start="2026-10-01T23:00:00Z", date=TODAY, period=None, period_type="REG",
               remaining=None, intermission=False, last_period=None):
    """One game as /v1/score/{date} returns it (only the fields used)."""
    game = {
        "id": gid, "gameDate": date, "startTimeUTC": start, "gameState": state,
        "homeTeam": {"abbrev": home}, "awayTeam": {"abbrev": away},
    }
    if home_score is not None:
        game["homeTeam"]["score"] = home_score
    if away_score is not None:
        game["awayTeam"]["score"] = away_score
    if period is not None:
        game["periodDescriptor"] = {"number": period, "periodType": period_type}
        game["clock"] = {"timeRemaining": remaining, "inIntermission": intermission}
    if last_period:
        game["gameOutcome"] = {"lastPeriodType": last_period}
    return game


@pytest.fixture
def nhl():
    command = NHLCommand()
    command.session.get = MagicMock()  # nothing here may reach the network
    return command


def scores(nhl, by_date):
    """Makes _fetch_scores answer from {date: [games] | None}."""
    def fake(date):
        games = by_date.get(date, [])
        return None if games is None else {g["id"]: g for g in games}
    return patch.object(nhl, "_fetch_scores", side_effect=fake)


def eastern_dates(count=4):
    now = datetime.datetime.now(NHLCommand.EASTERN_TZ)
    return [(now - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(count)]


class TestFetchScores:
    def test_returns_games_keyed_by_id(self, nhl):
        nhl.session.get.return_value = make_json_response({"games": [score_game(5), score_game(6)]})
        assert set(nhl._fetch_scores(TODAY)) == {5, 6}
        assert nhl.session.get.call_args[0][0].endswith(f"/score/{TODAY}")

    def test_drops_games_of_another_date(self, nhl):
        nhl.session.get.return_value = make_json_response({"games": [score_game(5), score_game(6, date="2026-10-02")]})
        assert set(nhl._fetch_scores(TODAY)) == {5}

    def test_keeps_a_game_with_no_gamedate_and_drops_one_with_no_id(self, nhl):
        no_date = score_game(5); del no_date["gameDate"]
        no_id = score_game(6); del no_id["id"]
        nhl.session.get.return_value = make_json_response({"games": [no_date, no_id]})
        assert set(nhl._fetch_scores(TODAY)) == {5}

    def test_missing_games_key_is_an_empty_slate(self, nhl):
        nhl.session.get.return_value = make_json_response({})
        assert nhl._fetch_scores(TODAY) == {}

    @pytest.mark.parametrize("failure", [
        requests.exceptions.ConnectionError("down"),
        requests.exceptions.Timeout("slow"),
    ])
    def test_request_failure_is_none(self, nhl, failure):
        nhl.session.get.side_effect = failure
        assert nhl._fetch_scores(TODAY) is None

    def test_http_error_is_none(self, nhl):
        nhl.session.get.return_value = make_json_response({}, status_code=500)
        assert nhl._fetch_scores(TODAY) is None

    @pytest.mark.parametrize("body", [["not", "a", "dict"], "text"])
    def test_unexpected_shape_is_none(self, nhl, body):
        nhl.session.get.return_value = make_json_response(body)
        assert nhl._fetch_scores(TODAY) is None

    def test_invalid_json_is_none(self, nhl):
        resp = make_json_response({})
        resp.json.side_effect = ValueError("bad json")
        nhl.session.get.return_value = resp
        assert nhl._fetch_scores(TODAY) is None

    def test_repeat_within_the_cache_window_makes_one_request(self, nhl):
        nhl.session.get.return_value = make_json_response({"games": [score_game(5)]})
        nhl._fetch_scores(TODAY)
        nhl._fetch_scores(TODAY)
        assert nhl.session.get.call_count == 1

    def test_cache_expires(self, nhl):
        nhl.session.get.return_value = make_json_response({"games": [score_game(5)]})
        nhl._fetch_scores(TODAY)
        nhl._score_cache[TODAY] = (time.monotonic() - nhl.SCORE_CACHE_SECONDS - 1, {})
        nhl._fetch_scores(TODAY)
        assert nhl.session.get.call_count == 2

    def test_a_failure_is_not_cached(self, nhl):
        nhl.session.get.side_effect = requests.exceptions.ConnectionError("down")
        nhl._fetch_scores(TODAY)
        nhl.session.get.side_effect = None
        nhl.session.get.return_value = make_json_response({"games": [score_game(5)]})
        assert set(nhl._fetch_scores(TODAY)) == {5}

    def test_cache_is_per_date(self, nhl):
        nhl.session.get.return_value = make_json_response({"games": []})
        nhl._fetch_scores(TODAY)
        nhl._fetch_scores(YESTERDAY)
        assert nhl.session.get.call_count == 2


class TestLiveStatus:
    @pytest.mark.parametrize("period, ptype, remaining, intermission, expected", [
        (1, "REG", "12:34", False, "1st 12:34 left"),
        (2, "REG", "00:05", False, "2nd 00:05 left"),
        (3, "REG", "20:00", False, "3rd 20:00 left"),
        (1, "REG", "00:00", True, "1st int."),
        (4, "OT", "03:20", False, "OT 03:20 left"),
        (5, "OT", "04:10", False, "2OT 04:10 left"),
        (5, "SO", None, False, "SO"),
        (3, "REG", None, False, "3rd"),
    ])
    def test_status_text(self, nhl, period, ptype, remaining, intermission, expected):
        game = score_game(state="LIVE", period=period, period_type=ptype, remaining=remaining,
                          intermission=intermission)
        assert nhl._live_status(game) == expected

    def test_no_period_or_clock_gives_empty_status(self, nhl):
        assert nhl._live_status(score_game(state="LIVE")) == ""

    def test_non_integer_ot_number_does_not_crash(self, nhl):
        game = score_game(state="LIVE", period=None)
        game["periodDescriptor"] = {"number": "x", "periodType": "OT"}
        assert nhl._live_status(game) == ""

    def test_unknown_period_number_shows_only_the_clock(self, nhl):
        game = score_game(state="LIVE", period=9, remaining="01:00")
        assert nhl._live_status(game) == "01:00 left"


class TestLabels:
    def test_live_label_has_score_and_status(self, nhl):
        game = score_game(home="TOR", away="NYI", state="LIVE", home_score=2, away_score=1,
                          period=2, remaining="12:34")
        assert nhl._live_label(game) == "TOR 2-1 NYI 2nd 12:34 left"

    def test_live_game_without_scores_shows_just_the_matchup(self, nhl):
        game = score_game(home="TOR", away="NYI", state="LIVE", period=1, remaining="19:59")
        assert nhl._live_label(game) == "TOR-NYI 1st 19:59 left"

    def test_live_label_without_period_data_has_no_trailing_space(self, nhl):
        game = score_game(home="TOR", away="NYI", state="LIVE", home_score=0, away_score=0)
        assert nhl._live_label(game) == "TOR 0-0 NYI"

    def test_final_label(self, nhl):
        assert nhl._final_label(score_game(home="PHI", away="PIT", state="OFF", home_score=0, away_score=7,
                                           last_period="REG")) == "PHI 0-7 PIT"

    @pytest.mark.parametrize("kind", ["OT", "SO"])
    def test_final_label_marks_overtime_and_shootout(self, nhl, kind):
        game = score_game(home="PHI", away="PIT", state="FINAL", home_score=3, away_score=2, last_period=kind)
        assert nhl._final_label(game) == f"PHI 3-2 PIT ({kind})"

    def test_final_without_scores_shows_just_the_matchup(self, nhl):
        assert nhl._final_label(score_game(home="PHI", away="PIT", state="OFF")) == "PHI-PIT"

    def test_missing_team_data_does_not_crash(self, nhl):
        assert nhl._final_label({"gameState": "OFF"}) == "?-?"


class TestBoard:
    def test_all_three_groups_in_order(self, nhl):
        live = score_game(1, "TOR", "NYI", "LIVE", 2, 1, period=2, remaining="12:34")
        done = score_game(2, "PHI", "PIT", "OFF", 0, 7, last_period="REG")
        later = score_game(3, "COL", "LAK", "FUT", start="2026-10-02T02:00:00Z")
        with scores(nhl, {eastern_dates()[0]: [later, done, live], eastern_dates()[1]: []}):
            messages = nhl._board_messages()

        assert len(messages) == 1
        text = messages[0]
        assert text.index("Live: TOR 2-1 NYI 2nd 12:34 left") < text.index("Final: PHI 0-7 PIT") < text.index("Upcoming: ")
        assert "COL-LAK" in text
        assert " || " in text

    def test_empty_groups_are_left_out(self, nhl):
        done = score_game(2, "PHI", "PIT", "OFF", 0, 7)
        with scores(nhl, {eastern_dates()[0]: [done], eastern_dates()[1]: []}):
            text = nhl._board_messages()[0]
        assert text == "Final: PHI 0-7 PIT"

    def test_upcoming_is_grouped_by_helsinki_start_time(self, nhl):
        a = score_game(1, "A", "B", "FUT", start="2026-10-01T23:00:00Z")
        b = score_game(2, "C", "D", "PRE", start="2026-10-01T23:00:00Z")
        c = score_game(3, "E", "F", "FUT", start="2026-10-02T02:00:00Z")
        with scores(nhl, {eastern_dates()[0]: [a, b, c], eastern_dates()[1]: []}):
            text = nhl._board_messages()[0]
        assert text == "Upcoming: 02:00 A-B, C-D | 05:00 E-F"

    def test_crit_counts_as_live(self, nhl):
        g = score_game(1, "A", "B", "CRIT", 1, 1, period=3, remaining="00:40")
        with scores(nhl, {eastern_dates()[0]: [g], eastern_dates()[1]: []}):
            assert nhl._board_messages()[0].startswith("Live: A 1-1 B 3rd 00:40 left")

    def test_a_game_from_yesterdays_slate_still_in_progress_is_included(self, nhl):
        spill = score_game(9, "EDM", "VAN", "LIVE", 1, 0, date=YESTERDAY, period=3, remaining="05:00")
        old_final = score_game(8, "OLD", "ONE", "OFF", 3, 2, date=YESTERDAY)
        with scores(nhl, {eastern_dates()[0]: [], eastern_dates()[1]: [spill, old_final]}):
            text = nhl._board_messages()[0]
        assert "EDM 1-0 VAN" in text
        assert "OLD" not in text  # yesterday's finished games are not today's board

    def test_yesterday_failing_still_shows_today(self, nhl):
        g = score_game(1, "A", "B", "FUT")
        with scores(nhl, {eastern_dates()[0]: [g], eastern_dates()[1]: None}):
            assert nhl._board_messages() == ["Upcoming: 02:00 A-B"]

    def test_today_failing_is_none(self, nhl):
        with scores(nhl, {eastern_dates()[0]: None}):
            assert nhl._board_messages() is None

    def test_a_postponed_or_odd_state_game_is_listed_as_upcoming_not_dropped(self, nhl):
        g = score_game(1, "A", "B", "PPD")
        with scores(nhl, {eastern_dates()[0]: [g], eastern_dates()[1]: []}):
            assert "A-B" in nhl._board_messages()[0]

    def test_no_games_points_at_the_next_gameday(self, nhl):
        nxt = score_game(1, "TOR", "MTL", "FUT", start="2026-10-03T23:00:00Z")
        with scores(nhl, {eastern_dates()[0]: [], eastern_dates()[1]: []}), \
             patch.object(nhl, "_fetch_next_gameday", return_value=("2026-10-04", {1: nxt})):
            text = nhl._board_messages()[0]
        assert text.startswith("No NHL games today. Next NHL gameday (")
        assert "TOR-MTL" in text

    def test_no_games_and_no_next_gameday(self, nhl):
        with scores(nhl, {eastern_dates()[0]: [], eastern_dates()[1]: []}), \
             patch.object(nhl, "_fetch_next_gameday", return_value=(None, None)):
            assert nhl._board_messages() == ["No NHL games today."]

    def test_no_games_and_next_gameday_lookup_raising(self, nhl):
        with scores(nhl, {eastern_dates()[0]: [], eastern_dates()[1]: []}), \
             patch.object(nhl, "_fetch_next_gameday", side_effect=RuntimeError("boom")):
            assert nhl._board_messages() == ["No NHL games today."]

    def test_many_games_split_into_lines_that_fit(self, nhl):
        games = [score_game(i, f"T{i:02d}", f"U{i:02d}", "LIVE", 1, 2, period=2, remaining="10:00")
                 for i in range(30)]
        with scores(nhl, {eastern_dates()[0]: games, eastern_dates()[1]: []}):
            messages = nhl._board_messages()
        assert len(messages) > 1
        for m in messages:
            assert len(m.encode("utf-8")) <= nhl.MAX_LINE_BYTES
        joined = " ".join(messages)
        for i in range(30):
            assert f"T{i:02d} 1-2 U{i:02d}" in joined  # nothing lost in the split
        assert all(m.startswith("Live: ") for m in messages)  # continuation lines repeat the label


class TestResults:
    def test_lists_finished_games_of_todays_slate(self, nhl):
        done = score_game(1, "PHI", "PIT", "OFF", 0, 7, last_period="REG", start="2026-09-30T23:30:00Z")
        ot = score_game(2, "TOR", "NYI", "FINAL", 2, 1, last_period="OT", start="2026-09-30T23:30:00Z")
        with scores(nhl, {eastern_dates()[0]: [ot, done]}):
            messages = nhl._results_messages()
        assert messages == ["NHL results 1.10.: TOR 2-1 NYI (OT), PHI 0-7 PIT"]  # same start: input order kept

    def test_falls_back_to_yesterday_when_today_has_nothing_finished(self, nhl):
        today_upcoming = score_game(1, "A", "B", "FUT")
        last_night = score_game(2, "PHI", "PIT", "OFF", 0, 7, date=YESTERDAY, start="2026-09-30T23:30:00Z")
        with scores(nhl, {eastern_dates()[0]: [today_upcoming], eastern_dates()[1]: [last_night]}):
            messages = nhl._results_messages()
        assert messages == ["NHL results 1.10.: PHI 0-7 PIT"]

    def test_skips_off_days(self, nhl):
        far = score_game(2, "PHI", "PIT", "OFF", 1, 0, start="2026-09-28T23:30:00Z")
        d = eastern_dates()
        with scores(nhl, {d[0]: [], d[1]: [], d[2]: [], d[3]: [far]}):
            assert nhl._results_messages() == ["NHL results 29.9.: PHI 1-0 PIT"]

    def test_games_still_on_are_counted_not_listed(self, nhl):
        done = score_game(1, "PHI", "PIT", "OFF", 0, 7)
        live1 = score_game(2, "TOR", "NYI", "LIVE", 1, 0, period=1, remaining="10:00")
        live2 = score_game(3, "COL", "LAK", "CRIT", 2, 2, period=3, remaining="01:00")
        with scores(nhl, {eastern_dates()[0]: [done, live1, live2]}):
            messages = nhl._results_messages()
        assert messages == ["NHL results 2.10.: PHI 0-7 PIT (2 game(s) still on: !nhl now)"]  # default start is 02:00 Helsinki on the 2nd
        assert "TOR" not in messages[0]

    def test_a_game_still_on_today_is_counted_when_results_come_from_yesterday(self, nhl):
        live = score_game(1, "TOR", "NYI", "LIVE", 1, 0, period=1, remaining="10:00")
        last_night = score_game(2, "PHI", "PIT", "OFF", 0, 7, date=YESTERDAY)
        with scores(nhl, {eastern_dates()[0]: [live], eastern_dates()[1]: [last_night]}):
            assert nhl._results_messages()[0].endswith("(1 game(s) still on: !nhl now)")

    def test_nothing_recent(self, nhl):
        d = eastern_dates()
        with scores(nhl, {x: [] for x in d}):
            assert nhl._results_messages() == ["No recent NHL results found."]

    def test_today_failing_is_none(self, nhl):
        with scores(nhl, {eastern_dates()[0]: None}):
            assert nhl._results_messages() is None

    def test_a_lookback_date_failing_is_none(self, nhl):
        with scores(nhl, {eastern_dates()[0]: [], eastern_dates()[1]: None}):
            assert nhl._results_messages() is None

    def test_long_result_list_splits_and_the_pointer_goes_on_the_last_line(self, nhl):
        games = [score_game(i, f"T{i:02d}", f"U{i:02d}", "OFF", 3, 2) for i in range(40)]
        games.append(score_game(99, "L", "M", "LIVE", 0, 0, period=1, remaining="01:00"))
        with scores(nhl, {eastern_dates()[0]: games}):
            messages = nhl._results_messages()
        assert len(messages) > 1
        assert messages[-1].endswith("(1 game(s) still on: !nhl now)")
        assert all("still on" not in m for m in messages[:-1])
        assert all(m.startswith("NHL results ") for m in messages)

    def test_date_label_falls_back_to_the_raw_text_when_unparseable(self, nhl):
        with patch.object(nhl, "_helsinki_date_label", return_value="soon"):
            assert nhl._result_date_label({}, "2026-09-30") == "soon"


class TestPacking:
    def test_short_parts_share_one_message(self, nhl):
        assert nhl._pack(["a", "b", "c"]) == ["a || b || c"]

    def test_parts_that_do_not_fit_together_start_a_new_message(self, nhl):
        nhl.MAX_LINE_BYTES = 10
        assert nhl._pack(["aaaaaa", "bbbbbb"]) == ["aaaaaa", "bbbbbb"]

    def test_no_parts_no_messages(self, nhl):
        assert nhl._pack([]) == []

    def test_a_single_oversize_item_is_kept_whole(self, nhl):
        nhl.MAX_LINE_BYTES = 5
        assert nhl._chunk("P: ", ["toolongitem"], ", ") == ["P: toolongitem"]

    def test_byte_length_not_character_length_is_what_counts(self, nhl):
        nhl.MAX_LINE_BYTES = 12
        # "ää" is 2 chars but 4 bytes each pair - 'P: ää, ää' is 9 chars / 13 bytes
        assert nhl._chunk("P: ", ["ää", "ää"], ", ") == ["P: ää", "P: ää"]


class _InlineThread:
    """Stands in for threading.Thread so the lookup runs where the test can
    see it finish, with no real thread to wait for."""
    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class TestExecute:
    def _run(self, nhl, arg, bot=None):
        bot = bot or MagicMock()
        with patch("nhl_scoreboard.threading.Thread", _InlineThread):
            result = nhl.execute(arg, irc_bot=bot, channel="#nhl.fi")
        return result, bot

    def test_now_sends_the_board_and_replies_nothing_itself(self, nhl):
        g = score_game(1, "A", "B", "FUT")
        with scores(nhl, {eastern_dates()[0]: [g], eastern_dates()[1]: []}):
            result, bot = self._run(nhl, "now")
        assert result is None
        assert bot.send_message.call_args[0] == ("#nhl.fi", "Upcoming: 02:00 A-B")

    def test_results_sends_the_results(self, nhl):
        g = score_game(1, "PHI", "PIT", "OFF", 0, 7, start="2026-09-30T23:30:00Z")
        with scores(nhl, {eastern_dates()[0]: [g]}):
            result, bot = self._run(nhl, "results")
        assert result is None
        assert bot.send_message.call_args[0] == ("#nhl.fi", "NHL results 1.10.: PHI 0-7 PIT")

    def test_arguments_are_case_and_space_insensitive(self, nhl):
        g = score_game(1, "A", "B", "FUT")
        with scores(nhl, {eastern_dates()[0]: [g], eastern_dates()[1]: []}):
            _, bot = self._run(nhl, "  NOW ")
        bot.send_message.assert_called_once()

    def test_multi_message_output_is_sent_in_order(self, nhl):
        games = [score_game(i, f"T{i:02d}", f"U{i:02d}", "OFF", 1, 0) for i in range(40)]
        with scores(nhl, {eastern_dates()[0]: games}):
            _, bot = self._run(nhl, "results")
        assert bot.send_message.call_count > 1

    @pytest.mark.parametrize("arg", ["now", "results"])
    def test_api_failure_replies_with_an_error_string(self, nhl, arg):
        with scores(nhl, {eastern_dates()[0]: None}):
            _, bot = self._run(nhl, arg)
        assert bot.send_message.call_args[0][1] == "Error: could not reach the NHL API."

    def test_an_unexpected_exception_replies_with_an_error_string(self, nhl):
        with patch.object(nhl, "_board_messages", side_effect=RuntimeError("boom")):
            _, bot = self._run(nhl, "now")
        assert bot.send_message.call_args[0][1] == "Error: could not reach the NHL API."

    @pytest.mark.parametrize("arg", ["now", "results"])
    def test_without_channel_context_it_says_so(self, nhl, arg):
        assert "channel context" in nhl.execute(arg, irc_bot=None, channel=None)

    def test_a_broken_connection_does_not_raise(self, nhl):
        bot = MagicMock()
        bot.send_message.side_effect = OSError("socket closed")
        g = score_game(1, "A", "B", "FUT")
        with scores(nhl, {eastern_dates()[0]: [g], eastern_dates()[1]: []}):
            self._run(nhl, "now", bot)  # must not raise

    @pytest.mark.parametrize("arg, method", [("start", "_start"), ("stop", "_stop"), ("next", "_next")])
    def test_start_stop_and_next_still_dispatch_to_the_tracker(self, nhl, arg, method):
        with patch.object(nhl, method, return_value="handled") as handler:
            assert nhl.execute(arg, irc_bot=MagicMock(), channel="#nhl.fi") == "handled"
        handler.assert_called_once()

    def test_usage_and_help_list_the_new_subcommands(self, nhl):
        assert nhl.execute("bogus", irc_bot=MagicMock(), channel="#nhl.fi") == (
            "Usage: !nhl start | !nhl stop | !nhl next | !nhl now | !nhl results")
        assert nhl.HELP == "!nhl start|stop|next|now|results"
