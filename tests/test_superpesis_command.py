import datetime
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


def run_sub_event_by_number(jersey_number, team_id):
    """Same as run_sub_event(pattern="eteni_koti") but using a jersey
    "number" reference instead of a global "id" - the format confirmed
    live for some matches (see _last_player_ref())."""
    return {
        "texts": [
            {"team": team_id, "type": "player", "number": jersey_number},
            {"type": "event", "text": "eteni"},
            "kotipesään",
            {"type": "stat", "score": 1},
        ],
        "runnersAtBases": [None] * 5,
    }


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
def sc(tmp_path, monkeypatch):
    # Isolate every test from the real on-disk series-id cache: without
    # this, a leftover cache file from a real bot run (or another test)
    # would make _resolve_series_id() skip the mocked session.get() call
    # entirely, breaking assertions that expect it to be called.
    monkeypatch.setattr(SuperpesisCommand, "SERIES_CACHE_FILE", str(tmp_path / "series_cache.json"))
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

        roster = {16802: {1: "A"}}
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_today_matches", return_value={146953: match}), \
             patch.object(sc, "_fetch_match_events", return_value=[{"id": 1}, {"id": 2}]), \
             patch.object(sc, "_fetch_match_roster", return_value=roster), \
             patch.object(sc, "_poll_loop"):
            sc._run(bot, "#pesis.fi", stop_event)

        assert sc._channels["#pesis.fi"]["matches"][146953]["event_count"] == 2
        assert sc._channels["#pesis.fi"]["matches"][146953]["roster"] == roster
        message = bot.send_message.call_args[0][1]
        assert "Tracking 1 Superpesis match" in message
        assert "Manse PP-Hyvinkään Tahko" in message


class TestSeedSnapshot:
    def test_seeds_from_the_current_period_only(self, sc):
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        match["liveResult"] = {
            "finished": False,
            "lastPeriod": 1,
            "runs": [{"home": [4], "away": [2]}, {"home": [0], "away": [1]}],
        }
        snapshot = sc._seed_snapshot(match)
        assert snapshot["period"] == 1
        assert snapshot["period_home_runs"] == 0
        assert snapshot["period_away_runs"] == 1

    def test_defaults_to_period_zero_with_no_live_result(self, sc):
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        snapshot = sc._seed_snapshot(match)
        assert snapshot["period"] == 0
        assert snapshot["period_home_runs"] == 0
        assert snapshot["period_away_runs"] == 0

    def test_seeds_an_empty_signature_set(self, sc):
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        snapshot = sc._seed_snapshot(match)
        assert snapshot["seen_run_signatures"] == set()


