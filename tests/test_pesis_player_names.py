from unittest.mock import patch

import requests

from tests.conftest import make_json_response as make_response
from tests.pesis_test_helpers import sc


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


