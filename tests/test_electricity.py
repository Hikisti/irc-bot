from unittest.mock import patch

import pytest
import requests

from src.electricity import ElectricityCommand
from tests.conftest import make_json_response as make_response


@pytest.fixture
def electricity_command():
    return ElectricityCommand()


class TestElectricityCommand:
    def test_happy_path_rounds_price(self, electricity_command):
        with patch("src.electricity.requests.get", return_value=make_response({"price": 5.125})):
            result = electricity_command.execute()
        assert result == "5.13 snt / kWh"

    def test_negative_price_is_formatted(self, electricity_command):
        with patch("src.electricity.requests.get", return_value=make_response({"price": -1.2})):
            result = electricity_command.execute()
        assert result == "-1.20 snt / kWh"

    def test_second_call_uses_cache_without_new_request(self, electricity_command):
        with patch(
            "src.electricity.requests.get", return_value=make_response({"price": 3.0})
        ) as mock_get:
            first = electricity_command.execute()
            second = electricity_command.execute()

        assert first == second == "3.00 snt / kWh"
        mock_get.assert_called_once()

    def test_missing_price_field_returns_error(self, electricity_command):
        with patch("src.electricity.requests.get", return_value=make_response({"foo": "bar"})):
            result = electricity_command.execute()
        assert "Unexpected data format" in result

    def test_non_numeric_price_returns_error(self, electricity_command):
        with patch(
            "src.electricity.requests.get", return_value=make_response({"price": "not-a-number"})
        ):
            result = electricity_command.execute()
        assert "Invalid price data" in result

    def test_invalid_json_returns_error(self, electricity_command):
        resp = make_response({})
        resp.json.side_effect = ValueError("bad json")
        with patch("src.electricity.requests.get", return_value=resp):
            result = electricity_command.execute()
        assert "Could not parse" in result

    def test_timeout_returns_friendly_error(self, electricity_command):
        with patch("src.electricity.requests.get", side_effect=requests.exceptions.Timeout):
            result = electricity_command.execute()
        assert "timed out" in result

    def test_connection_error_returns_friendly_error(self, electricity_command):
        with patch(
            "src.electricity.requests.get", side_effect=requests.exceptions.ConnectionError
        ):
            result = electricity_command.execute()
        assert "Could not connect" in result

    def test_http_error_includes_status(self, electricity_command):
        with patch("src.electricity.requests.get", return_value=make_response({}, 500)):
            result = electricity_command.execute()
        assert "500" in result