class TestSeedMatchExtras:
    def test_fills_in_event_count_and_roster_for_every_match(self, sc):
        state = {
            1: {"event_count": 0, "roster": {}},
            2: {"event_count": 0, "roster": {}},
        }
        rosters = {1: {10802: {1: "A"}}, 2: {10804: {1: "B"}}}

        def fake_events(mid):
            return [{"id": i} for i in range(mid * 3)]

        def fake_roster(mid):
            return rosters[mid]

        with patch.object(sc, "_fetch_match_events", side_effect=fake_events), \
             patch.object(sc, "_fetch_match_roster", side_effect=fake_roster):
            sc._seed_match_extras(state)

        assert state[1]["event_count"] == 3
        assert state[2]["event_count"] == 6
        assert state[1]["roster"] == rosters[1]
        assert state[2]["roster"] == rosters[2]

    def test_matches_are_fetched_concurrently_not_sequentially(self, sc):
        # If this ran sequentially, 3 matches x 0.1s each would take
        # >=0.3s; concurrently it should take roughly one slot's worth.
        state = {i: {"event_count": 0, "roster": {}} for i in (1, 2, 3)}

        def slow_events(mid):
            time.sleep(0.1)
            return []

        with patch.object(sc, "_fetch_match_events", side_effect=slow_events), \
             patch.object(sc, "_fetch_match_roster", return_value={}):
            start = time.time()
            sc._seed_match_extras(state)
            elapsed = time.time() - start

        assert elapsed < 0.25

    def test_event_fetch_failure_leaves_event_count_untouched(self, sc):
        state = {1: {"event_count": 5, "roster": {}}}

        with patch.object(sc, "_fetch_match_events", return_value=None), \
             patch.object(sc, "_fetch_match_roster", return_value={}):
            sc._seed_match_extras(state)

        assert state[1]["event_count"] == 5  # unchanged, not reset to 0

    def test_empty_state_does_nothing(self, sc):
        sc._seed_match_extras({})  # must not raise


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

    def test_skips_today_when_all_of_todays_matches_already_finished(self, sc):
        # Regression test for a real report: checking !superpesis next
        # hours after today's matches finished repeated today's stale
        # result instead of finding the actual next matchday.
        finished_today = make_match(mid=1, finished=True)
        tomorrow_match = make_match(mid=2, finished=False)
        today_str = datetime.datetime.now(sc.HELSINKI_TZ).strftime("%Y-%m-%d")
        tomorrow_str = (datetime.datetime.now(sc.HELSINKI_TZ) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        def fake_fetch(series_id, date_str):
            if date_str == today_str:
                return {1: finished_today}
            if date_str == tomorrow_str:
                return {2: tomorrow_match}
            return {}

        with patch.object(sc, "_fetch_matches_for_date", side_effect=fake_fetch):
            status, date_str, matches = sc._fetch_next_matchday(2945)

        assert status == "found"
        assert date_str == tomorrow_str
        assert matches == {2: tomorrow_match}

    def test_returns_today_if_only_some_of_todays_matches_have_finished(self, sc):
        finished = make_match(mid=1, finished=True)
        still_live = make_match(mid=2, finished=False)

        with patch.object(sc, "_fetch_matches_for_date", return_value={1: finished, 2: still_live}):
            status, date_str, matches = sc._fetch_next_matchday(2945)

        assert status == "found"
        assert matches == {1: finished, 2: still_live}


class TestAllMatchesFinished:
    def test_true_when_every_match_finished(self, sc):
        assert sc._all_matches_finished({1: make_match(mid=1, finished=True)}) is True

    def test_false_when_any_match_not_finished(self, sc):
        matches = {1: make_match(mid=1, finished=True), 2: make_match(mid=2, finished=False)}
        assert sc._all_matches_finished(matches) is False

    def test_false_for_empty_dict(self, sc):
        assert sc._all_matches_finished({}) is False


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

    def test_uses_current_season_filter(self, sc):
        # The unfiltered response is ~5.6MB (all historical seasons) vs
        # ~1MB filtered - confirmed live. Must always ask for the filter.
        with patch.object(sc.session, "get", return_value=make_response(series_list_payload())) as mock_get:
            sc._resolve_series_id()
        assert mock_get.call_args.kwargs["params"]["current-season"] == "true"

    def test_result_is_cached_across_calls(self, sc):
        with patch.object(sc.session, "get", return_value=make_response(series_list_payload())) as mock_get:
            first = sc._resolve_series_id()
            second = sc._resolve_series_id()
        assert first == second == 2945
        mock_get.assert_called_once()  # second call must hit the cache

    def test_cache_expires_after_ttl(self, sc):
        with patch.object(sc.session, "get", return_value=make_response(series_list_payload())) as mock_get:
            sc._resolve_series_id()
            with patch("time.time", return_value=time.time() + sc.SERIES_CACHE_TTL_SECONDS + 1):
                sc._resolve_series_id()
        assert mock_get.call_count == 2

    def test_cache_survives_across_instances_via_disk(self, sc):
        # The whole point of persisting to disk: a fresh instance (e.g.
        # after a bot restart) should still hit the cache rather than
        # refetching, as long as SERIES_CACHE_FILE points at the same
        # (still-fresh) file.
        with patch.object(sc.session, "get", return_value=make_response(series_list_payload())) as mock_get:
            sc._resolve_series_id()

        fresh_instance = SuperpesisCommand()
        with patch.object(fresh_instance.session, "get") as mock_get2:
            series_id = fresh_instance._resolve_series_id()

        mock_get2.assert_not_called()
        assert series_id == 2945

    def test_stale_disk_cache_is_not_used(self, sc):
        with patch.object(sc.session, "get", return_value=make_response(series_list_payload())):
            sc._resolve_series_id()

        stale_time = time.time() - sc.SERIES_CACHE_TTL_SECONDS - 1
        with open(sc.SERIES_CACHE_FILE, "w") as f:
            import json
            json.dump({"series_id": 2945, "resolved_at": stale_time}, f)

        fresh_instance = SuperpesisCommand()
        with patch.object(fresh_instance.session, "get", return_value=make_response(series_list_payload())) as mock_get2:
            fresh_instance._resolve_series_id()

        mock_get2.assert_called_once()

    def test_missing_cache_file_is_not_an_error(self, sc):
        assert sc._load_series_cache() is None  # tmp_path file doesn't exist yet

    def test_corrupt_cache_file_is_ignored(self, sc):
        with open(sc.SERIES_CACHE_FILE, "w") as f:
            f.write("not valid json{{{")
        assert sc._load_series_cache() is None

    def test_failed_lookup_is_not_cached(self, sc):
        with patch.object(sc.session, "get", side_effect=requests.exceptions.Timeout) as mock_get:
            sc._resolve_series_id()
            sc._resolve_series_id()
        assert mock_get.call_count == 2  # no successful id to cache, so it keeps retrying


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

    def test_kunnari_is_a_run(self, sc):
        # Home run - confirmed live it doesn't pair with a "kotipesään"
        # destination string like regular advancement does.
        sub = {
            "texts": [
                {"team": 16804, "type": "player", "number": 1},
                {"type": "event", "text": "löi kunnarin!", "base": 2},
                {"type": "stat", "homerun": 2},
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is True

    def test_eteni_harhaheitolla_kotipesaan_is_a_run(self, sc):
        # A suffixed "eteni..." variant (wild throw) - was being missed
        # entirely by an exact "eteni" match before this was reported.
        sub = {
            "texts": [
                {"team": 16804, "type": "player", "number": 11},
                {"type": "event", "text": "eteni harhaheitolla", "base": 3},
                "kotipesään",
                {"type": "stat", "wtscore": 3},
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is True

    def test_eteni_harhaheitolla_to_a_regular_base_is_not_a_run(self, sc):
        sub = {
            "texts": [
                {"team": 16804, "type": "player", "number": 7},
                {"type": "event", "text": "eteni harhaheitolla", "base": 0},
                "ykköspesälle",
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is False

    def _without_signature(self, runs):
        """_extract_runs() also yields a content signature (see
        _run_signature) - strip it for tests that only care about who/what
        scored, not the exact fingerprint string."""
        return [r[:3] for r in runs]

    def test_extract_runs_yields_multiple_scores_in_one_event(self, sc):
        event = match_event(1, team_id=16803, sub_events=[
            run_sub_event(9986, 16803, pattern="eteni_koti"),
            {"texts": [{"type": "event", "text": "eteni"}, "kolmospesälle"]},  # not a run
            run_sub_event(9904, 16803, pattern="eteni_koti"),
        ])
        runs = self._without_signature(sc._extract_runs(event))
        assert runs == [({"id": 9986}, 16803, None), ({"id": 9904}, 16803, None)]

    def test_extract_runs_with_number_based_player_ref(self, sc):
        event = match_event(1, team_id=16798, sub_events=[
            run_sub_event_by_number(1, 16798),
        ])
        runs = self._without_signature(sc._extract_runs(event))
        assert runs == [({"number": 1}, 16798, None)]

    def test_extract_runs_no_player_ref_carries_batter_through(self, sc):
        event = match_event(1, team_id=16802, batter=555, sub_events=[
            {"texts": [{"type": "event", "text": "juoksu"}]},
        ])
        runs = self._without_signature(sc._extract_runs(event))
        assert runs == [(None, 16802, 555)]

    def test_extract_runs_empty_for_non_scoring_event(self, sc):
        event = match_event(1, team_id=16802, sub_events=[
            {"texts": ["1. lyönti", {"type": "hit", "hit": None}]},
        ])
        assert list(sc._extract_runs(event)) == []

    def test_extract_runs_uses_harhaheitto_label_not_the_parent_batter(self, sc):
        # Regression test for a real incident: the parent event's own
        # "batter" field pointed at a completely unrelated player for a
        # wild-throw-caused run - must be replaced with the literal
        # "Harhaheitto" label instead of misattributing it.
        event = match_event(1, team_id=16804, batter=7, sub_events=[
            {"texts": [
                {"team": 16804, "type": "player", "number": 11},
                {"type": "event", "text": "eteni harhaheitolla", "base": 3},
                "kotipesään",
                {"type": "stat", "wtscore": 3},
            ]},
        ])
        runs = self._without_signature(sc._extract_runs(event))
        assert runs == [({"number": 11}, 16804, "Harhaheitto")]

    def test_extract_runs_regular_run_still_uses_parent_batter(self, sc):
        event = match_event(1, team_id=16804, batter=10, sub_events=[
            run_sub_event_by_number(3, 16804),
        ])
        runs = self._without_signature(sc._extract_runs(event))
        assert runs == [({"number": 3}, 16804, 10)]

    def test_extract_runs_two_identical_sub_events_get_different_signatures_only_if_content_differs(self, sc):
        # Same player/base/score twice in one event (distinct plays, e.g.
        # unlikely but not impossible) still get compared on full content -
        # this just documents that the signature is deterministic per
        # sub-event content, not e.g. randomized or positional.
        event = match_event(1, team_id=16803, sub_events=[
            run_sub_event(9986, 16803, pattern="eteni_koti"),
        ])
        sig_a = list(sc._extract_runs(event))[0][3]
        sig_b = list(sc._extract_runs(event))[0][3]
        assert sig_a == sig_b


class TestIsErrorDrivenRun:
    def test_true_for_harhaheitolla_suffix(self, sc):
        texts = [{"type": "event", "text": "eteni harhaheitolla"}]
        assert sc._is_error_driven_run(texts) is True

    def test_false_for_plain_eteni(self, sc):
        texts = [{"type": "event", "text": "eteni"}]
        assert sc._is_error_driven_run(texts) is False

    def test_false_for_kunnari(self, sc):
        texts = [{"type": "event", "text": "löi kunnarin!"}]
        assert sc._is_error_driven_run(texts) is False


class TestRunSignature:
    def test_deterministic_for_identical_content(self, sc):
        sub_event = run_sub_event(9986, 16803, pattern="eteni_koti")
        assert sc._run_signature(sub_event) == sc._run_signature(sub_event)

    def test_differs_for_different_content(self, sc):
        a = run_sub_event(9986, 16803, pattern="eteni_koti")
        b = run_sub_event(9904, 16803, pattern="eteni_koti")
        assert sc._run_signature(a) != sc._run_signature(b)

    def test_key_order_does_not_matter(self, sc):
        a = {"texts": ["x"], "runnersAtBases": [1, 2]}
        b = {"runnersAtBases": [1, 2], "texts": ["x"]}
        assert sc._run_signature(a) == sc._run_signature(b)

    def test_differs_across_periods_for_identical_sub_event_content(self, sc):
        # Regression test for a real incident (pesistulokset.fi match
        # 147207): the exact same two players and exact same bases-
        # occupied state produced a byte-identical sub-event in jakso 1
        # and again in jakso 2 - a real coincidence, not corrupt data
        # (confirmed live: both were genuine, distinct real runs on
        # pesistulokset.fi's own page). Without the period folded in
        # here, the second one collided with the first's signature and
        # was silently dropped as a false-positive duplicate.
        sub_event = run_sub_event(8119, 16801, pattern="eteni_koti")
        assert sc._run_signature(sub_event, period=0) != sc._run_signature(sub_event, period=1)

    def test_no_period_given_still_works(self, sc):
        # Callers that don't have a period (or don't care) keep working -
        # period defaults to None and still participates consistently.
        sub_event = run_sub_event(9986, 16803, pattern="eteni_koti")
        assert sc._run_signature(sub_event) == sc._run_signature(sub_event, period=None)


class TestLastPlayerRef:
    def test_prefers_id_when_present(self, sc):
        texts = [{"team": 1, "type": "player", "id": 42}]
        assert sc._last_player_ref(texts) == {"id": 42}

    def test_falls_back_to_number(self, sc):
        texts = [{"team": 1, "type": "player", "number": 7}]
        assert sc._last_player_ref(texts) == {"number": 7}

    def test_no_player_entry_returns_none(self, sc):
        assert sc._last_player_ref([{"type": "event", "text": "eteni"}]) is None

    def test_takes_the_last_player_entry(self, sc):
        texts = [
            {"team": 1, "type": "player", "id": 1},
            {"type": "event", "text": "eteni"},
            {"team": 1, "type": "player", "id": 2, "hide": True},
        ]
        assert sc._last_player_ref(texts) == {"id": 2}


class TestResolveScorerName:
    def test_id_ref_uses_global_lookup(self, sc):
        with patch.object(sc, "_resolve_player_name", return_value="Global Player") as mock_resolve:
            name = sc._resolve_scorer_name({"id": 42}, 16798, None, {})
        mock_resolve.assert_called_once_with(42)
        assert name == "Global Player"

    def test_number_ref_uses_roster(self, sc):
        roster = {16798: {1: "Konsta Piironen"}}
        name = sc._resolve_scorer_name({"number": 1}, 16798, None, roster)
        assert name == "Konsta Piironen"

    def test_number_ref_missing_from_roster_falls_through_to_batter(self, sc):
        roster = {16798: {1: "Konsta Piironen"}}
        with patch.object(sc, "_resolve_player_name", return_value="Fallback"):
            name = sc._resolve_scorer_name({"number": 99}, 16798, 5, roster)
        # number 99 isn't in the roster - must not silently invent a name.
        assert name == "Fallback"
        assert name != "Konsta Piironen"

    def test_batter_fallback_prefers_roster_lookup_over_global_id(self, sc):
        # Confirms the actual bug fix: a small "batter" number must NOT
        # be resolved as if it were a global player id when the roster
        # has an entry for it.
        roster = {16798: {1: "Konsta Piironen"}}
        with patch.object(sc, "_resolve_player_name") as mock_resolve:
            name = sc._resolve_scorer_name(None, 16798, 1, roster)
        mock_resolve.assert_not_called()
        assert name == "Konsta Piironen"

    def test_batter_fallback_uses_global_lookup_when_not_in_roster(self, sc):
        with patch.object(sc, "_resolve_player_name", return_value="Santtu Patova") as mock_resolve:
            name = sc._resolve_scorer_name(None, 16802, 7911, {})
        mock_resolve.assert_called_once_with(7911)
        assert name == "Santtu Patova"

    def test_no_ref_and_no_batter_returns_none(self, sc):
        assert sc._resolve_scorer_name(None, 16802, None, {}) is None

    def test_string_batter_fallback_is_returned_as_is(self, sc):
        # "Harhaheitto" (see _extract_runs/_is_error_driven_run) is a
        # literal label, not a jersey number or global id to look up -
        # must never reach the roster or the player-lookup API.
        with patch.object(sc, "_resolve_player_name") as mock_resolve:
            name = sc._resolve_scorer_name(None, 16802, "Harhaheitto", {16802: {1: "X"}})
        mock_resolve.assert_not_called()
        assert name == "Harhaheitto"


class TestFetchMatchRoster:
    def test_builds_number_to_name_mapping_for_both_teams(self, sc):
        payload = {
            "home": {"id": 16798, "players": [
                {"id": 7723, "number": 1, "name": "Konsta Piironen"},
                {"id": 11974, "number": 2, "first_name": "Niko", "last_name": "Korhonen"},
            ]},
            "away": {"id": 16804, "players": [
                {"id": 9471, "number": 1, "name": "Elmeri Purmonen"},
            ]},
        }
        with patch.object(sc.session, "get", return_value=make_response(payload)):
            roster = sc._fetch_match_roster(146949)

        assert roster == {
            16798: {1: "Konsta Piironen", 2: "Niko Korhonen"},
            16804: {1: "Elmeri Purmonen"},
        }

    def test_request_failure_returns_empty_dict(self, sc):
        with patch.object(sc.session, "get", side_effect=requests.exceptions.Timeout):
            assert sc._fetch_match_roster(146949) == {}

    def test_malformed_response_returns_empty_dict(self, sc):
        with patch.object(sc.session, "get", return_value=make_response(["not", "a", "dict"])):
            assert sc._fetch_match_roster(146949) == {}

    def test_missing_players_key_returns_empty_roster_for_that_team(self, sc):
        payload = {"home": {"id": 16798}, "away": {"id": 16804, "players": []}}
        with patch.object(sc.session, "get", return_value=make_response(payload)):
            roster = sc._fetch_match_roster(146949)
        assert roster == {16798: {}, 16804: {}}


class TestPeriodEndDetection:
    """periodend detection is built directly against real data - see the
    commit history this was developed against for exact payloads."""

    def _periodend_sub_event(self, text="Ensimmäinen jakso päättyi"):
        return {
            "texts": [{"type": "event", "text": text}, {"type": "stat", "periodend": 1}],
            "runnersAtBases": [None] * 5,
        }

    def test_extracts_the_period_end_text(self, sc):
        event = match_event(1, team_id=16798, sub_events=[self._periodend_sub_event()])
        assert sc._extract_period_end_text(event) == "Ensimmäinen jakso päättyi"

    def test_second_period_text(self, sc):
        event = match_event(1, team_id=16798, sub_events=[
            self._periodend_sub_event("Toinen jakso päättyi"),
        ])
        assert sc._extract_period_end_text(event) == "Toinen jakso päättyi"

    def test_supervuoro_text(self, sc):
        event = match_event(1, team_id=16798, sub_events=[
            self._periodend_sub_event("Supervuoro päättyi"),
        ])
        assert sc._extract_period_end_text(event) == "Supervuoro päättyi"

    def test_match_end_is_not_mistaken_for_period_end(self, sc):
        # "Ottelu päättyi" uses a different stat key ("match-ended"), not
        # "periodend" - confirmed live these never overlap.
        event = match_event(1, team_id=16798, sub_events=[
            {"texts": [{"type": "event", "text": "Ottelu päättyi"},
                       {"type": "stat", "match-ended": "2026-08-25T19:33:00+03:00"}]},
        ])
        assert sc._extract_period_end_text(event) is None

    def test_regular_run_is_not_mistaken_for_period_end(self, sc):
        event = match_event(1, team_id=16798, sub_events=[run_sub_event(1, 16798)])
        assert sc._extract_period_end_text(event) is None

    def test_no_events_returns_none(self, sc):
        event = match_event(1, team_id=16798, sub_events=[])
        assert sc._extract_period_end_text(event) is None


class TestFormatPeriodEnd:
    def test_format(self, sc):
        msg = sc._format_period_end("Ensimmäinen jakso päättyi", "Home", "Away", 4, 2)
        assert msg.startswith(sc.PERIOD_END_PREFIX)
        assert "Ensimmäinen jakso päättyi" in msg
        assert "Home 4-2 Away" in msg


class TestFormatFinal:
    """Real pesäpallo results are headlined by jaksovoitot (periods won),
    not total runs - matches pesistulokset.fi's own result_string shape,
    e.g. "1-0k (0-0, 0-0, 0-0, 2-1)"."""

    def test_matches_the_requested_format(self, sc):
        live = {
            "periods": {"home": 1, "away": 0},
            "runs": [{"home": [4], "away": [2]}, {"home": [2], "away": [2]}],
        }
        result = sc._format_final(live, "Joensuun Maila", "Sotkamon Jymy")
        assert result == "Joensuun Maila - Sotkamon Jymy 1 - 0 (4 - 2, 2 - 2)"

    def test_unplayed_periods_are_omitted_from_the_breakdown(self, sc):
        live = {
            "periods": {"home": 1, "away": 0},
            "runs": [
                {"home": [4], "away": [2]},
                {"home": [2], "away": [2]},
                {"home": [None], "away": [None]},  # supervuoro - never needed
                {"home": [None], "away": [None]},  # kotiutuslyöntikilpailu - never needed
            ],
        }
        result = sc._format_final(live, "Home", "Away")
        assert result == "Home - Away 1 - 0 (4 - 2, 2 - 2)"

    def test_includes_a_played_tiebreak(self, sc):
        live = {
            "periods": {"home": 1, "away": 0},
            "runs": [
                {"home": [0], "away": [0]},
                {"home": [0], "away": [0]},
                {"home": [0], "away": [0]},
                {"home": [2], "away": [1]},  # kotiutuslyöntikilpailu decided it
            ],
        }
        result = sc._format_final(live, "Home", "Away")
        assert result == "Home - Away 1 - 0 (0 - 0, 0 - 0, 0 - 0, 2 - 1)"

    def test_missing_periods_field_falls_back_gracefully(self, sc):
        live = {"runs": [{"home": [4], "away": [2]}]}
        result = sc._format_final(live, "Home", "Away")
        assert result == "Home - Away ? - ? (4 - 2)"

    def test_no_runs_data_omits_the_breakdown_entirely(self, sc):
        live = {"periods": {"home": 1, "away": 0}}
        result = sc._format_final(live, "Home", "Away")
        assert result == "Home - Away 1 - 0"


class TestSumRuns:
    def test_sums_across_periods(self, sc):
        live = {"runs": [{"home": [2, 0], "away": [1]}, {"home": [3], "away": [0, 1]}]}
        assert sc._sum_runs(live, "home") == 5
        assert sc._sum_runs(live, "away") == 2

    def test_missing_runs_returns_none(self, sc):
        assert sc._sum_runs({}, "home") is None

    def test_malformed_runs_returns_none(self, sc):
        assert sc._sum_runs({"runs": "not a list"}, "home") is None


class TestPeriodRuns:
    """Regression coverage: pesäpallo scores each jakso independently
    (confirmed live via the API's own runs_home_first_period/
    second_period/etc split), so RUN:/JAKSO: must use only the current
    period's own tally, not a match-wide cumulative sum like _sum_runs()."""

    LIVE = {"runs": [
        {"home": [0, 0, 2, 0], "away": [0, 1, 1, 0]},   # jakso 1: 2-2
        {"home": [3, 0, 6, None], "away": [0, 0, None, None]},  # jakso 2 (in progress): 9-0
    ]}

    def test_scoped_to_a_single_period(self, sc):
        assert sc._period_runs(self.LIVE, "home", 0) == 2
        assert sc._period_runs(self.LIVE, "away", 0) == 2

    def test_different_period_gives_a_different_total(self, sc):
        # This is the actual bug: summing (_sum_runs) would give 11 for
        # home here (2+9), not jakso 2's own 9.
        assert sc._period_runs(self.LIVE, "home", 1) == 9
        assert sc._period_runs(self.LIVE, "away", 1) == 0

    def test_none_values_in_the_period_are_ignored(self, sc):
        # jakso 2's arrays contain None for innings not yet played.
        assert sc._period_runs(self.LIVE, "home", 1) == 9

    def test_none_period_index_returns_none(self, sc):
        assert sc._period_runs(self.LIVE, "home", None) is None

    def test_out_of_range_period_index_returns_none(self, sc):
        assert sc._period_runs(self.LIVE, "home", 5) is None
        assert sc._period_runs(self.LIVE, "home", -1) is None

    def test_missing_runs_returns_none(self, sc):
        assert sc._period_runs({}, "home", 0) is None


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
        # period=1 is the *second* period - the feed is 0-indexed
        # (confirmed live: "Ensimmäinen jakso päättyi" carries period=0).
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 3, 1, 16802, 16802, "Santtu Patova")
        assert msg.startswith(sc.RUN_PREFIX)
        assert "Home — Santtu Patova" in msg
        assert "Home 3-1 Away" in msg
        assert "(2. jakso)" in msg

    def test_away_team_scoring(self, sc):
        event = {"period": 2}
        msg = sc._format_run(event, "Home", "Away", 1, 2, 999, 16802, "Someone")
        assert "Away — Someone" in msg

    def test_unknown_scorer_name(self, sc):
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, None)
        assert "Unknown" in msg

    def test_period_zero_is_first_period_not_omitted(self, sc):
        # Regression test: period=0 is a real, meaningful value (1st
        # period) - must not be treated as "no period" just because it's
        # falsy in Python.
        event = {"period": 0}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(1. jakso)" in msg

    def test_period_two_is_supervuoro(self, sc):
        event = {"period": 2}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(supervuoro)" in msg

    def test_period_three_is_kotiutuslyontikilpailu(self, sc):
        event = {"period": 3}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(kotiutuslyöntikilpailu)" in msg

    def test_missing_period_has_no_label(self, sc):
        event = {}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "jakso" not in msg

    def test_includes_batter_when_different_from_scorer(self, sc):
        # Lyöjä (batter) before etenijä (scorer), matching pesistulokset.fi's
        # own column order.
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "Konsta Piironen", "Joosua Rättö")
        assert "Joosua Rättö → Konsta Piironen" in msg

    def test_omits_batter_when_same_as_scorer(self, sc):
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "Same Person", "Same Person")
        assert "lyöjä" not in msg

    def test_omits_batter_when_unresolved(self, sc):
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "Scorer", None)
        assert "lyöjä" not in msg


class TestProcessMatch:
    def _prev(self, **overrides):
        prev = {
            "match_id": 146953,
            "home_id": 16802,
            "away_id": 16796,
            "home_name": "Manse PP",
            "away_name": "Hyvinkään Tahko",
            "period": 0,
            "period_home_runs": 0,
            "period_away_runs": 0,
            "event_count": 0,
            "finished": False,
            "roster": {},
        }
        prev.update(overrides)
        return prev

    def test_number_based_scorer_is_resolved_via_roster(self, sc):
        # Regression test for the real bug: a jersey-number-only player
        # ref must resolve through the match's roster, not get treated
        # as a global player id (which resolved to a real but completely
        # unrelated person in production).
        bot = MagicMock()
        roster = {16798: {1: "Konsta Piironen"}}
        prev = self._prev(match_id=146949, home_id=16798, away_id=16804, roster=roster)
        match = make_match(mid=146949, home_id=16798, away_id=16804)
        events = [match_event(1, team_id=16798, sub_events=[run_sub_event_by_number(1, 16798)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name") as mock_global_lookup:
            sc._process_match(bot, "#pesis.fi", match, prev)

        mock_global_lookup.assert_not_called()  # must never treat "1" as a global id
        message = bot.send_message.call_args[0][1]
        assert "Konsta Piironen" in message

    def test_batter_and_scorer_both_shown_when_different(self, sc):
        bot = MagicMock()
        roster = {16798: {1: "Konsta Piironen", 4: "Joosua Rättö"}}
        prev = self._prev(match_id=146949, home_id=16798, away_id=16804, roster=roster)
        match = make_match(mid=146949, home_id=16798, away_id=16804)
        events = [match_event(
            1, team_id=16798, batter=4,
            sub_events=[run_sub_event_by_number(1, 16798)],
        )]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            sc._process_match(bot, "#pesis.fi", match, prev)

        message = bot.send_message.call_args[0][1]
        assert "Joosua Rättö → Konsta Piironen" in message

    def test_real_match_146950_reproduces_all_three_reported_runs(self, sc):
        # End-to-end regression test built directly from the real incident
        # report: three runs from pesistulokset.fi's own match page
        # (kunnari, regular hit, wild throw), only the middle one detected
        # (and with the wrong score) before this fix.
        bot = MagicMock()
        roster = {16804: {1: "Iivari Vihanto", 3: "Kalle Kuosmanen", 10: "Roope Korhonen", 11: "Elmeri Purmonen"}}
        prev = self._prev(match_id=146950, home_id=16804, away_id=16798,
                           home_name="Sotkamon Jymy", away_name="Joensuun Maila", roster=roster)
        match = make_match(mid=146950, home_id=16804, away_id=16798,
                            home="Sotkamon Jymy", away="Joensuun Maila")
        events = [
            match_event(1, team_id=16804, batter=1, sub_events=[
                {"texts": [{"team": 16804, "type": "player", "number": 1}, "jätettiin välistä"]},
                {"texts": [{"team": 16804, "type": "player", "number": 1},
                           {"type": "event", "text": "eteni", "base": 0}, "ykköspesälle"]},
                {"texts": [{"team": 16804, "type": "player", "number": 1},
                           {"type": "event", "text": "eteni", "base": 1}, "kakkospesälle"]},
                {"texts": [{"team": 16804, "type": "player", "number": 1},
                           {"type": "event", "text": "löi kunnarin!", "base": 2},
                           {"type": "stat", "homerun": 2}]},
            ]),
            match_event(2, team_id=16804, batter=10, sub_events=[
                run_sub_event_by_number(3, 16804),
            ]),
            match_event(3, team_id=16804, batter=7, sub_events=[
                {"texts": [{"team": 16804, "type": "player", "number": 11},
                           {"type": "event", "text": "eteni harhaheitolla", "base": 3},
                           "kotipesään", {"type": "stat", "wtscore": 3}]},
            ]),
        ]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            sc._process_match(bot, "#pesis.fi", match, prev)

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        assert len(messages) == 3
        assert "Iivari Vihanto | Sotkamon Jymy 1-0 Joensuun Maila" in messages[0]
        assert "lyöjä" not in messages[0]  # batter == scorer for the kunnari
        assert "Roope Korhonen → Kalle Kuosmanen | Sotkamon Jymy 2-0 Joensuun Maila" in messages[1]
        assert "Harhaheitto → Elmeri Purmonen | Sotkamon Jymy 3-0 Joensuun Maila" in messages[2]

    def test_real_match_147206_duplicated_segment_is_not_double_counted(self, sc):
        # Regression test for a real incident, confirmed via a diagnostic
        # log line ("home score snap ... 4 -> 3") and independently by
        # re-fetching the match's actual event feed: a whole segment of
        # already-counted plays was re-appended later in the events array
        # at different positions, and one of those got counted a second
        # time - a real run scored 3 times over showed up as 4.
        bot = MagicMock()
        roster = {16804: {1: "Iivari Vihanto", 4: "Hannes Pekkinen"}}
        prev = self._prev(match_id=147206, home_id=16804, away_id=16801,
                           home_name="Sotkamon Jymy", away_name="Kouvolan Pallonlyöjät",
                           roster=roster, period_home_runs=2, period_away_runs=1)
        match = make_match(mid=147206, home_id=16804, away_id=16801,
                            home="Sotkamon Jymy", away="Kouvolan Pallonlyöjät")

        # The real run: Hannes Pekkinen (batter) -> Iivari Vihanto (scorer).
        real_run_sub_event = {
            "texts": [{"team": 16804, "type": "player", "number": 1},
                      {"type": "event", "text": "eteni", "base": 3}, "kotipesään",
                      {"type": "stat", "score": 3}],
            "runnersAtBases": [None, None, None, None, 1],
        }
        # Same events array the whole match sees in one poll: the real
        # play, then (much later, different array position, but byte-
        # identical content) the server re-appending that same play.
        events = [
            match_event(1, team_id=16804, batter=4, period=0, sub_events=[real_run_sub_event]),
            match_event(2, team_id=16804, batter=99, period=0, sub_events=[  # unrelated in between
                {"texts": ["1. lyönti", {"type": "hit", "hit": None}]},
            ]),
            match_event(3, team_id=16804, batter=4, period=0, sub_events=[real_run_sub_event]),  # re-appended
        ]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        assert len(messages) == 1  # not 2
        assert "Sotkamon Jymy 3-1 Kouvolan Pallonlyöjät" in messages[0]  # not 4-1
        assert new_state["period_home_runs"] == 3

    def test_real_match_147206_runs_appended_to_an_already_seen_event_are_not_dropped(self, sc):
        # Regression test for a real incident (pesistulokset.fi match
        # 147206, "2. jakso"): a batter's whole turn is ONE outer event at
        # a fixed array position, but its own "events" sub-array grows in
        # place over several polls as the play develops (confirmed live:
        # the same object's "updated" timestamp kept changing while its
        # array index didn't). The old event_count-based slicing marked
        # that position "seen" the first time it crossed the boundary -
        # even with zero runs in it yet - so three later-appended runs
        # (two runners driven in, then the batter's own "löi kunnarin!")
        # were silently dropped, and the next real run's local counter
        # ended up one too high (announced "6-0" for what
        # pesistulokset.fi's own page showed as "5-0") because an
        # authoritative-score snap had already silently absorbed the
        # missing growth. _process_match must now rescan the full events
        # array every poll (relying on seen_run_signatures, not position)
        # so growth appended to an already-"seen" event still gets picked
        # up.
        bot = MagicMock()
        roster = {16804: {2: "Jere Vikström", 3: "Kalle Kuosmanen", 4: "Hannes Pekkinen", 10: "Roope Korhonen",
                           11: "Elmeri Purmonen"}}
        # One real batter's turn (id=14 in the actual feed): three
        # separate runners reach "kotipesään" inside it - #2, #3, then
        # #4 (the batter himself) via "löi kunnarin!" - interleaved with
        # non-scoring "eteni ... kolmospesälle/kakkospesälle" advances,
        # exactly as pesistulokset.fi's own play-by-play recorded it.
        growing_event = match_event(14, team_id=16804, batter=4, period=1, sub_events=[
            {"texts": ["3. lyönti", {"type": "hit", "hit": {"out": False}}]},
        ])
        # First poll: this event has only just started (no runs in it
        # yet) - the old code would mark this array position "seen" here.
        prev = self._prev(match_id=147206, home_id=16804, away_id=16801,
                           home_name="Sotkamon Jymy", away_name="Kouvolan Pallonlyöjät",
                           roster=roster, period=1, period_home_runs=1, period_away_runs=0)
        with patch.object(sc, "_fetch_match_events", return_value=[growing_event]):
            prev = sc._process_match(bot, "#pesis.fi", match_placeholder := make_match(
                mid=147206, home_id=16804, away_id=16801,
                home="Sotkamon Jymy", away="Kouvolan Pallonlyöjät",
            ), prev)
        assert bot.send_message.call_count == 0  # no runs yet, nothing to announce

        # Second poll: the SAME event (same array position) has grown to
        # include all three runs, and a genuinely new event (Roope
        # Korhonen driving in Elmeri Purmonen) has also appeared.
        grown_event = match_event(14, team_id=16804, batter=4, period=1, sub_events=[
            {"texts": ["3. lyönti", {"type": "hit", "hit": {"out": False}}]},
            {"texts": [{"team": 16804, "type": "player", "number": 2},
                       {"type": "event", "text": "eteni", "base": 2}, "kolmospesälle"]},
            {"texts": [{"team": 16804, "type": "player", "number": 2},
                       {"type": "event", "text": "eteni", "base": 3}, "kotipesään",
                       {"type": "stat", "score": 3}]},
            {"texts": [{"team": 16804, "type": "player", "number": 3},
                       {"type": "event", "text": "eteni", "base": 2}, "kolmospesälle"]},
            {"texts": [{"team": 16804, "type": "player", "number": 3},
                       {"type": "event", "text": "eteni", "base": 3}, "kotipesään",
                       {"type": "stat", "score": 3}]},
            {"texts": [{"team": 16804, "type": "player", "number": 4},
                       {"type": "event", "text": "löi kunnarin!", "base": 2},
                       {"type": "stat", "homerun": 2}]},
        ])
        roope_event = match_event(15, team_id=16804, batter=10, period=1, sub_events=[
            run_sub_event_by_number(11, 16804),
        ])
        match = make_match(mid=147206, home_id=16804, away_id=16801,
                            home="Sotkamon Jymy", away="Kouvolan Pallonlyöjät")

        with patch.object(sc, "_fetch_match_events", return_value=[grown_event, roope_event]):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        assert len(messages) == 4  # Jere Vikström, Kalle Kuosmanen, Hannes Pekkinen's kunnari, Roope->Elmeri
        assert "Hannes Pekkinen → Jere Vikström | Sotkamon Jymy 2-0" in messages[0]
        assert "Hannes Pekkinen → Kalle Kuosmanen | Sotkamon Jymy 3-0" in messages[1]
        assert "Hannes Pekkinen | Sotkamon Jymy 4-0" in messages[2]  # kunnari: batter == scorer
        # The real incident report: this line showed "6-0" before this
        # fix (double-counted on top of a snap that had silently absorbed
        # the three dropped runs above) instead of the real "5-0".
        assert "Sotkamon Jymy 5-0 Kouvolan Pallonlyöjät" in messages[3]
        assert new_state["period_home_runs"] == 5

    def test_real_match_147201_a_run_that_would_exceed_the_authoritative_total_is_suppressed(self, sc):
        # Regression test for a real incident (pesistulokset.fi match
        # 147201, "1. jakso" and "2. jakso" both affected): pesistulokset.fi
        # apparently retracts and reissues a play mid-game (confirmed live:
        # the real "4. lopettava" run ended up announced twice, first as
        # "Perttu Ruuska -> Aapo Komulainen" then, minutes later, as the
        # actually-correct "Jukka-Pekka Vainionpää -> Aapo Komulainen" -
        # same real point, two different batters), which produces a new
        # content signature our dedup can't recognize as "already seen".
        # Without a cap, that shows an impossible score (higher than
        # pesistulokset.fi's own authoritative period total - the real
        # period ended 6-5, but the bot announced "6-6"). The authoritative
        # total is always eventually correct (confirmed against this same
        # match's real final result: "6 - 5, 6 - 5"), so once the local
        # count already matches it, one more detected run for that side
        # must be suppressed rather than shown.
        bot = MagicMock()
        roster = {16802: {10: "Perttu Ruuska", 11: "Jukka-Pekka Vainionpää", 5: "Aapo Komulainen"}}
        # Local count is already at the real, authoritative period total
        # (5) after the first ten real runs - matches the true incident
        # timeline (the ghost run was the eleventh and last of the period).
        prev = self._prev(match_id=147201, home_id=16805, away_id=16802,
                           home_name="Vimpelin Veto", away_name="Manse PP, Tampere",
                           roster=roster, period=0, period_home_runs=6, period_away_runs=4)
        match = make_match(mid=147201, home_id=16805, away_id=16802,
                            home="Vimpelin Veto", away="Manse PP, Tampere")
        match["liveResult"]["runs"] = [{"home": [6], "away": [4]}]  # authoritative: still 6-4

        ghost_run = match_event(50, team_id=16802, batter=10, period=0, sub_events=[
            run_sub_event_by_number(5, 16802),
        ])

        with patch.object(sc, "_fetch_match_events", return_value=[ghost_run]), \
             patch("builtins.print") as mock_print:
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()  # no RUN: line for the ghost
        assert new_state["period_away_runs"] == 4  # not bumped to 5
        assert any("suppressed" in str(c.args[0]) for c in mock_print.call_args_list)

    def test_suppressed_run_is_retried_once_authoritative_catches_up(self, sc):
        # A suppressed signature is deliberately NOT marked "seen" - if
        # the suppression was instead just the authoritative source
        # lagging a poll behind a genuinely new run, the next poll (once
        # authoritative rises to match) must still count and announce it,
        # not lose it forever.
        bot = MagicMock()
        roster = {16802: {10: "Perttu Ruuska", 5: "Aapo Komulainen"}}
        run_event = match_event(50, team_id=16802, batter=10, period=0, sub_events=[
            run_sub_event_by_number(5, 16802),
        ])

        # First poll: authoritative hasn't caught up yet - suppressed.
        prev = self._prev(match_id=147201, home_id=16805, away_id=16802,
                           roster=roster, period=0, period_home_runs=0, period_away_runs=4)
        match = make_match(mid=147201, home_id=16805, away_id=16802)
        match["liveResult"]["runs"] = [{"home": [0], "away": [4]}]
        with patch.object(sc, "_fetch_match_events", return_value=[run_event]):
            prev = sc._process_match(bot, "#pesis.fi", match, prev)
        assert bot.send_message.call_count == 0
        assert prev["period_away_runs"] == 4

        # Second poll: authoritative has now risen to 5 - the exact same
        # signature (still present in the events array) must be picked up.
        match["liveResult"]["runs"] = [{"home": [0], "away": [5]}]
        with patch.object(sc, "_fetch_match_events", return_value=[run_event]):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_called_once()
        assert new_state["period_away_runs"] == 5

    def test_real_match_147207_a_late_run_for_an_earlier_period_does_not_reset_the_active_tally(self, sc):
        # Regression test for a real incident (pesistulokset.fi match
        # 147207): a run event tagged period=0 (jakso 1) showed up in the
        # feed well after jakso 1's own JAKSO: end had already been
        # announced and jakso 2 was already in progress - a correction
        # appended out of period order (confirmed live: the settled
        # events array itself is cleanly period-ordered, so this only
        # shows up mid-poll, not in a post-game fetch). The old behavior
        # reset the active tally to 0 whenever a scanned event's period
        # differed from the current one, wiping out jakso 2's real
        # running score and restarting it from 0 instead of only ever
        # advancing it.
        bot = MagicMock()
        roster = {16804: {2: "Aapo Hiltunen", 3: "Iivari Vihanto"}}
        prev = self._prev(match_id=147207, home_id=16801, away_id=16804,
                           home_name="Kouvolan Pallonlyöjät", away_name="Sotkamon Jymy",
                           roster=roster, period=1, period_home_runs=1, period_away_runs=1)
        match = make_match(mid=147207, home_id=16801, away_id=16804,
                            home="Kouvolan Pallonlyöjät", away="Sotkamon Jymy")
        match["liveResult"]["runs"] = [{"home": [2], "away": [2]}, {"home": [1], "away": [1]}]

        late_jakso1_run = match_event(99, team_id=16804, batter=2, period=0, sub_events=[
            run_sub_event_by_number(3, 16804),
        ])

        with patch.object(sc, "_fetch_match_events", return_value=[late_jakso1_run]):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        message = bot.send_message.call_args[0][1]
        assert "(1. jakso)" in message  # labeled with its own real period
        # Scoped to jakso 1's own (previously-untouched) tally, not
        # jakso 2's running score.
        assert "Kouvolan Pallonlyöjät 0-1 Sotkamon Jymy" in message
        # jakso 2 (the active period) is completely untouched.
        assert new_state["period"] == 1
        assert new_state["period_home_runs"] == 1
        assert new_state["period_away_runs"] == 1
        assert new_state["past_period_runs"] == {0: {"home": 0, "away": 1}}

    def test_roster_is_carried_forward_unchanged(self, sc):
        bot = MagicMock()
        roster = {16802: {1: "A"}, 16796: {1: "B"}}
        prev = self._prev(roster=roster)
        match = make_match(mid=146953, home_id=16802, away_id=16796)

        with patch.object(sc, "_fetch_match_events", return_value=None):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["roster"] == roster

    def test_period_end_is_announced(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=16802, period=0, sub_events=[
            {"texts": [{"type": "event", "text": "Ensimmäinen jakso päättyi"},
                       {"type": "stat", "periodend": 1}]},
        ])]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            sc._process_match(bot, "#pesis.fi", match, prev)

        message = bot.send_message.call_args[0][1]
        assert message.startswith(sc.PERIOD_END_PREFIX)
        assert "Ensimmäinen jakso päättyi" in message

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

    def test_score_resets_across_a_period_boundary(self, sc):
        # The actual reported bug: a run in the 2nd period was showing
        # the match-wide cumulative score (e.g. 4-3, continuing from
        # jakso 1's 4-2) instead of restarting at 0 for the new period.
        bot = MagicMock()
        prev = self._prev(event_count=0, period=0, period_home_runs=4, period_away_runs=2)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=16796, period=1, sub_events=[run_sub_event(111, 16796)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            sc._process_match(bot, "#pesis.fi", match, prev)

        message = bot.send_message.call_args[0][1]
        assert "Manse PP 0-1 Hyvinkään Tahko" in message  # not 4-3
        assert "(2. jakso)" in message

    def test_period_field_updates_when_the_period_changes(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0, period=0, period_home_runs=4, period_away_runs=2)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=16796, period=1, sub_events=[run_sub_event(111, 16796)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["period"] == 1
        assert new_state["period_home_runs"] == 0
        assert new_state["period_away_runs"] == 1

    def test_period_end_uses_the_ending_periods_own_score_not_reset(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0, period=0, period_home_runs=0, period_away_runs=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [
            match_event(1, team_id=16802, period=0, sub_events=[run_sub_event(111, 16802)]),
            match_event(2, team_id=16802, period=0, sub_events=[
                {"texts": [{"type": "event", "text": "Ensimmäinen jakso päättyi"},
                           {"type": "stat", "periodend": 1}]},
            ]),
        ]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            sc._process_match(bot, "#pesis.fi", match, prev)

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        period_end_msg = next(m for m in messages if m.startswith(sc.PERIOD_END_PREFIX))
        assert "Manse PP 1-0 Hyvinkään Tahko" in period_end_msg

    def test_score_snaps_to_authoritative_total(self, sc):
        bot = MagicMock()
        # Our own counting would only get to 1, but the authoritative
        # liveResult says 3 - the final state must reflect the latter.
        prev = self._prev(event_count=0, period=0, period_home_runs=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=3, away_runs=0)
        events = [match_event(1, team_id=16802, period=0, sub_events=[run_sub_event(111, 16802)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["period_home_runs"] == 3

    def test_no_new_events_sends_nothing(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=5)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=0, away_runs=0)

        with patch.object(sc, "_fetch_match_events", return_value=[{"id": i} for i in range(5)]):
            sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()

    def test_finish_transition_sends_final(self, sc):
        bot = MagicMock()
        # FINAL is headlined by jaksovoitot (periods won) with a
        # per-period run breakdown, unlike RUN/JAKSO which are scoped to
        # the current period - see TestFormatFinal for the format itself.
        prev = self._prev(event_count=0, finished=False)
        match = make_match(mid=146953, home_id=16802, away_id=16796, finished=True)
        match["liveResult"]["runs"] = [{"home": [2], "away": [1]}, {"home": [0], "away": [0]}]
        match["liveResult"]["periods"] = {"home": 1, "away": 0}

        with patch.object(sc, "_fetch_match_events", return_value=None):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["finished"] is True
        message = bot.send_message.call_args[0][1]
        assert message.startswith(sc.FINAL_PREFIX)
        assert "Manse PP - Hyvinkään Tahko 1 - 0 (2 - 1, 0 - 0)" in message

    def test_already_finished_does_not_resend_final(self, sc):
        bot = MagicMock()
        prev = self._prev(finished=True, period_home_runs=2, period_away_runs=1)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=2, away_runs=1, finished=True)

        with patch.object(sc, "_fetch_match_events", return_value=None):
            sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()

    def test_already_finished_match_is_never_touched_again(self, sc):
        # Regression test for a real incident: the event feed kept
        # appending events (apparent corrections) well after "Ottelu
        # päättyi" had already fired and FINAL: had already been sent,
        # producing contradictory RUN:/JAKSO: lines for a finished match.
        # Once finished, a match must be skipped entirely - not just have
        # its messages suppressed - so no amount of new "corrected" data
        # can produce any announcement for it again.
        bot = MagicMock()
        prev = self._prev(finished=True, period_home_runs=2, period_away_runs=1)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=2, away_runs=1, finished=True)

        with patch.object(sc, "_fetch_match_events") as mock_fetch_events:
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        mock_fetch_events.assert_not_called()
        bot.send_message.assert_not_called()
        assert new_state is prev

    def test_events_fetch_failure_does_not_crash(self, sc):
        bot = MagicMock()
        prev = self._prev()
        match = make_match(mid=146953, home_id=16802, away_id=16796)

        with patch.object(sc, "_fetch_match_events", return_value=None):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)  # must not raise

        assert new_state["event_count"] == prev["event_count"]

    def test_event_count_baseline_never_regresses(self, sc):
        # Regression test for a real incident: a transiently shorter
        # events array on one poll (e.g. a flaky/incomplete API response)
        # must not lower the stored baseline - otherwise a later poll,
        # once the array recovers, re-slices already-announced events as
        # "new" and re-sends them. Observed live as an exact duplicate
        # RUN: message with the score inflated for every play after it,
        # until the next period-end self-correction.
        bot = MagicMock()
        prev = self._prev(event_count=10)
        match = make_match(mid=146953, home_id=16802, away_id=16796)

        # A poll that (for whatever reason) sees fewer events than the
        # stored baseline - must not shrink event_count below 10.
        with patch.object(sc, "_fetch_match_events", return_value=[{"id": i} for i in range(3)]):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["event_count"] == 10
        bot.send_message.assert_not_called()

    def test_run_not_reannounced_after_a_transient_array_shrink(self, sc):
        # Full reproduction of the reported sequence: poll 1 processes a
        # run; poll 2 (transiently) sees a shorter array than poll 1 did;
        # poll 3 sees the array back to its full (or longer) length.
        # Without the monotonic guard, poll 3 would re-slice and
        # re-announce the run poll 1 already sent.
        bot = MagicMock()
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        run_event = match_event(1, team_id=16802, period=0, sub_events=[run_sub_event(111, 16802)])

        prev = self._prev(event_count=0, period=0)
        with patch.object(sc, "_fetch_match_events", return_value=[run_event]), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            prev = sc._process_match(bot, "#pesis.fi", match, prev)
        assert prev["event_count"] == 1
        assert bot.send_message.call_count == 1

        # Poll 2: transiently shorter (e.g. flaky response) - must not
        # crash or lower the baseline, and must not re-announce anything.
        with patch.object(sc, "_fetch_match_events", return_value=[]):
            prev = sc._process_match(bot, "#pesis.fi", match, prev)
        assert prev["event_count"] == 1
        assert bot.send_message.call_count == 1  # unchanged

        # Poll 3: array "recovers" to the same single event again - must
        # not be re-sliced as new since the baseline never regressed.
        with patch.object(sc, "_fetch_match_events", return_value=[run_event]), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            prev = sc._process_match(bot, "#pesis.fi", match, prev)

        assert bot.send_message.call_count == 1  # still just the one real send

    def test_run_by_scoring_team_not_home_or_away_is_ignored(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=99999, sub_events=[run_sub_event(1, 99999)])]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()
        assert new_state["period_home_runs"] == 0
        assert new_state["period_away_runs"] == 0


class TestPollLoop:
    def test_unexpected_poll_once_failure_logs_a_traceback_and_continues(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()

        def fail_once(*args, **kwargs):
            stop_event.set()  # let the loop exit after this one iteration
            raise RuntimeError("boom")

        with patch.object(sc, "_poll_once", side_effect=fail_once), \
             patch("traceback.print_exc") as mock_print_exc:
            sc._poll_loop(bot, "#pesis.fi", stop_event, 2945)  # must not raise

        mock_print_exc.assert_called_once()


class TestPollOnce:
    def _seed(self, sc, channel, matches_state):
        sc._channels[channel] = {"stop_event": MagicMock(), "thread": None, "matches": matches_state}

    def test_unexpected_process_match_failure_logs_a_traceback(self, sc):
        # The catch-all around _process_match is the last line of defense
        # against anything not anticipated by a narrower handler - it
        # must print a full traceback, not just str(e), since that's
        # often the only way to pinpoint where a genuinely new bug broke.
        bot = MagicMock()
        self._seed(sc, "#pesis.fi", {
            146953: {"match_id": 146953, "home_id": 1, "away_id": 2, "home_name": "A", "away_name": "B",
                     "period": 0, "period_home_runs": 0, "period_away_runs": 0, "event_count": 0, "finished": False},
        })
        match = make_match(mid=146953, home_id=1, away_id=2, finished=False)

        with patch.object(sc, "_fetch_today_matches", return_value={146953: match}), \
             patch.object(sc, "_process_match", side_effect=KeyError("boom")), \
             patch("traceback.print_exc") as mock_print_exc:
            result = sc._poll_once(bot, "#pesis.fi", 2945)  # must not raise

        assert result is False
        mock_print_exc.assert_called_once()

    def test_all_finished_returns_true(self, sc):
        bot = MagicMock()
        self._seed(sc, "#pesis.fi", {
            146953: {"match_id": 146953, "home_id": 1, "away_id": 2, "home_name": "A", "away_name": "B",
                     "period": 0, "period_home_runs": 1, "period_away_runs": 0, "event_count": 0, "finished": True},
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
                     "period": 0, "period_home_runs": 0, "period_away_runs": 0, "event_count": 0, "finished": False},
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
                          "period": 0, "period_home_runs": 0, "period_away_runs": 0, "event_count": 0, "finished": False}
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
