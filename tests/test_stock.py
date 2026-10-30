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
        assert "Volume 5.00M." in result

    BASE = {"regularMarketPrice": 345.18, "regularMarketPreviousClose": 341.14, "regularMarketVolume": 17_111_010,
            "currency": "USD", "shortName": "Apple Inc."}

    def _run(self, stock_command, **extra):
        with patch("stock.yf.Ticker", return_value=make_ticker({**self.BASE, **extra})):
            return stock_command.execute("aapl")

    # ---- issue #69: the change of a coin, the decimals of a small price, the size of the volume -------------

    def _crypto(self, **extra):
        # the shape of BTC-USD on 2026-10-30: since the start of the UTC day it is down 0.58%, over 24 hours 3.25%
        info = {"regularMarketPrice": 77342.34, "regularMarketPreviousClose": 77791.38, "regularMarketChange": -2598.6,
                "regularMarketChangePercent": -3.25, "regularMarketVolume": 58_679_742_464, "currency": "USD",
                "shortName": "Bitcoin USD", "quoteType": "CRYPTOCURRENCY"}
        return {**info, **extra}

    def _line(self, stock_command, info, symbol="x"):
        with patch("stock.yf.Ticker", return_value=make_ticker(info)):
            return stock_command.execute(symbol)

    def test_a_coins_change_is_yahoos_rolling_24_hours_labelled_24h(self, stock_command):
        result = self._line(stock_command, self._crypto(), "btc-usd")
        assert "77342.34 USD, 24h " in result and "-2598.60 (-3.25%)" in result
        assert "today" not in result and "\x0304" in result  # red

    def test_the_two_definitions_can_have_opposite_signs_and_the_24h_one_wins(self, stock_command):
        # AAVE-USD on 2026-10-30: +5.3% since the start of the UTC day, -5.2% over 24 hours
        info = self._crypto(regularMarketPrice=126.06, regularMarketPreviousClose=119.5322, regularMarketChange=-6.9,
                            regularMarketChangePercent=-5.19, shortName="Aave USD")
        result = self._line(stock_command, info)
        assert "-6.90 (-5.19%)" in result and "+6.5" not in result

    def test_a_stocks_change_is_unchanged_and_still_labelled_today(self, stock_command):
        info = {"regularMarketPrice": 9.26, "regularMarketPreviousClose": 9.12, "regularMarketChange": 0.14,
                "regularMarketChangePercent": 1.5350877, "regularMarketVolume": 573_210, "currency": "USD",
                "shortName": "Loma Negra", "quoteType": "EQUITY"}
        result = self._line(stock_command, info, "loma")
        assert "9.26 USD, today " in result and "+0.14 (+1.54%)" in result and "Volume 573.21k." in result

    def test_without_yahoos_change_the_old_calculation_is_used(self, stock_command):
        result = self._line(stock_command, self._crypto(regularMarketChange=None, regularMarketChangePercent=None))
        assert "-449.04 (-0.58%)" in result  # price against the previous close, as before
        info = self._crypto(regularMarketChange=-1.0)  # only one of the two present
        info.pop("regularMarketChangePercent")
        assert "-449.04 (-0.58%)" in self._line(stock_command, info)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "n/a", True])
    def test_an_unusable_yahoo_change_falls_back(self, stock_command, bad):
        assert "-449.04 (-0.58%)" in self._line(stock_command, self._crypto(regularMarketChange=bad))

    def test_a_coin_under_a_dollar_keeps_its_decimals(self, stock_command):
        # DOGE-USD on 2026-10-30: 0.08 USD and a change of +0.00 before
        info = self._crypto(regularMarketPrice=0.07581, regularMarketPreviousClose=0.07518, regularMarketChange=-0.00309,
                            regularMarketChangePercent=-3.92, regularMarketVolume=1_377_726_976, shortName="Dogecoin USD",
                            fiftyTwoWeekLow=0.0701, fiftyTwoWeekHigh=0.1923)
        result = self._line(stock_command, info)
        assert "0.0758 USD, 24h " in result and "-0.0031 (-3.92%)" in result
        assert "52W: 0.0701 - 0.1923." in result

    @pytest.mark.parametrize("price, decimals", [(345.18, 2), (1.0, 2), (0.99, 4), (0.01, 4), (0.009, 6), (0.0000123, 6)])
    def test_the_decimals_follow_the_size_of_the_price(self, price, decimals):
        assert StockCommand._decimals(price) == decimals

    @pytest.mark.parametrize("volume, text", [
        (0, "0.00k"), (None, "0.00k"), (573_210, "573.21k"), (999_999, "1000.00k"), (1_000_000, "1.00M"),
        (17_111_010, "17.11M"), (999_999_999, "1000.00M"), (1_377_726_976, "1.378B"), (58_679_742_464, "58.680B"),
    ])
    def test_the_volume_gets_a_suffix_by_size(self, volume, text):
        assert StockCommand._format_volume(volume) == text

    def test_the_52_week_range_ends_the_line(self, stock_command):
        result = self._run(stock_command, fiftyTwoWeekLow=243.42, fiftyTwoWeekHigh=348.1499)
        assert result.endswith("Volume 17.11M. 52W: 243.42 - 348.15.")
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
        assert result.endswith("Volume 17.11M.") and "52W" not in result

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
