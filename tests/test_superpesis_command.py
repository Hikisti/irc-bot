import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from superpesis_command import SuperpesisCommand


def make_response(json_data, status_code=200, reason="Error"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = reason
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


def series_list_payload():
    """Trimmed real shape: multiple divisions under one season, plus an
    older season, to exercise both the level/series disambiguation and
    the "pick the latest season" logic."""
    return {
        "seasons": [
            {
                "season": {"id": 110, "season": 2026},
                "seasonSerieses": [
                    {"seasonSeries": {"id": 2945, "name": "Miesten Superpesis"},
                     "level": {"id": 1, "name": "Superpesis"}, "series": {"id": 1, "name": "Miehet"}},
                    {"seasonSeries": {"id": 2946, "name": "Naisten Superpesis"},
                     "level": {"id": 1, "name": "Superpesis"}, "series": {"id": 2, "name": "Naiset"}},
                    {"seasonSeries": {"id": 2954, "name": "Miesten Ykköspesis"},
                     "level": {"id": 3, "name": "Ykköspesis"}, "series": {"id": 1, "name": "Miehet"}},
                ],
            },
            {
                "season": {"id": 95, "season": 2025},
                "seasonSerieses": [
                    {"seasonSeries": {"id": 2810, "name": "Miesten Superpesis"},
                     "level": {"id": 1, "name": "Superpesis"}, "series": {"id": 1, "name": "Miehet"}},
                ],
            },
        ]
    }


def make_match(mid=146953, home="Manse PP", home_id=16802, away="Hyvinkään Tahko", away_id=16796,
                date="2026-08-24T14:00:00.000000Z", home_runs=None, away_runs=None, finished=False):
    live_result = {"finished": finished}
    if home_runs is not None:
        live_result["runs"] = [{"home": [home_runs], "away": [away_runs or 0]}]
    return {
        "id": mid,
        "home": {"id": home_id, "name": home},
        "away": {"id": away_id, "name": away},
        "date": date,
        "liveResult": live_result,
    }


def matches_list_payload(*matches):
    return [{"groups": [{"matches": list(matches)}]}]


def run_sub_event(player_id, team_id, pattern="eteni_koti"):
    """Builds a sub-event that _is_run_sub_event() recognizes."""
    if pattern == "eteni_koti":
        texts = [
            {"team": team_id, "type": "player", "id": player_id},
            {"type": "event", "text": "eteni"},
            "kotipesään",
            {"type": "stat", "score": 1},
        ]
    elif pattern == "juoksu":
        texts = [
            {"type": "event", "text": "juoksu"},
            {"team": team_id, "type": "player", "id": player_id, "hide": True},
        ]
    else:
        raise ValueError(pattern)
    return {"texts": texts, "runnersAtBases": [None] * 5}


def out_at_home_sub_event(player_id, team_id):
    """"paloi" (put out) at home - must NOT be counted as a run."""
    return {
        "texts": [
            {"team": team_id, "type": "player", "id": player_id},
            {"type": "event", "text": "paloi"},
            {"type": "stat", "out": 1},
            "kotipesään",
        ],
        "runnersAtBases": [None] * 5,
    }


def match_event(event_id, team_id, period=1, sub_events=None, batter=None):
    return {
        "id": event_id,
        "period": period,
        "inning": 0,
        "team": team_id,
        "hTeam": team_id,
        "batter": batter,
        "events": sub_events or [],
    }


def join_channel_thread(sc, channel, timeout=2):
    entry = sc._channels.get(channel)
    if entry and entry.get("thread"):
        entry["thread"].join(timeout=timeout)


@pytest.fixture
def sc():
    return SuperpesisCommand()


class TestUsage:
    def test_no_args_shows_usage(self, sc):
        assert "Usage:" in sc.execute("", irc_bot=MagicMock(), channel="#pesis.fi")

    def test_unknown_arg_shows_usage(self, sc):
        assert "Usage:" in sc.execute("bogus", irc_bot=MagicMock(), channel="#pesis.fi")

    def test_start_without_context_errors(self, sc):
        assert "Error" in sc.execute("start")


class TestStartDoesNotBlock:
    def test_start_returns_before_series_lookup_completes(self, sc):
        release = threading.Event()

        def slow_resolve():
            release.wait(timeout=2)
            return 2945

        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", side_effect=slow_resolve), \
             patch.object(sc, "_fetch_today_matches", return_value={}):
            start = time.time()
            result = sc.execute("start", irc_bot=bot, channel="#pesis.fi")
            elapsed = time.time() - start

            assert elapsed < 1, "execute() blocked on the network"
            assert "Checking" in result

            release.set()
            join_channel_thread(sc, "#pesis.fi")

        bot.send_message.assert_called_once()

    def test_start_reserves_slot_against_races(self, sc):
        release = threading.Event()

        def slow_resolve():
            release.wait(timeout=2)
            return None

        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", side_effect=slow_resolve):
            sc.execute("start", irc_bot=bot, channel="#pesis.fi")
            second = sc.execute("start", irc_bot=bot, channel="#pesis.fi")
            release.set()
            join_channel_thread(sc, "#pesis.fi")

        assert "Already tracking" in second


class TestRun:
    def test_series_resolution_fails(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}

        with patch.object(sc, "_resolve_series_id", return_value=None):
            sc._run(bot, "#pesis.fi", stop_event)

        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")
        assert "#pesis.fi" not in sc._channels

    def test_matches_fetch_fails(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}

        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_today_matches", return_value=None):
            sc._run(bot, "#pesis.fi", stop_event)

        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")

    def test_no_matches_today(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}

        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_today_matches", return_value={}):
            sc._run(bot, "#pesis.fi", stop_event)

        bot.send_message.assert_called_once_with("#pesis.fi", "No Superpesis matches scheduled today.")

    def test_unexpected_exception_does_not_propagate(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}

        with patch.object(sc, "_resolve_series_id", side_effect=RuntimeError("boom")):
            sc._run(bot, "#pesis.fi", stop_event)  # must not raise

        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")

    def test_seeds_event_baseline_and_reports_matches(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}
        match = make_match()

        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_today_matches", return_value={146953: match}), \
             patch.object(sc, "_fetch_match_events", return_value=[{"id": 1}, {"id": 2}]), \
             patch.object(sc, "_poll_loop"):
            sc._run(bot, "#pesis.fi", stop_event)

        assert sc._channels["#pesis.fi"]["matches"][146953]["event_count"] == 2
        message = bot.send_message.call_args[0][1]
        assert "Tracking 1 Superpesis match" in message
        assert "Manse PP-Hyvinkään Tahko" in message


class TestStop:
    def test_stop_without_active_tracking(self, sc):
        assert "Not currently tracking" in sc.execute("stop", irc_bot=MagicMock(), channel="#pesis.fi")

    def test_stop_signals_thread_and_clears_state(self, sc):
        release = threading.Event()

        def slow_resolve():
            release.wait(timeout=2)
            return None

        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", side_effect=slow_resolve):
            sc.execute("start", irc_bot=bot, channel="#pesis.fi")
            stop_event = sc._channels["#pesis.fi"]["stop_event"]

            result = sc.execute("stop", irc_bot=bot, channel="#pesis.fi")

            assert "Stopped" in result
            assert stop_event.is_set()
            assert "#pesis.fi" not in sc._channels
            release.set()


class TestNext:
    def test_next_returns_immediately_without_blocking(self, sc):
        release = threading.Event()

        def slow_next():
            release.wait(timeout=2)
            return "found", "2026-08-28", {1: make_match(mid=1)}

        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", side_effect=slow_next):
            start = time.time()
            result = sc.execute("next", irc_bot=bot, channel="#pesis.fi")
            elapsed = time.time() - start

            assert elapsed < 1, "execute() blocked on the network"
            assert "Checking" in result

            release.set()
            time.sleep(0.2)  # let the one-shot background thread finish

        bot.send_message.assert_called_once()

    def test_next_without_context_errors(self, sc):
        assert "Error" in sc.execute("next")

    def test_run_next_reports_matches_and_date_label(self, sc):
        bot = MagicMock()
        matches = {1: make_match(mid=1, home="Sotkamon Jymy", away="Joensuun Maila")}
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", return_value=("found", "2026-08-28", matches)), \
             patch.object(sc, "_format_date_label", return_value="tomorrow"):
            sc._run_next(bot, "#pesis.fi")

        message = bot.send_message.call_args[0][1]
        assert "Next Superpesis matchday (tomorrow)" in message
        assert "Sotkamon Jymy-Joensuun Maila" in message

    def test_run_next_series_lookup_fails(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=None):
            sc._run_next(bot, "#pesis.fi")
        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")

    def test_run_next_api_error(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", return_value=("error", None, None)):
            sc._run_next(bot, "#pesis.fi")
        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")

    def test_run_next_nothing_found(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", return_value=("not_found", None, None)):
            sc._run_next(bot, "#pesis.fi")
        message = bot.send_message.call_args[0][1]
        assert "No upcoming Superpesis matches found" in message
        assert str(sc.NEXT_SEARCH_MAX_DAYS) in message

    def test_run_next_unexpected_exception_does_not_propagate(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", side_effect=RuntimeError("boom")):
            sc._run_next(bot, "#pesis.fi")  # must not raise
        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")


class TestFetchNextMatchday:
    def test_finds_the_first_date_with_matches(self, sc):
        calls = []

        def fake_fetch(series_id, date_str):
            calls.append(date_str)
            return {1: make_match(mid=1)} if len(calls) == 3 else {}

        with patch.object(sc, "_fetch_matches_for_date", side_effect=fake_fetch):
            status, date_str, matches = sc._fetch_next_matchday(2945)

        assert status == "found"
        assert len(calls) == 3
        assert matches == {1: make_match(mid=1)}

    def test_returns_today_if_matches_already_scheduled_today(self, sc):
        with patch.object(sc, "_fetch_matches_for_date", return_value={1: make_match(mid=1)}):
            status, date_str, matches = sc._fetch_next_matchday(2945)
        assert status == "found"
        assert matches == {1: make_match(mid=1)}

    def test_gives_up_after_max_days_and_reports_not_found(self, sc):
        with patch.object(sc, "_fetch_matches_for_date", return_value={}):
            status, date_str, matches = sc._fetch_next_matchday(2945)
        assert status == "not_found"
        assert date_str is None
        assert matches is None

    def test_request_failure_reports_error_distinct_from_not_found(self, sc):
        with patch.object(sc, "_fetch_matches_for_date", return_value=None):
            status, date_str, matches = sc._fetch_next_matchday(2945)
        assert status == "error"


class TestDateLabel:
    def test_today(self, sc):
        import datetime
        today = datetime.datetime.now(sc.HELSINKI_TZ).strftime("%Y-%m-%d")
        assert sc._format_date_label(today) == "today"

    def test_tomorrow(self, sc):
        import datetime
        tomorrow = (datetime.datetime.now(sc.HELSINKI_TZ) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        assert sc._format_date_label(tomorrow) == "tomorrow"

    def test_other_date_shows_weekday_and_date(self, sc):
        import datetime
        far_future = datetime.datetime.now(sc.HELSINKI_TZ) + datetime.timedelta(days=10)
        label = sc._format_date_label(far_future.strftime("%Y-%m-%d"))
        assert far_future.strftime("%d/%m") in label

    def test_malformed_date_falls_back_to_raw_string(self, sc):
        assert sc._format_date_label("not-a-date") == "not-a-date"


class TestSeriesResolution:
    def test_finds_mens_superpesis_in_latest_season(self, sc):
        with patch.object(sc.session, "get", return_value=make_response(series_list_payload())):
            series_id = sc._resolve_series_id()
        assert series_id == 2945  # not 2946 (women's) or 2810 (last season)

    def test_does_not_confuse_womens_superpesis(self, sc):
        payload = series_list_payload()
        with patch.object(sc.session, "get", return_value=make_response(payload)):
            series_id = sc._resolve_series_id()
        # 2946 is "Naisten Superpesis" - same level name, different series name.
        assert series_id != 2946

    def test_missing_series_returns_none(self, sc):
        with patch.object(sc.session, "get", return_value=make_response({"seasons": [{"season": {"season": 2026}, "seasonSerieses": []}]})):
            assert sc._resolve_series_id() is None

    def test_request_failure_returns_none(self, sc):
        with patch.object(sc.session, "get", side_effect=requests.exceptions.Timeout):
            assert sc._resolve_series_id() is None

    def test_malformed_json_returns_none(self, sc):
        resp = make_response({})
        resp.json.side_effect = ValueError()
        with patch.object(sc.session, "get", return_value=resp):
            assert sc._resolve_series_id() is None


class TestFetchMatches:
    def test_flattens_nested_groups(self, sc):
        m1, m2 = make_match(mid=1), make_match(mid=2)
        payload = matches_list_payload(m1, m2)
        with patch.object(sc.session, "get", return_value=make_response(payload)):
            matches = sc._fetch_today_matches(2945)
        assert set(matches.keys()) == {1, 2}

    def test_request_failure_returns_none(self, sc):
        with patch.object(sc.session, "get", side_effect=requests.exceptions.ConnectionError):
            assert sc._fetch_today_matches(2945) is None

    def test_unexpected_shape_returns_empty_dict(self, sc):
        with patch.object(sc.session, "get", return_value=make_response({"not": "a list"})):
            assert sc._fetch_today_matches(2945) == {}


class TestFetchEvents:
    def test_returns_events_list(self, sc):
        with patch.object(sc.session, "get", return_value=make_response({"events": [{"id": 1}]})):
            events = sc._fetch_match_events(146953)
        assert events == [{"id": 1}]

    def test_request_failure_returns_none(self, sc):
        with patch.object(sc.session, "get", side_effect=requests.exceptions.Timeout):
            assert sc._fetch_match_events(146953) is None

    def test_unexpected_shape_returns_none(self, sc):
        with patch.object(sc.session, "get", return_value=make_response({"no": "events key"})):
            assert sc._fetch_match_events(146953) is None


class TestRunDetection:
    """Built directly against real production data patterns (see the
    commit history/conversation this was developed against)."""

    def test_eteni_kotipesaan_is_a_run(self, sc):
        sub = run_sub_event(9986, 16803, pattern="eteni_koti")
        assert sc._is_run_sub_event(sub["texts"]) is True

    def test_juoksu_is_a_run(self, sc):
        sub = run_sub_event(7911, 16802, pattern="juoksu")
        assert sc._is_run_sub_event(sub["texts"]) is True

    def test_paloi_kotipesaan_is_not_a_run(self, sc):
        # Put out at home plate - same destination string, different verb.
        sub = out_at_home_sub_event(9986, 16803)
        assert sc._is_run_sub_event(sub["texts"]) is False

    def test_eteni_to_a_regular_base_is_not_a_run(self, sc):
        sub = {
            "texts": [
                {"team": 16803, "type": "player", "id": 9986},
                {"type": "event", "text": "eteni"},
                "ykköspesälle",
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is False

    def test_extract_runs_yields_multiple_scores_in_one_event(self, sc):
        event = match_event(1, team_id=16803, sub_events=[
            run_sub_event(9986, 16803, pattern="eteni_koti"),
            {"texts": [{"type": "event", "text": "eteni"}, "kolmospesälle"]},  # not a run
            run_sub_event(9904, 16803, pattern="eteni_koti"),
        ])
        runs = list(sc._extract_runs(event))
        assert runs == [(9986, 16803), (9904, 16803)]

    def test_extract_runs_falls_back_to_batter_when_no_player_ref(self, sc):
        event = match_event(1, team_id=16802, batter=555, sub_events=[
            {"texts": [{"type": "event", "text": "juoksu"}]},
        ])
        runs = list(sc._extract_runs(event))
        assert runs == [(555, 16802)]

    def test_extract_runs_empty_for_non_scoring_event(self, sc):
        event = match_event(1, team_id=16802, sub_events=[
            {"texts": ["1. lyönti", {"type": "hit", "hit": None}]},
        ])
        assert list(sc._extract_runs(event)) == []


class TestSumRuns:
    def test_sums_across_periods(self, sc):
        live = {"runs": [{"home": [2, 0], "away": [1]}, {"home": [3], "away": [0, 1]}]}
        assert sc._sum_runs(live, "home") == 5
        assert sc._sum_runs(live, "away") == 2

    def test_missing_runs_returns_none(self, sc):
        assert sc._sum_runs({}, "home") is None

    def test_malformed_runs_returns_none(self, sc):
        assert sc._sum_runs({"runs": "not a list"}, "home") is None


class TestPlayerResolution:
    def test_resolves_and_caches(self, sc):
        payload = {"name": "Santtu Patova"}
        with patch.object(sc.session, "get", return_value=make_response(payload)) as mock_get:
            first = sc._resolve_player_name(7911)
            second = sc._resolve_player_name(7911)

        assert first == "Santtu Patova"
        assert second == "Santtu Patova"
        mock_get.assert_called_once()  # second call hit the cache

    def test_falls_back_to_first_last_name(self, sc):
        payload = {"first_name": "Santtu", "last_name": "Patova"}
        with patch.object(sc.session, "get", return_value=make_response(payload)):
            assert sc._resolve_player_name(7911) == "Santtu Patova"

    def test_lookup_failure_falls_back_to_placeholder(self, sc):
        with patch.object(sc.session, "get", side_effect=requests.exceptions.Timeout):
            assert sc._resolve_player_name(999) == "Player 999"

    def test_404_falls_back_to_placeholder(self, sc):
        with patch.object(sc.session, "get", return_value=make_response({}, status_code=404)):
            assert sc._resolve_player_name(999) == "Player 999"


class TestMatchesSummary:
    def test_groups_by_start_time(self, sc):
        matches = [
            make_match(mid=1, home="A", away="B", date="2026-08-24T14:00:00.000000Z"),
            make_match(mid=2, home="C", away="D", date="2026-08-24T14:00:00.000000Z"),
            make_match(mid=3, home="E", away="F", date="2026-08-24T15:30:00.000000Z"),
        ]
        summary = sc._format_matches_summary(matches)
        assert summary == "17:00 A-B, C-D | 18:30 E-F"

    def test_missing_date_sorts_last(self, sc):
        matches = [
            make_match(mid=1, home="A", away="B", date=None),
            make_match(mid=2, home="C", away="D", date="2026-08-24T14:00:00.000000Z"),
        ]
        summary = sc._format_matches_summary(matches)
        assert summary == "17:00 C-D | ??:?? A-B"


class TestFormatRun:
    def test_includes_team_scorer_and_score(self, sc):
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 3, 1, 16802, 16802, "Santtu Patova")
        assert msg.startswith(sc.RUN_PREFIX)
        assert "Home — Santtu Patova" in msg
        assert "Home 3-1 Away" in msg
        assert "(jakso 1)" in msg

    def test_away_team_scoring(self, sc):
        event = {"period": 2}
        msg = sc._format_run(event, "Home", "Away", 1, 2, 999, 16802, "Someone")
        assert "Away — Someone" in msg

    def test_unknown_scorer_name(self, sc):
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, None)
        assert "Unknown" in msg

    def test_period_zero_has_no_label(self, sc):
        event = {"period": 0}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "jakso" not in msg


class TestProcessMatch:
    def _prev(self, **overrides):
        prev = {
            "match_id": 146953,
            "home_id": 16802,
            "away_id": 16796,
            "home_name": "Manse PP",
            "away_name": "Hyvinkään Tahko",
            "home_runs": 0,
            "away_runs": 0,
            "event_count": 0,
            "finished": False,
        }
        prev.update(overrides)
        return prev

    def test_new_run_is_announced_and_scored(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=16802, sub_events=[run_sub_event(111, 16802)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["event_count"] == 1
        message = bot.send_message.call_args[0][1]
        assert "RUN:" in message
        assert "Test Player" in message

    def test_score_snaps_to_authoritative_total(self, sc):
        bot = MagicMock()
        # Our own counting would only get to 1, but the authoritative
        # liveResult says 3 - the final state must reflect the latter.
        prev = self._prev(event_count=0, home_runs=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=3, away_runs=0)
        events = [match_event(1, team_id=16802, sub_events=[run_sub_event(111, 16802)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["home_runs"] == 3

    def test_no_new_events_sends_nothing(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=5)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=0, away_runs=0)

        with patch.object(sc, "_fetch_match_events", return_value=[{"id": i} for i in range(5)]):
            sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()

    def test_finish_transition_sends_final(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0, finished=False, home_runs=2, away_runs=1)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=2, away_runs=1, finished=True)

        with patch.object(sc, "_fetch_match_events", return_value=None):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["finished"] is True
        message = bot.send_message.call_args[0][1]
        assert message.startswith(sc.FINAL_PREFIX)
        assert "Manse PP 2-1 Hyvinkään Tahko" in message

    def test_already_finished_does_not_resend_final(self, sc):
        bot = MagicMock()
        prev = self._prev(finished=True, home_runs=2, away_runs=1)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=2, away_runs=1, finished=True)

        with patch.object(sc, "_fetch_match_events", return_value=None):
            sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()

    def test_events_fetch_failure_does_not_crash(self, sc):
        bot = MagicMock()
        prev = self._prev()
        match = make_match(mid=146953, home_id=16802, away_id=16796)

        with patch.object(sc, "_fetch_match_events", return_value=None):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)  # must not raise

        assert new_state["event_count"] == prev["event_count"]

    def test_run_by_scoring_team_not_home_or_away_is_ignored(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=99999, sub_events=[run_sub_event(1, 99999)])]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()
        assert new_state["home_runs"] == 0
        assert new_state["away_runs"] == 0


class TestPollOnce:
    def _seed(self, sc, channel, matches_state):
        sc._channels[channel] = {"stop_event": MagicMock(), "thread": None, "matches": matches_state}

    def test_all_finished_returns_true(self, sc):
        bot = MagicMock()
        self._seed(sc, "#pesis.fi", {
            146953: {"match_id": 146953, "home_id": 1, "away_id": 2, "home_name": "A", "away_name": "B",
                     "home_runs": 1, "away_runs": 0, "event_count": 0, "finished": True},
        })
        match = make_match(mid=146953, home_id=1, away_id=2, finished=True)

        with patch.object(sc, "_fetch_today_matches", return_value={146953: match}), \
             patch.object(sc, "_fetch_match_events", return_value=None):
            result = sc._poll_once(bot, "#pesis.fi", 2945)

        assert result is True

    def test_not_all_finished_returns_false(self, sc):
        bot = MagicMock()
        self._seed(sc, "#pesis.fi", {
            146953: {"match_id": 146953, "home_id": 1, "away_id": 2, "home_name": "A", "away_name": "B",
                     "home_runs": 0, "away_runs": 0, "event_count": 0, "finished": False},
        })
        match = make_match(mid=146953, home_id=1, away_id=2, finished=False)

        with patch.object(sc, "_fetch_today_matches", return_value={146953: match}), \
             patch.object(sc, "_fetch_match_events", return_value=None):
            result = sc._poll_once(bot, "#pesis.fi", 2945)

        assert result is False

    def test_fetch_failure_returns_false_without_crashing(self, sc):
        bot = MagicMock()
        self._seed(sc, "#pesis.fi", {146953: {"finished": False}})

        with patch.object(sc, "_fetch_today_matches", return_value=None):
            result = sc._poll_once(bot, "#pesis.fi", 2945)

        assert result is False

    def test_match_missing_from_todays_list_is_left_alone(self, sc):
        bot = MagicMock()
        prev_snapshot = {"match_id": 146953, "home_id": 1, "away_id": 2, "home_name": "A", "away_name": "B",
                          "home_runs": 0, "away_runs": 0, "event_count": 0, "finished": False}
        self._seed(sc, "#pesis.fi", {146953: prev_snapshot})

        with patch.object(sc, "_fetch_today_matches", return_value={}):
            result = sc._poll_once(bot, "#pesis.fi", 2945)

        assert result is False
        assert sc._channels["#pesis.fi"]["matches"][146953] == prev_snapshot

    def test_untracked_channel_stops_polling(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_fetch_today_matches", return_value={}):
            result = sc._poll_once(bot, "#never-started", 2945)
        assert result is True
