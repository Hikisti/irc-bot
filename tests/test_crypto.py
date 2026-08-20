from unittest.mock import patch

import pytest
import requests

from src.crypto import CryptoCommand


@pytest.fixture
def crypto_command():
    return CryptoCommand()


class TestCryptoCommand:
    def test_no_args_returns_usage(self, crypto_command):
        assert "Usage:" in crypto_command.execute("")

    def test_happy_path_positive_change(self, crypto_command):
        price_data = {
            "bitcoin": {
                "usd": 50000.0,
                "usd_24h_change": 2.5,
                "usd_24h_vol": 30_000_000_000,
            }
        }
        with patch.object(crypto_command.cg, "get_price", return_value=price_data):
            result = crypto_command.execute("bitcoin")

        assert "Bitcoin USD:" in result
        assert "50000.00 USD" in result
        assert "\x0309" in result  # green for positive change
        assert "+1250.00 (+2.50%)" in result
        assert "Volume 30.00B." in result

    def test_happy_path_negative_change_uses_red(self, crypto_command):
        price_data = {
            "bitcoin": {
                "usd": 50000.0,
                "usd_24h_change": -3.0,
                "usd_24h_vol": 10_000_000_000,
            }
        }
        with patch.object(crypto_command.cg, "get_price", return_value=price_data):
            result = crypto_command.execute("bitcoin")

        assert "\x0304" in result  # red for negative change
        assert "-1500.00 (-3.00%)" in result

    def test_input_is_lowercased(self, crypto_command):
        price_data = {"bitcoin": {"usd": 100.0, "usd_24h_change": 1.0, "usd_24h_vol": 0}}
        with patch.object(crypto_command.cg, "get_price", return_value=price_data) as mock_get:
            crypto_command.execute("BitCoin")
        assert mock_get.call_args.kwargs["ids"] == "bitcoin"

    def test_unknown_crypto_returns_error(self, crypto_command):
        with patch.object(crypto_command.cg, "get_price", return_value={}):
            result = crypto_command.execute("notacoin")
        assert "not found" in result

    def test_incomplete_data_returns_error(self, crypto_command):
        price_data = {"bitcoin": {"usd": 50000.0}}  # missing 24h_change
        with patch.object(crypto_command.cg, "get_price", return_value=price_data):
            result = crypto_command.execute("bitcoin")
        assert "Incomplete data" in result

    def test_http_error_returns_friendly_message(self, crypto_command):
        error = requests.exceptions.HTTPError(response=type("R", (), {"status_code": 500})())
        with patch.object(crypto_command.cg, "get_price", side_effect=error):
            result = crypto_command.execute("bitcoin")
        assert "HTTP error" in result

    def test_timeout_returns_friendly_message(self, crypto_command):
        with patch.object(crypto_command.cg, "get_price", side_effect=requests.exceptions.Timeout):
            result = crypto_command.execute("bitcoin")
        assert "timed out" in result

    def test_connection_error_returns_friendly_message(self, crypto_command):
        with patch.object(
            crypto_command.cg, "get_price", side_effect=requests.exceptions.ConnectionError
        ):
            result = crypto_command.execute("bitcoin")
        assert "Cannot connect" in result
