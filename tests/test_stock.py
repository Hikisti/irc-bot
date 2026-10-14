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

    BASE = {"regularMarketPrice": 345.18, "regularMarketPreviousClose": 341.14, "regularMarketVolume": 17_111_010,
            "currency": "USD", "shortName": "Apple Inc."}

    def _run(self, stock_command, **extra):
        with patch("stock.yf.Ticker", return_value=make_ticker({**self.BASE, **extra})):
            return stock_command.execute("aapl")

    def test_the_52_week_range_ends_the_line(self, stock_command):
        result = self._run(stock_command, fiftyTwoWeekLow=243.42, fiftyTwoWeekHigh=348.1499)
        assert result.endswith("Volume 17111.01k. 52W: 243.42 - 348.15.")
        assert result.startswith("\x02Apple Inc. (AAPL):\x02 345.18 USD, today ")  # the rest of the line is as before

    @pytest.mark.parametrize("extra", [
        {},                                                            # neither end in the response
        {"fiftyTwoWeekLow": 243.42},                                   # one end only
        {"fiftyTwoWeekHigh": 348.15},
        {"fiftyTwoWeekLow": None, "fiftyTwoWeekHigh": 348.15},
        {"fiftyTwoWeekLow": "n/a", "fiftyTwoWeekHigh": 348.15},         # not numbers
        {"fiftyTwoWeekLow": float("nan"), "fiftyTwoWeekHigh": 348.15},
        {"fiftyTwoWeekLow": 243.42, "fiftyTwoWeekHigh": float("inf")},
        {"fiftyTwoWeekLow": True, "fiftyTwoWeekHigh": 348.15},
    ])
    def test_a_missing_or_unusable_end_leaves_the_range_out(self, stock_command, extra):
        result = self._run(stock_command, **extra)
        assert result.endswith("Volume 17111.01k.") and "52W" not in result

    def test_whole_number_ends_and_a_crypto_pair_format_like_the_price(self, stock_command):
        assert self._run(stock_command, fiftyTwoWeekLow=57747.766, fiftyTwoWeekHigh=116273).endswith("52W: 57747.77 - 116273.00.")

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
