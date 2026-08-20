from unittest.mock import MagicMock, patch

import pytest

from src.stock import StockCommand


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
        with patch("src.stock.yf.Ticker", return_value=make_ticker(info)):
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
        with patch("src.stock.yf.Ticker", return_value=make_ticker(info)) as mock_ticker:
            stock_command.execute("tsla")
        mock_ticker.assert_called_once_with("TSLA")

    def test_negative_change_uses_red(self, stock_command):
        info = {
            "regularMarketPrice": 90.0,
            "regularMarketPreviousClose": 100.0,
            "shortName": "Tesla Inc.",
        }
        with patch("src.stock.yf.Ticker", return_value=make_ticker(info)):
            result = stock_command.execute("tsla")
        assert "\x0304" in result
        assert "-10.00 (-10.00%)" in result

    def test_missing_price_field_returns_error(self, stock_command):
        with patch("src.stock.yf.Ticker", return_value=make_ticker({})):
            result = stock_command.execute("bogus")
        assert "unavailable" in result

    def test_none_price_returns_error(self, stock_command):
        info = {"regularMarketPrice": None, "regularMarketPreviousClose": 100.0}
        with patch("src.stock.yf.Ticker", return_value=make_ticker(info)):
            result = stock_command.execute("tsla")
        assert "incomplete" in result

    def test_zero_prev_close_does_not_divide_by_zero(self, stock_command):
        info = {
            "regularMarketPrice": 10.0,
            "regularMarketPreviousClose": 0,
            "shortName": "x",
        }
        with patch("src.stock.yf.Ticker", return_value=make_ticker(info)):
            result = stock_command.execute("tsla")
        assert "(+0.00%)" in result

    def test_unexpected_exception_returns_error(self, stock_command):
        with patch("src.stock.yf.Ticker", side_effect=RuntimeError("boom")):
            result = stock_command.execute("tsla")
        assert "Could not retrieve stock data" in result
