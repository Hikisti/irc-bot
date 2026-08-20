import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz
import requests

from src.f1_command import F1Command


def make_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = "Not Found"
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


# Chinese GP race session is 07:00-09:00 UTC on 2026-03-15; freeze "now" to
# fall inside that window so it resolves as the ongoing event.
FIXED_UTC_NOW = datetime.datetime(2026, 3, 15, 8, 0, tzinfo=pytz.UTC)


class FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FIXED_UTC_NOW.replace(tzinfo=None)
        return FIXED_UTC_NOW.astimezone(tz)


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr("src.f1_command.datetime.datetime", FrozenDateTime)


@pytest.fixture
def f1_command():
    return F1Command()


RACE_JSON = {
    "MRData": {
        "RaceTable": {
            "Races": [
                {
                    "raceName": "Chinese Grand Prix",
                    "date": "2026-03-15",
                    "time": "07:00:00Z",
                    "Circuit": {"Location": {"locality": "Shanghai", "country": "China"}},
                    "FirstPractice": {"date": "2026-03-13", "time": "03:30:00Z"},
                    "Qualifying": {"date": "2026-03-14", "time": "07:00:00Z"},
                },
                {
                    "raceName": "Japanese Grand Prix",
                    "date": "2026-03-29",
                    "time": "05:00:00Z",
                    "Circuit": {"Location": {"locality": "Suzuka", "country": "Japan"}},
                    "FirstPractice": {"date": "2026-03-27", "time": "02:30:00Z"},
                },
            ]
        }
    }
}


class TestExecute:
    def test_ongoing_and_next_event(self, f1_command, frozen_now):
        with patch.object(f1_command.session, "get", return_value=make_response(RACE_JSON)):
            result = f1_command.execute()

        assert result.startswith("Ongoing: Chinese Grand Prix (Race) - Shanghai, China |")
        assert " || Next: Japanese Grand Prix (Practice 1) - Suzuka, Japan |" in result

    def test_no_races_returns_message(self, f1_command, frozen_now):
        empty = {"MRData": {"RaceTable": {"Races": []}}}
        with patch.object(f1_command.session, "get", return_value=make_response(empty)):
            result = f1_command.execute()
        assert result == "No F1 races found for current season."

    def test_timeout_returns_friendly_error(self, f1_command):
        with patch.object(f1_command.session, "get", side_effect=requests.exceptions.Timeout):
            result = f1_command.execute()
        assert "timed out" in result

    def test_connection_error_returns_friendly_error(self, f1_command):
        with patch.object(
            f1_command.session, "get", side_effect=requests.exceptions.ConnectionError
        ):
            result = f1_command.execute()
        assert "Could not connect" in result

    def test_http_error_includes_status(self, f1_command):
        with patch.object(f1_command.session, "get", return_value=make_response({}, 500)):
            result = f1_command.execute()
        assert "500" in result

    def test_malformed_response_missing_race_table(self, f1_command):
        with patch.object(f1_command.session, "get", return_value=make_response({"foo": "bar"})):
            result = f1_command.execute()
        assert "Unexpected F1 API response format" in result

    def test_non_list_races_returns_error(self, f1_command):
        bad = {"MRData": {"RaceTable": {"Races": "not-a-list"}}}
        with patch.object(f1_command.session, "get", return_value=make_response(bad)):
            result = f1_command.execute()
        assert "Invalid race data received" in result


class TestParseDt:
    def test_parses_valid_date_and_time(self, f1_command):
        dt = f1_command._parse_dt("2026-03-15", "07:00:00Z")
        assert dt == datetime.datetime(2026, 3, 15, 7, 0, tzinfo=datetime.timezone.utc)

    def test_missing_time_defaults_to_midnight(self, f1_command):
        dt = f1_command._parse_dt("2026-03-15", None)
        assert dt == datetime.datetime(2026, 3, 15, 0, 0, tzinfo=datetime.timezone.utc)

    def test_missing_date_returns_none(self, f1_command):
        assert f1_command._parse_dt(None, "07:00:00Z") is None

    def test_invalid_date_returns_none(self, f1_command):
        assert f1_command._parse_dt("not-a-date", "07:00:00Z") is None

    def test_parse_session_dt_ignores_non_dict(self, f1_command):
        assert f1_command._parse_session_dt("not-a-dict") is None


class TestFormatEvent:
    def test_formats_location_and_time(self, f1_command):
        dt = datetime.datetime(2026, 3, 15, 7, 0, tzinfo=datetime.timezone.utc)
        race_info = {
            "raceName": "Chinese Grand Prix",
            "Circuit": {"Location": {"locality": "Shanghai", "country": "China"}},
        }
        result = f1_command._format_event("Race", dt, race_info)
        assert result.startswith("Chinese Grand Prix (Race) - Shanghai, China |")

    def test_missing_locality_falls_back_to_country_only(self, f1_command):
        dt = datetime.datetime(2026, 3, 15, 7, 0, tzinfo=datetime.timezone.utc)
        race_info = {"raceName": "Test GP", "Circuit": {"Location": {"country": "Italy"}}}
        result = f1_command._format_event("Qualifying", dt, race_info)
        assert "- Italy |" in result

    def test_missing_race_name_uses_fallback(self, f1_command):
        dt = datetime.datetime(2026, 3, 15, 7, 0, tzinfo=datetime.timezone.utc)
        result = f1_command._format_event("Race", dt, {})
        assert result.startswith("Unknown GP (Race)")
