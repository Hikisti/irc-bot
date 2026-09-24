import datetime
from unittest.mock import patch

import pytest
import pytz
import requests

from f1_command import F1Command
from tests.conftest import make_json_response as make_response


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
    monkeypatch.setattr("f1_command.datetime.datetime", FrozenDateTime)


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


class TestCollectEvents:
    def test_flattens_sessions_and_race_sorted_by_start_time(self, f1_command):
        events = f1_command._collect_events(RACE_JSON["MRData"]["RaceTable"]["Races"])
        labels_in_order = [label for label, _dt, _race in events]
        # Chinese GP's FirstPractice/Qualifying/Race, then Japanese GP's
        # FirstPractice/Race - chronological, not grouped by race.
        assert labels_in_order == ["Practice 1", "Qualifying", "Race", "Practice 1", "Race"]

    def test_skips_non_dict_race_entries(self, f1_command):
        events = f1_command._collect_events(["not-a-race", None])
        assert events == []

    def test_race_with_no_parseable_dates_yields_nothing(self, f1_command):
        events = f1_command._collect_events([{"raceName": "X"}])
        assert events == []


class TestFindOngoingAndNext:
    def test_finds_ongoing_and_the_event_right_after_it(self, f1_command, frozen_now):
        events = f1_command._collect_events(RACE_JSON["MRData"]["RaceTable"]["Races"])
        ongoing, next_ = f1_command._find_ongoing_and_next(events)
        assert ongoing[0] == "Race"
        assert ongoing[2]["raceName"] == "Chinese Grand Prix"
        assert next_[0] == "Practice 1"
        assert next_[2]["raceName"] == "Japanese Grand Prix"

    def test_nothing_ongoing_returns_first_future_event_as_next(self, f1_command):
        # "now" is real time here, far outside the fixture data's 2026
        # session windows below - construct events entirely in the future.
        far_future = datetime.datetime(2099, 1, 1, tzinfo=pytz.UTC)
        events = [("Race", far_future, {"raceName": "Future GP"})]
        ongoing, next_ = f1_command._find_ongoing_and_next(events)
        assert ongoing is None
        assert next_[2]["raceName"] == "Future GP"

    def test_everything_in_the_past_returns_nothing(self, f1_command):
        past = datetime.datetime(2000, 1, 1, tzinfo=pytz.UTC)
        events = [("Race", past, {"raceName": "Old GP"})]
        ongoing, next_ = f1_command._find_ongoing_and_next(events)
        assert ongoing is None
        assert next_ is None


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
