import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import requests

from time_command import TimeCommand
from tests.conftest import make_json_response as make_response


@pytest.fixture
def time_command(monkeypatch):
    monkeypatch.setenv("TIME_API_KEY", "test-key")
    return TimeCommand()


class FrozenDateTime(datetime.datetime):
    """Freezes _time_for_abbreviation()'s "now" to a specific instant -
    without this, whether a requested abbreviation (e.g. "CST") matches
    what's actually currently observed depends on the real calendar date
    the suite happens to run on (DST makes America/Chicago's real
    abbreviation flip between CST and CDT across the year)."""
    _frozen_utc = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen_utc.astimezone(tz) if tz else cls._frozen_utc.replace(tzinfo=None)


def freeze_at(monkeypatch, year, month, day):
    FrozenDateTime._frozen_utc = datetime.datetime(year, month, day, 12, 0, tzinfo=ZoneInfo("UTC"))
    monkeypatch.setattr("time_command.datetime", FrozenDateTime)


class TestCityLookup:
    def test_empty_city_returns_usage_error(self, time_command):
        assert "Please provide a city" in time_command.execute("")

    def test_missing_api_key_returns_error(self, monkeypatch):
        monkeypatch.delenv("TIME_API_KEY", raising=False)
        with patch("time_command.load_dotenv"):
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

    def test_non_dict_payload_does_not_crash(self, time_command):
        # Regression test: a list payload used to reach data.get("error")
        # unconditionally and raise AttributeError instead of returning an
        # error message.
        with patch.object(time_command.session, "get", return_value=make_response(["not", "a", "dict"])):
            result = time_command.execute("austin")
        assert result.startswith("Error:")

    def test_invalid_json_returns_friendly_error(self, time_command):
        resp = make_response({})
        resp.json.side_effect = ValueError("bad json")
        with patch.object(time_command.session, "get", return_value=resp):
            result = time_command.execute("austin")
        assert "Invalid response from time service" in result

    def test_api_error_message_is_surfaced(self, time_command):
        with patch.object(
            time_command.session, "get", return_value=make_response({"error": "location not found"})
        ):
            result = time_command.execute("nowhereville")
        assert result == "Error: location not found"

    def test_api_message_field_is_surfaced(self, time_command):
        with patch.object(
            time_command.session, "get", return_value=make_response({"message": "quota exceeded"})
        ):
            result = time_command.execute("austin")
        assert result == "Error: quota exceeded"


class TestTimezoneAbbreviation:
    def test_known_abbreviation_skips_api_call(self, time_command):
        with patch.object(time_command.session, "get") as mock_get:
            result = time_command.execute("cdt")
        mock_get.assert_not_called()
        assert result.startswith("Local time in CDT (America/Chicago):")

    def test_abbreviation_lookup_is_case_insensitive(self, time_command):
        result = time_command.execute("eest")
        assert result.startswith("Local time in EEST (Europe/Helsinki):")

    def test_zoneinfo_construction_failure_returns_friendly_error(self, time_command, monkeypatch):
        # Every mapped IANA name is a real zone in practice, so this
        # guards a failure mode that's never actually been observed live
        # - still worth a direct test since _time_for_abbreviation()
        # can't otherwise fail this way.
        def boom(name):
            raise KeyError(name)

        monkeypatch.setattr("time_command.ZoneInfo", boom)
        result = time_command.execute("cdt")
        assert result == "Error: Could not resolve timezone for CDT."

    def test_no_mismatch_note_when_actually_observing_the_requested_abbreviation(
        self, time_command, monkeypatch
    ):
        freeze_at(monkeypatch, 2026, 1, 15)  # winter -> Chicago really is on CST
        result = time_command.execute("cst")
        assert result.startswith("Local time in CST (America/Chicago):")
        assert "currently observing" not in result

    def test_mismatch_note_when_dst_means_a_different_abbreviation_is_active(
        self, time_command, monkeypatch
    ):
        freeze_at(monkeypatch, 2026, 7, 15)  # summer -> Chicago is on CDT, not CST
        result = time_command.execute("cst")
        assert "(currently observing CDT, not CST)" in result

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


class TestFormatLocation:
    def test_uses_city_and_country_from_location(self, time_command):
        data = {"location": {"city": "Austin", "country_name": "United States"}}
        assert time_command._format_location(data, "austin") == "Austin, United States"

    def test_falls_back_to_geo_key(self, time_command):
        data = {"geo": {"city": "Austin", "country_name": "United States"}}
        assert time_command._format_location(data, "austin") == "Austin, United States"

    def test_falls_back_to_the_queried_city_name(self, time_command):
        assert time_command._format_location({}, "austin") == "Austin"

    def test_dedupes_country_already_in_the_city_string(self, time_command):
        data = {"location": {"city": "Chicago, United States", "country_name": "United States"}}
        assert time_command._format_location(data, "chicago") == "Chicago, United States"

    def test_no_country_returns_bare_city(self, time_command):
        data = {"location": {"city": "somewhere"}}
        assert time_command._format_location(data, "somewhere") == "Somewhere"


class TestFormatDate:
    def test_reformats_iso_date(self, time_command):
        assert time_command._format_date("2026-01-15") == "15/01/26"

    def test_wrong_length_passes_through_unchanged(self, time_command):
        assert time_command._format_date("2026-1-1") == "2026-1-1"

    def test_empty_string_passes_through(self, time_command):
        assert time_command._format_date("") == ""


class TestResolveTzAbbr:
    def test_resolves_named_abbreviation(self, time_command):
        assert time_command._resolve_tz_abbr("2026-01-15", "13:00:39", "America/Chicago") == "CST"

    def test_resolves_short_time_format(self, time_command):
        assert time_command._resolve_tz_abbr("2026-01-15", "13:00", "America/Chicago") == "CST"

    def test_missing_timezone_returns_none(self, time_command):
        assert time_command._resolve_tz_abbr("2026-01-15", "13:00:39", None) is None

    def test_missing_date_or_time_returns_none(self, time_command):
        assert time_command._resolve_tz_abbr("", "13:00:39", "America/Chicago") is None
        assert time_command._resolve_tz_abbr("2026-01-15", "", "America/Chicago") is None

    def test_invalid_timezone_name_returns_none(self, time_command):
        assert time_command._resolve_tz_abbr("2026-01-15", "13:00:39", "Not/AZone") is None


class TestFormatTzLabel:
    def test_named_abbreviation_passes_through(self):
        assert TimeCommand._format_tz_label("CDT") == "CDT"

    def test_positive_offset_gets_utc_prefix(self):
        assert TimeCommand._format_tz_label("+10") == "UTC+10"

    def test_negative_offset_gets_utc_prefix(self):
        assert TimeCommand._format_tz_label("-08") == "UTC-08"

    def test_none_passes_through(self):
        assert TimeCommand._format_tz_label(None) is None
