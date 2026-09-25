from unittest.mock import MagicMock, patch

import curl_cffi.requests.exceptions as curl_exceptions
import pytest
import requests
from yfinance.exceptions import YFRateLimitError

from stock import StockCommand


@pytest.fixture
def stock_command():
    return StockCommand()


def make_ticker(info):
    ticker = MagicMock()
    ticker.info = info
    return ticker


class TestStockCommand:
    def test_no_args_returns_usage(self, stock_command):
        assert "Usage:" in stock_command.execute("")

    def test_happy_path_positive_change(self, stock_command):
        info = {
            "regularMarketPrice": 110.0,
            "regularMarketPreviousClose": 100.0,
            "regularMarketVolume": 5_000_000,
            "currency": "USD",
            "shortName": "Tesla Inc.",
        }
        with patch("stock.yf.Ticker", return_value=make_ticker(info)):
            result = stock_command.execute("tsla")

        assert "Tesla Inc. (TSLA):" in result
        assert "110.00 USD" in result
        assert "\x0309" in result  # green for positive change
        assert "+10.00 (+10.00%)" in result
        assert "Volume 5000.00k." in result

    def test_symbol_is_uppercased(self, stock_command):
        info = {
            "regularMarketPrice": 1.0,
            "regularMarketPreviousClose": 1.0,
            "shortName": "x",
        }
        with patch("stock.yf.Ticker", return_value=make_ticker(info)) as mock_ticker:
            stock_command.execute("tsla")
        mock_ticker.assert_called_once_with("TSLA")

    def test_negative_change_uses_red(self, stock_command):
        info = {
            "regularMarketPrice": 90.0,
            "regularMarketPreviousClose": 100.0,
            "shortName": "Tesla Inc.",
        }
        with patch("stock.yf.Ticker", return_value=make_ticker(info)):
            result = stock_command.execute("tsla")
        assert "\x0304" in result
        assert "-10.00 (-10.00%)" in result

    def test_missing_price_field_returns_error(self, stock_command):
        with patch("stock.yf.Ticker", return_value=make_ticker({})):
            result = stock_command.execute("bogus")
        assert "unavailable" in result

    def test_none_price_returns_error(self, stock_command):
        info = {"regularMarketPrice": None, "regularMarketPreviousClose": 100.0}
        with patch("stock.yf.Ticker", return_value=make_ticker(info)):
            result = stock_command.execute("tsla")
        assert "incomplete" in result

    def test_zero_prev_close_does_not_divide_by_zero(self, stock_command):
        info = {
            "regularMarketPrice": 10.0,
            "regularMarketPreviousClose": 0,
            "shortName": "x",
        }
        with patch("stock.yf.Ticker", return_value=make_ticker(info)):
            result = stock_command.execute("tsla")
        assert "(+0.00%)" in result

    def test_unexpected_exception_returns_error(self, stock_command):
        with patch("stock.yf.Ticker", side_effect=RuntimeError("boom")):
            result = stock_command.execute("tsla")
        assert "Could not retrieve stock data" in result

    def test_invalid_symbol_value_error_returns_friendly_message(self, stock_command):
        with patch("stock.yf.Ticker", side_effect=ValueError("bad symbol")):
            result = stock_command.execute("!!!")
        assert "Invalid stock symbol" in result

    def test_connection_error_returns_friendly_error(self, stock_command):
        # Regression test: yfinance's real network errors are not the
        # builtin ConnectionError/TimeoutError this used to (uselessly)
        # catch - this used to fall through to the generic exception
        # branch instead of a network-specific message. Covers the
        # requests.exceptions.* shape (yfinance's fallback HTTP path when
        # curl_cffi isn't installed).
        with patch("stock.yf.Ticker", side_effect=requests.exceptions.ConnectionError):
            result = stock_command.execute("tsla")
        assert "Could not connect" in result

    def test_timeout_returns_friendly_error(self, stock_command):
        with patch("stock.yf.Ticker", side_effect=requests.exceptions.Timeout):
            result = stock_command.execute("tsla")
        assert "timed out" in result

    def test_curl_cffi_connection_error_returns_friendly_error(self, stock_command):
        # Regression test: yfinance 1.x does its actual HTTP via
        # curl_cffi by default (confirmed live: curl_cffi is installed
        # and yfinance uses it unless YF_DISABLE_CURL_CFFI is set), whose
        # exceptions don't inherit from requests.exceptions.* at all - the
        # requests-only except clause above would silently miss these.
        with patch("stock.yf.Ticker", side_effect=curl_exceptions.ConnectionError("refused")):
            result = stock_command.execute("tsla")
        assert "Could not connect" in result

    def test_curl_cffi_timeout_returns_friendly_error(self, stock_command):
        with patch("stock.yf.Ticker", side_effect=curl_exceptions.Timeout("slow")):
            result = stock_command.execute("tsla")
        assert "timed out" in result

    def test_rate_limit_error_returns_friendly_error(self, stock_command):
        # New in yfinance 1.x - Yahoo actively rate-limits frequent
        # requests, confirmed to happen in practice.
        with patch("stock.yf.Ticker", side_effect=YFRateLimitError()):
            result = stock_command.execute("tsla")
        assert "rate limit" in result.lower()
