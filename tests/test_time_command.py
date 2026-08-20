from unittest.mock import MagicMock, patch

import pytest
import requests

from src.time_command import TimeCommand


def make_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


@pytest.fixture
def time_command(monkeypatch):
    monkeypatch.setenv("TIME_API_KEY", "test-key")
    return TimeCommand()


class TestCityLookup:
    def test_empty_city_returns_usage_error(self, time_command):
        assert "Please provide a city" in time_command.execute("")

    def test_missing_api_key_returns_error(self, monkeypatch):
        monkeypatch.delenv("TIME_API_KEY", raising=False)
        tc = TimeCommand()
        assert "TIME_API_KEY is not set" in tc.execute("austin")

    def test_happy_path_with_seconds_in_time(self, time_command):
        # Regression test: time_24 formatted as HH:MM:SS (as returned in
        # practice) previously broke the timezone abbreviation lookup.
        data = {
            "date": "2026-01-15",
            "time_24": "13:00:39",
            "timezone": "America/Chicago",
            "location": {"city": "Austin", "country_name": "United States of America"},
        }
        with patch.object(time_command.session, "get", return_value=make_response(data)):
            result = time_command.execute("austin")

        assert result == "Local time in Austin, United States of America: 15/01/26 13:00:39 CST"

    def test_happy_path_with_short_time_format(self, time_command):
        # Regression test: time_24 formatted as HH:MM (no seconds) should
        # still resolve the abbreviation and get padded to HH:MM:00.
        data = {
            "date": "2026-01-15",
            "time_24": "13:00",
            "timezone": "America/Chicago",
            "location": {"city": "Austin", "country_name": "United States of America"},
        }
        with patch.object(time_command.session, "get", return_value=make_response(data)):
            result = time_command.execute("austin")

        assert result == "Local time in Austin, United States of America: 15/01/26 13:00:00 CST"

    def test_summer_date_returns_daylight_abbreviation(self, time_command):
        data = {
            "date": "2026-07-15",
            "time_24": "13:00:39",
            "timezone": "America/Chicago",
            "location": {"city": "Austin", "country_name": "United States of America"},
        }
        with patch.object(time_command.session, "get", return_value=make_response(data)):
            result = time_command.execute("austin")

        assert result.endswith("CDT")

    def test_zone_without_named_abbreviation_shows_utc_offset(self, time_command):
        data = {
            "date": "2026-08-20",
            "time_24": "09:13:00",
            "timezone": "Pacific/Pitcairn",
            "location": {"city": "Pitcairn islands", "country_name": "Pitcairn Islands"},
        }
        with patch.object(time_command.session, "get", return_value=make_response(data)):
            result = time_command.execute("pitcairn islands")

        assert result == "Local time in Pitcairn islands, Pitcairn Islands: 20/08/26 09:13:00 UTC-08"

    def test_country_name_deduped_from_city(self, time_command):
        data = {
            "date": "2026-01-15",
            "time_24": "13:00:39",
            "timezone": "America/Chicago",
            "location": {
                "city": "Chicago, United States of America",
                "country_name": "United States of America",
            },
        }
        with patch.object(time_command.session, "get", return_value=make_response(data)):
            result = time_command.execute("chicago")

        assert result.startswith("Local time in Chicago, United States of America:")

    def test_missing_timezone_field_omits_suffix(self, time_command):
        data = {
            "date": "2026-01-15",
            "time_24": "13:00:39",
            "location": {"city": "Austin", "country_name": "United States of America"},
        }
        with patch.object(time_command.session, "get", return_value=make_response(data)):
            result = time_command.execute("austin")

        assert result == "Local time in Austin, United States of America: 15/01/26 13:00:39"

    def test_timeout_returns_friendly_error(self, time_command):
        with patch.object(time_command.session, "get", side_effect=requests.exceptions.Timeout):
            result = time_command.execute("austin")
        assert "timed out" in result

    def test_connection_error_returns_friendly_error(self, time_command):
        with patch.object(
            time_command.session, "get", side_effect=requests.exceptions.ConnectionError
        ):
            result = time_command.execute("austin")
        assert "Could not connect" in result

    def test_http_error_returns_status_in_message(self, time_command):
        error_response = make_response({}, status_code=404)
        with patch.object(time_command.session, "get", return_value=error_response):
            result = time_command.execute("nonexistent-city")
        assert "404" in result

    def test_unexpected_payload_returns_error(self, time_command):
        with patch.object(time_command.session, "get", return_value=make_response({"foo": "bar"})):
            result = time_command.execute("austin")
        assert result.startswith("Error:")


class TestTimezoneAbbreviation:
    def test_known_abbreviation_skips_api_call(self, time_command):
        with patch.object(time_command.session, "get") as mock_get:
            result = time_command.execute("cdt")
        mock_get.assert_not_called()
        assert result.startswith("Local time in CDT (America/Chicago):")

    def test_abbreviation_lookup_is_case_insensitive(self, time_command):
        result = time_command.execute("eest")
        assert result.startswith("Local time in EEST (Europe/Helsinki):")

    def test_unknown_abbreviation_falls_back_to_city_lookup(self, time_command):
        # A 3-letter string that isn't a known abbreviation should still be
        # treated as a city and hit the API.
        data = {
            "date": "2026-01-15",
            "time_24": "13:00:39",
            "timezone": "Europe/Paris",
            "location": {"city": "Nyc", "country_name": "France"},
        }
        with patch.object(time_command.session, "get", return_value=make_response(data)) as mock_get:
            time_command.execute("nyc")
        mock_get.assert_called_once()


class TestFormatTzLabel:
    def test_named_abbreviation_passes_through(self):
        assert TimeCommand._format_tz_label("CDT") == "CDT"

    def test_positive_offset_gets_utc_prefix(self):
        assert TimeCommand._format_tz_label("+10") == "UTC+10"

    def test_negative_offset_gets_utc_prefix(self):
        assert TimeCommand._format_tz_label("-08") == "UTC-08"

    def test_none_passes_through(self):
        assert TimeCommand._format_tz_label(None) is None
