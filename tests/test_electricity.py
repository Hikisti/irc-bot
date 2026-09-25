import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import requests

from electricity import ElectricityCommand
from tests.conftest import make_json_response as make_response


@pytest.fixture
def electricity_command():
    return ElectricityCommand()


class FrozenDateTime(datetime.datetime):
    """Freezes execute()'s "now" to a specific Helsinki wall-clock time -
    without this, whether the cache's next-quarter-hour rollover hits the
    "into the next hour" branch or the "same hour" branch (and thus which
    one gets test coverage) depends on the real clock at whatever moment
    the suite happens to run."""
    _frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen.astimezone(tz) if tz else cls._frozen.replace(tzinfo=None)


def freeze_at(monkeypatch, hour, minute, year=2026, month=1, day=15, fold=0):
    tz = ZoneInfo("Europe/Helsinki")
    FrozenDateTime._frozen = datetime.datetime(year, month, day, hour, minute, 30, fold=fold, tzinfo=tz)
    monkeypatch.setattr("electricity.datetime.datetime", FrozenDateTime)


class TestElectricityCommand:
    def test_happy_path_rounds_price(self, electricity_command):
        with patch.object(electricity_command.session, "get", return_value=make_response({"price": 5.125})):
            result = electricity_command.execute()
        assert result == "5.13 snt / kWh"

    def test_negative_price_is_formatted(self, electricity_command):
        with patch.object(electricity_command.session, "get", return_value=make_response({"price": -1.2})):
            result = electricity_command.execute()
        assert result == "-1.20 snt / kWh"

    def test_missing_price_field_returns_error(self, electricity_command):
        with patch.object(electricity_command.session, "get", return_value=make_response({"foo": "bar"})):
            result = electricity_command.execute()
        assert "Unexpected data format" in result

    def test_non_numeric_price_returns_error(self, electricity_command):
        with patch.object(
            electricity_command.session, "get", return_value=make_response({"price": "not-a-number"})
        ):
            result = electricity_command.execute()
        assert "Invalid price data" in result

    def test_invalid_json_returns_error(self, electricity_command):
        resp = make_response({})
        resp.json.side_effect = ValueError("bad json")
        with patch.object(electricity_command.session, "get", return_value=resp):
            result = electricity_command.execute()
        assert "Could not parse" in result

    def test_timeout_returns_friendly_error(self, electricity_command):
        with patch.object(electricity_command.session, "get", side_effect=requests.exceptions.Timeout):
            result = electricity_command.execute()
        assert "timed out" in result

    def test_connection_error_returns_friendly_error(self, electricity_command):
        with patch.object(
            electricity_command.session, "get", side_effect=requests.exceptions.ConnectionError
        ):
            result = electricity_command.execute()
        assert "Could not connect" in result

    def test_http_error_includes_status(self, electricity_command):
        with patch.object(electricity_command.session, "get", return_value=make_response({}, 500)):
            result = electricity_command.execute()
        assert "500" in result


class TestCacheExpiry:
    def test_cache_expires_at_the_next_quarter_hour_within_the_same_hour(
        self, electricity_command, monkeypatch
    ):
        freeze_at(monkeypatch, hour=10, minute=20)  # -> next quarter is 10:30, same hour
        with patch.object(electricity_command.session, "get", return_value=make_response({"price": 1.0})):
            electricity_command.execute()

        expected = datetime.datetime(2026, 1, 15, 10, 30, 0, tzinfo=ZoneInfo("Europe/Helsinki"))
        assert electricity_command._cache_until_timestamp == expected.timestamp()

    def test_cache_expires_at_the_top_of_the_next_hour_past_minute_45(
        self, electricity_command, monkeypatch
    ):
        freeze_at(monkeypatch, hour=10, minute=50)  # -> next quarter would be :60, rolls to 11:00
        with patch.object(electricity_command.session, "get", return_value=make_response({"price": 1.0})):
            electricity_command.execute()

        expected = datetime.datetime(2026, 1, 15, 11, 0, 0, tzinfo=ZoneInfo("Europe/Helsinki"))
        assert electricity_command._cache_until_timestamp == expected.timestamp()

    def test_cache_expires_after_15_real_minutes_across_dst_fall_back(
        self, electricity_command, monkeypatch
    ):
        # Regression test: Finland's DST fall-back for 2026 is 2026-10-25,
        # 04:00 EEST -> 03:00 EET, so 03:00-03:59 occurs twice (fold=0 is
        # the first, EEST pass). Computing the next cache boundary by
        # adding a wall-clock timedelta(hours=1) to "now" (the old logic)
        # jumped straight over the repeated hour, caching a price for a
        # real 70 minutes here instead of ~10-15.
        freeze_at(monkeypatch, year=2026, month=10, day=25, hour=3, minute=50, fold=0)
        with patch.object(electricity_command.session, "get", return_value=make_response({"price": 1.0})):
            electricity_command.execute()

        now = datetime.datetime(2026, 10, 25, 3, 50, 30, fold=0, tzinfo=ZoneInfo("Europe/Helsinki"))
        real_minutes_cached = (electricity_command._cache_until_timestamp - now.timestamp()) / 60
        assert 9 <= real_minutes_cached <= 10

    def test_cache_is_reused_before_expiry_and_refetched_after(self, electricity_command, monkeypatch):
        freeze_at(monkeypatch, hour=10, minute=20)  # expires at 10:30
        with patch.object(
            electricity_command.session, "get", return_value=make_response({"price": 1.0})
        ) as mock_get:
            first = electricity_command.execute()

            freeze_at(monkeypatch, hour=10, minute=29)  # still before 10:30
            second = electricity_command.execute()
            assert mock_get.call_count == 1

            freeze_at(monkeypatch, hour=10, minute=31)  # now past expiry
            third = electricity_command.execute()
            assert mock_get.call_count == 2

        assert first == second == third == "1.00 snt / kWh"
