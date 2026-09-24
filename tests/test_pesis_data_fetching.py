import datetime
import time
from unittest.mock import patch

import requests

from pesis_command import SuperpesisCommand
from tests.conftest import make_json_response as make_response
from tests.pesis_test_helpers import series_list_payload, make_match, matches_list_payload, sc


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


class TestSeriesResolution:
    def test_finds_mens_superpesis_in_latest_season(self, sc):
        # 2945 not 2946 ("Naisten Superpesis" - same level name, different
        # series name) or 2810 (last season) - series_list_payload()'s
        # entries also have no "shortcut" key at all, so this doubles as
        # coverage for falling back to the first match when none has one.
        with patch.object(sc.session, "get", return_value=make_response(series_list_payload())):
            series_id = sc._resolve_series_id()
        assert series_id == 2945

    def test_prefers_the_shortcut_entry_when_level_series_matches_more_than_one(self, sc):
        # Regression test built from real data (Ykköspesis): the same
        # level/series pair can match more than one seasonSeries - the
        # real league plus unrelated same-category tournaments (e.g.
        # "Talven harjoitusotteluita"). "shortcut": true reliably marks
        # the real league - confirmed live for both leagues configured so
        # far - and must be preferred even when a non-league entry sorts
        # first in the array.
        payload = {"seasons": [{"season": {"id": 110, "season": 2026}, "seasonSerieses": [
            {"seasonSeries": {"id": 3050, "name": "Talven harjoitusotteluita", "shortcut": False},
             "level": {"name": "Ykköspesis"}, "series": {"name": "Miehet"}},
            {"seasonSeries": {"id": 2954, "name": "Miesten Ykköspesis", "shortcut": True},
             "level": {"name": "Ykköspesis"}, "series": {"name": "Miehet"}},
        ]}]}
        sc.SERIES_LEVEL_NAME = "Ykköspesis"  # this fixture's `sc` is a SuperpesisCommand
        with patch.object(sc.session, "get", return_value=make_response(payload)):
            series_id = sc._resolve_series_id()
        assert series_id == 2954  # not 3050, even though it sorts first

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
        fresh_instance.SERIES_CACHE_FILE = sc.SERIES_CACHE_FILE  # same isolated tmp file as `sc`
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
        fresh_instance.SERIES_CACHE_FILE = sc.SERIES_CACHE_FILE  # same isolated tmp file as `sc`
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

    def test_cache_write_failure_does_not_break_the_lookup(self, sc):
        # A disk write failure (e.g. read-only filesystem) shouldn't stop
        # the resolved id from being returned - it just means this run's
        # cache stays in-memory-only instead of also surviving a restart.
        sc.SERIES_CACHE_FILE = "/nonexistent-dir/cache.json"
        with patch.object(sc.session, "get", return_value=make_response(series_list_payload())):
            series_id = sc._resolve_series_id()
        assert series_id == 2945


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

    def test_team_missing_id_is_skipped_entirely(self, sc):
        payload = {
            "home": {"players": [{"number": 1, "name": "No Team Id"}]},  # no "id" key
            "away": {"id": 16804, "players": [{"number": 1, "name": "Elmeri Purmonen"}]},
        }
        with patch.object(sc.session, "get", return_value=make_response(payload)):
            roster = sc._fetch_match_roster(146949)
        assert roster == {16804: {1: "Elmeri Purmonen"}}

    def test_non_dict_player_entry_is_skipped_not_fatal(self, sc):
        payload = {
            "home": {"id": 16798, "players": ["not-a-dict", {"number": 1, "name": "Real Player"}]},
        }
        with patch.object(sc.session, "get", return_value=make_response(payload)):
            roster = sc._fetch_match_roster(146949)
        assert roster == {16798: {1: "Real Player"}}


