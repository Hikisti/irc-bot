import json as json_module
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.distance_command import DistanceCommand


def make_response(json_data, status_code=200, reason="Error"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = reason
    resp.json.return_value = json_data
    resp.text = json_module.dumps(json_data)
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


def geocode_payload(label, lon, lat):
    return {
        "features": [
            {"geometry": {"coordinates": [lon, lat]}, "properties": {"label": label}}
        ]
    }


def directions_payload(distance_m, duration_s):
    """The shape ORS's directions endpoint actually returns for driving-car
    (GeoJSON FeatureCollection) - confirmed against the live API."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "properties": {
                    "summary": {"distance": distance_m, "duration": duration_s},
                    "segments": [{"distance": distance_m, "duration": duration_s}],
                },
                "geometry": {"type": "LineString", "coordinates": []},
            }
        ],
    }


def legacy_directions_payload(distance_m, duration_s):
    """Older "routes" shape - kept as a fallback in the parser in case
    content negotiation ever serves it again."""
    return {"routes": [{"summary": {"distance": distance_m, "duration": duration_s}}]}


@pytest.fixture
def distance_command(monkeypatch):
    monkeypatch.setenv("ORS_API_KEY", "test-key")
    return DistanceCommand()


class TestMissingApiKey:
    def test_missing_api_key_returns_error_without_crashing(self, monkeypatch):
        monkeypatch.delenv("ORS_API_KEY", raising=False)
        with patch("src.distance_command.load_dotenv"):
            dc = DistanceCommand()
        assert "ORS_API_KEY is not set" in dc.execute("Kokkola,Vimpeli")


class TestParsing:
    def test_no_args_shows_usage(self, distance_command):
        assert "Usage:" in distance_command.execute("")

    def test_comma_separates_multi_word_cities(self, distance_command):
        with patch.object(distance_command, "_geocode") as mock_geocode, \
             patch.object(distance_command, "_driving_distance") as mock_route:
            mock_geocode.side_effect = [
                ({"label": "New York, USA", "lon": 1, "lat": 2}, None),
                ({"label": "Los Angeles, USA", "lon": 3, "lat": 4}, None),
            ]
            mock_route.return_value = ({"distance_m": 1000, "duration_s": 60}, None)

            distance_command.execute("New York, Los Angeles")

        assert mock_geocode.call_args_list[0].args[0] == "New York"
        assert mock_geocode.call_args_list[1].args[0] == "Los Angeles"

    def test_two_bare_words_are_accepted_as_single_word_cities(self, distance_command):
        with patch.object(distance_command, "_geocode") as mock_geocode, \
             patch.object(distance_command, "_driving_distance") as mock_route:
            mock_geocode.side_effect = [
                ({"label": "Kokkola, Finland", "lon": 1, "lat": 2}, None),
                ({"label": "Vimpeli, Finland", "lon": 3, "lat": 4}, None),
            ]
            mock_route.return_value = ({"distance_m": 1000, "duration_s": 60}, None)

            distance_command.execute("Kokkola Vimpeli")

        assert mock_geocode.call_args_list[0].args[0] == "Kokkola"
        assert mock_geocode.call_args_list[1].args[0] == "Vimpeli"

    def test_three_bare_words_without_comma_is_rejected(self, distance_command):
        result = distance_command.execute("New York Boston")
        assert "comma" in result.lower()

    def test_single_word_without_comma_is_rejected(self, distance_command):
        result = distance_command.execute("Kokkola")
        assert "comma" in result.lower()

    def test_trailing_comma_with_empty_second_city_is_usage_error(self, distance_command):
        result = distance_command.execute("Kokkola,")
        assert "Usage:" in result


class TestGeocoding:
    def test_city_not_found(self, distance_command):
        with patch.object(distance_command.session, "get", return_value=make_response({"features": []})):
            result = distance_command.execute("Nonexistentville,Vimpeli")
        assert "Could not find a location" in result
        assert "Nonexistentville" in result

    def test_geocode_timeout(self, distance_command):
        with patch.object(distance_command.session, "get", side_effect=requests.exceptions.Timeout):
            result = distance_command.execute("Kokkola,Vimpeli")
        assert "timed out" in result

    def test_geocode_connection_error(self, distance_command):
        with patch.object(distance_command.session, "get", side_effect=requests.exceptions.ConnectionError):
            result = distance_command.execute("Kokkola,Vimpeli")
        assert "Could not connect" in result

    def test_geocode_unauthorized(self, distance_command):
        with patch.object(distance_command.session, "get", return_value=make_response({}, status_code=401)):
            result = distance_command.execute("Kokkola,Vimpeli")
        assert "rejected the API key" in result

    def test_geocode_rate_limited(self, distance_command):
        with patch.object(distance_command.session, "get", return_value=make_response({}, status_code=429)):
            result = distance_command.execute("Kokkola,Vimpeli")
        assert "rate limit" in result.lower()


class TestAmbiguousCityDisambiguation:
    """Regression coverage for a real incident, captured directly from
    production logs: geocoding "miami" put a candidate named
    "Barranquilla" (a Colombian city, matched via some Pelias/WhosOnFirst
    alias not reflected in its own `name`/`label`) at identical
    confidence (1.0) and match_type ("exact") to the correct
    "Miami, FL, USA" candidate - so neither confidence, match_type,
    layer, nor population could distinguish them. Only the candidate's
    own `name` field actually corresponds (or doesn't) to what was
    searched for."""

    # Trimmed down real fixtures from the live "!distance austin miami"
    # incident (full properties dump from the bot's console log).
    REAL_AUSTIN_CANDIDATES = {
        "features": [
            {"geometry": {"coordinates": [-97.7431, 30.2711]},
             "properties": {"name": "Austin", "layer": "locality", "confidence": 1,
                             "match_type": "exact", "label": "Austin, TX, USA"}},
            {"geometry": {"coordinates": [-92.9746, 43.6666]},
             "properties": {"name": "Austin", "layer": "locality", "confidence": 1,
                             "match_type": "exact", "label": "Austin, MN, USA"}},
            {"geometry": {"coordinates": [-96.2716, 29.8878]},
             "properties": {"name": "Austin County", "layer": "county", "confidence": 0.4,
                             "match_type": "fallback", "label": "Austin County, TX, USA"}},
            {"geometry": {"coordinates": [-97.6412, 30.4526]},
             "properties": {"name": "Williamson", "layer": "neighbourhood", "confidence": 0.6,
                             "match_type": "fallback", "label": "Williamson, Austin, TX, USA"}},
            {"geometry": {"coordinates": [-92.3527, 35.1892]},
             "properties": {"name": "Austin", "layer": "locality", "confidence": 1,
                             "match_type": "exact", "label": "Austin, AR, USA"}},
        ]
    }
    REAL_MIAMI_CANDIDATES = {
        "features": [
            {"geometry": {"coordinates": [-74.7849130, 10.9873330]},
             "properties": {"name": "Barranquilla", "layer": "locality", "confidence": 1,
                             "match_type": "exact", "label": "Barranquilla, AT, Colombia"}},
            {"geometry": {"coordinates": [-80.1936589, 25.7616798]},
             "properties": {"name": "Miami", "layer": "locality", "confidence": 1,
                             "match_type": "exact", "label": "Miami, FL, USA"}},
            {"geometry": {"coordinates": [-94.8880385, 36.8742138]},
             "properties": {"name": "Miami", "layer": "locality", "confidence": 1,
                             "match_type": "exact", "label": "Miami, OK, USA"}},
            {"geometry": {"coordinates": [-84.2724, 39.6614]},
             "properties": {"name": "Miami Township", "layer": "localadmin", "confidence": 1,
                             "match_type": "exact", "label": "Miami Township, OH, USA"}},
            {"geometry": {"coordinates": [-84.2777, 39.1731]},
             "properties": {"name": "Miami Township", "layer": "localadmin", "confidence": 1,
                             "match_type": "exact", "label": "Miami Township, OH, USA"}},
        ]
    }

    def test_rejects_a_candidate_whose_name_does_not_match_the_query(self, distance_command):
        with patch.object(distance_command.session, "get", return_value=make_response(self.REAL_MIAMI_CANDIDATES)):
            result, error = distance_command._geocode("miami")

        assert error is None
        assert result["label"] == "Miami, FL, USA"

    def test_still_picks_correctly_for_austin(self, distance_command):
        with patch.object(distance_command.session, "get", return_value=make_response(self.REAL_AUSTIN_CANDIDATES)):
            result, error = distance_command._geocode("austin")

        assert error is None
        assert result["label"] == "Austin, TX, USA"

    def test_prefers_the_most_populous_candidate_when_population_is_available(self, distance_command):
        payload = {
            "features": [
                {"geometry": {"coordinates": [-94.88, 36.87]},
                 "properties": {"name": "Miami", "layer": "locality", "population": 13570,
                                 "label": "Miami, OK, USA"}},
                {"geometry": {"coordinates": [-80.19, 25.76]},
                 "properties": {"name": "Miami", "layer": "locality", "population": 442241,
                                 "label": "Miami, FL, USA"}},
            ]
        }
        with patch.object(distance_command.session, "get", return_value=make_response(payload)):
            result, error = distance_command._geocode("miami")

        assert error is None
        assert result["label"] == "Miami, FL, USA"

    def test_falls_back_to_first_result_when_no_candidate_name_matches(self, distance_command):
        # e.g. an abbreviation or nickname that isn't literally any
        # candidate's own name - degrade to Pelias's own top pick rather
        # than filtering everything away.
        payload = {
            "features": [
                {"geometry": {"coordinates": [1, 2]}, "properties": {"name": "Something Else", "label": "First match"}},
                {"geometry": {"coordinates": [3, 4]}, "properties": {"name": "Other Thing", "label": "Second match"}},
            ]
        }
        with patch.object(distance_command.session, "get", return_value=make_response(payload)):
            result, error = distance_command._geocode("nyc")

        assert error is None
        assert result["label"] == "First match"

    def test_name_match_is_case_insensitive(self, distance_command):
        with patch.object(distance_command.session, "get", return_value=make_response(self.REAL_MIAMI_CANDIDATES)):
            result, error = distance_command._geocode("MIAMI")

        assert error is None
        assert result["label"] == "Miami, FL, USA"

    def test_end_to_end_picks_the_famous_miami(self, distance_command):
        responses = [
            make_response(self.REAL_AUSTIN_CANDIDATES),
            make_response(self.REAL_MIAMI_CANDIDATES),
            make_response(directions_payload(2100000, 72000)),
        ]
        with patch.object(distance_command.session, "get", side_effect=responses):
            result = distance_command.execute("Austin,Miami")

        assert "Austin, TX, USA" in result
        assert "Miami, FL, USA" in result
        assert "Colombia" not in result


class TestDrivingDistance:
    def test_happy_path(self, distance_command):
        responses = [
            make_response(geocode_payload("Kokkola, Finland", 23.13, 63.84)),
            make_response(geocode_payload("Vimpeli, Finland", 23.81, 63.19)),
            make_response(directions_payload(88400, 4320)),
        ]
        with patch.object(distance_command.session, "get", side_effect=responses):
            result = distance_command.execute("Kokkola,Vimpeli")

        assert result == (
            "Driving distance from Kokkola, Finland to Vimpeli, Finland: "
            "88.4 km (54.9 mi), ~1h 12min drive."
        )

    def test_legacy_routes_shape_still_parses(self, distance_command):
        responses = [
            make_response(geocode_payload("Kokkola, Finland", 23.13, 63.84)),
            make_response(geocode_payload("Vimpeli, Finland", 23.81, 63.19)),
            make_response(legacy_directions_payload(88400, 4320)),
        ]
        with patch.object(distance_command.session, "get", side_effect=responses):
            result = distance_command.execute("Kokkola,Vimpeli")

        assert "88.4 km" in result

    def test_accept_header_is_not_overridden(self, distance_command):
        """Regression test: ORS's directions endpoint 406s on a bare
        "application/json" Accept header - make sure we leave requests'
        permissive "*/*" default in place instead of narrowing it."""
        assert distance_command.session.headers.get("Accept") == "*/*"

    def test_duration_under_an_hour(self, distance_command):
        responses = [
            make_response(geocode_payload("A", 1, 2)),
            make_response(geocode_payload("B", 3, 4)),
            make_response(directions_payload(5000, 300)),
        ]
        with patch.object(distance_command.session, "get", side_effect=responses):
            result = distance_command.execute("A,B")
        assert "~5min drive" in result

    def test_no_route_found_returns_both_city_names(self, distance_command):
        responses = [
            make_response(geocode_payload("Helsinki, Finland", 24.9, 60.2)),
            make_response(geocode_payload("Reykjavik, Iceland", -21.9, 64.1)),
            make_response({}, status_code=400),
        ]
        with patch.object(distance_command.session, "get", side_effect=responses):
            result = distance_command.execute("Helsinki,Reykjavik")
        assert "No driving route found" in result
        assert "Helsinki, Finland" in result
        assert "Reykjavik, Iceland" in result

    def test_empty_routes_list_treated_as_no_route(self, distance_command):
        responses = [
            make_response(geocode_payload("A", 1, 2)),
            make_response(geocode_payload("B", 3, 4)),
            make_response({"routes": []}),
        ]
        with patch.object(distance_command.session, "get", side_effect=responses):
            result = distance_command.execute("A,B")
        assert "No driving route found" in result

    def test_directions_timeout(self, distance_command):
        responses = [
            make_response(geocode_payload("A", 1, 2)),
            make_response(geocode_payload("B", 3, 4)),
        ]
        with patch.object(
            distance_command.session, "get",
            side_effect=responses + [requests.exceptions.Timeout()],
        ):
            result = distance_command.execute("A,B")
        assert "timed out" in result

    def test_directions_rate_limited(self, distance_command):
        responses = [
            make_response(geocode_payload("A", 1, 2)),
            make_response(geocode_payload("B", 3, 4)),
            make_response({}, status_code=429),
        ]
        with patch.object(distance_command.session, "get", side_effect=responses):
            result = distance_command.execute("A,B")
        assert "rate limit" in result.lower()
