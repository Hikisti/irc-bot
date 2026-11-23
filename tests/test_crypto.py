from unittest.mock import patch

import pytest
import requests

from crypto import HISTORY_TIMEOUT_SECONDS, PRICE_RETRIES, PRICE_TIMEOUT_SECONDS, CryptoCommand, price_decimals, year_range
from tests.conftest import make_json_response

YEAR = {"prices": [[1, 41000.5], [2, 69000.25], [3, 50000.0]], "market_caps": [], "total_volumes": []}
PRICE = {"bitcoin": {"usd": 50000.0, "usd_24h_change": 2.5, "usd_24h_vol": 30_000_000_000}}


@pytest.fixture
def crypto_command():
    command = CryptoCommand()
    # No test touches the network: the history request answers with YEAR unless a test says otherwise.
    with patch.object(command.history_cg, "get_coin_market_chart_by_id", return_value=YEAR) as history:
        command.history = history
        yield command


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
        error = requests.exceptions.HTTPError(response=make_json_response({}, status_code=500))
        with patch.object(crypto_command.cg, "get_price", side_effect=error):
            result = crypto_command.execute("bitcoin")
        assert "500" in result

    def test_timeout_returns_friendly_message(self, crypto_command):
        with patch.object(crypto_command.cg, "get_price", side_effect=requests.exceptions.Timeout):
            result = crypto_command.execute("bitcoin")
        assert "timed out" in result

    def test_connection_error_returns_friendly_message(self, crypto_command):
        with patch.object(
            crypto_command.cg, "get_price", side_effect=requests.exceptions.ConnectionError
        ):
            result = crypto_command.execute("bitcoin")
        assert "Could not connect" in result


class TestYearRange:
    def test_reply_ends_with_the_range_of_the_daily_prices(self, crypto_command):
        with patch.object(crypto_command.cg, "get_price", return_value=PRICE):
            result = crypto_command.execute("bitcoin")
        assert result.endswith("Volume 30.00B. 52W: 41000.50 - 69000.25.")
        assert crypto_command.history.call_args.args == ("bitcoin",)
        assert crypto_command.history.call_args.kwargs == {"vs_currency": "usd", "days": 365, "interval": "daily"}

    @pytest.mark.parametrize("error", [requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                                       ValueError({"error": "coin not found"}), KeyError("prices")])
    def test_a_failed_history_request_leaves_the_range_out(self, crypto_command, error, capsys):
        crypto_command.history.side_effect = error
        with patch.object(crypto_command.cg, "get_price", return_value=PRICE):
            result = crypto_command.execute("bitcoin")
        assert result.endswith("Volume 30.00B.")
        assert "!crypto: no 52-week range for bitcoin" in capsys.readouterr().out

    def test_no_history_request_when_the_price_lookup_failed(self, crypto_command):
        with patch.object(crypto_command.cg, "get_price", return_value={}):
            crypto_command.execute("notacoin")
        crypto_command.history.assert_not_called()

    @pytest.mark.parametrize("name", ["../simple/price", "bit coin", "bitcoin?x=1", "bitcoin/../x", "bitcoin#"])
    def test_a_name_outside_the_id_format_never_reaches_the_url_path(self, crypto_command, name):
        with patch.object(crypto_command.cg, "get_price", return_value={name: PRICE["bitcoin"]}):
            result = crypto_command.execute(name)
        crypto_command.history.assert_not_called()
        assert result.endswith("Volume 30.00B.")

    def test_both_requests_have_a_short_budget(self, crypto_command):
        # Commands run on the network thread: pycoingecko's 120 s and 5 retries could stall the bot for minutes.
        assert crypto_command.cg.request_timeout == PRICE_TIMEOUT_SECONDS <= 10
        assert crypto_command.history_cg.request_timeout == HISTORY_TIMEOUT_SECONDS <= 10
        assert crypto_command.cg.session.get_adapter("https://").max_retries.total == PRICE_RETRIES == 1
        assert crypto_command.history_cg.session.get_adapter("https://").max_retries.total == 0

    @pytest.mark.parametrize("chart", [
        None, [], "x", {}, {"prices": None}, {"prices": []}, {"prices": [[1], "x", None]},
        {"prices": [[1, None], [2, "5"], [3, True], [4, float("nan")], [5, float("inf")]]},
    ])
    def test_no_usable_price_gives_no_range(self, chart):
        assert year_range(chart) is None

    def test_unusable_points_are_skipped(self):
        assert year_range({"prices": [[1, 3.5], [2, None], [3, 1], [4, float("nan")], (5, 7.25)]}) == (1, 7.25)



class TestDecimals:
    @pytest.mark.parametrize("price, decimals", [
        (61316.0, 2), (1.0, 2), (0.9999, 4), (0.5, 4), (0.1, 4), (0.057147, 5), (0.01, 5), (0.0099, 6),
        (0.00000367, 9), (1e-11, 12), (1e-300, 12), (0, 2), (float("inf"), 2), (float("nan"), 2),
    ])
    def test_price_decimals(self, price, decimals):
        assert price_decimals(price) == decimals

    def test_a_coin_under_a_cent_shows_four_significant_digits(self, crypto_command):
        crypto_command.history.return_value = {"prices": [[1, 0.0000035805887], [2, 0.0000092500557]]}
        price = {"shiba-inu": {"usd": 0.00000367, "usd_24h_change": 2.1928684932455984, "usd_24h_vol": 0}}
        with patch.object(crypto_command.cg, "get_price", return_value=price):
            result = crypto_command.execute("shiba-inu")
        assert "0.000003670 USD, today " in result
        assert "+0.000000080 (+2.19%)" in result
        assert result.endswith("52W: 0.000003581 - 0.000009250.")

    def test_a_coin_under_a_dollar(self, crypto_command):
        crypto_command.history.return_value = {"prices": [[1, 0.054290546], [2, 0.154776947]]}
        price = {"dogecoin": {"usd": 0.057147, "usd_24h_change": 3.638356151658611, "usd_24h_vol": 0}}
        with patch.object(crypto_command.cg, "get_price", return_value=price):
            result = crypto_command.execute("dogecoin")
        assert "0.05715 USD, today " in result and "+0.00208 (+3.64%)" in result
        assert result.endswith("52W: 0.05429 - 0.15478.")
