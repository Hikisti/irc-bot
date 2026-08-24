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
