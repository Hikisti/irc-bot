import math
import re

from pycoingecko import CoinGeckoAPI

from base_command import BaseCommand
from irc_format import BOLD, RESET, signed_change_color
from request_errors import format_request_error

# Commands run on the bot's network thread, and pycoingecko waits up to 120 s with 5 retries. The range is
# an extra the reply can do without, so its request gets a short budget and no retries.
HISTORY_TIMEOUT_SECONDS = 10
# The history request puts the coin into the URL path as it stands (the price request sends it as a query
# parameter), so only a name in CoinGecko's id format goes there; anything else gets no range.
COIN_ID = re.compile(r"[a-z0-9-]+")


def year_range(chart):
    """(low, high) of the daily prices in a market_chart response, or None when it has no usable price.
    CoinGecko has no 52-week field, so the range comes from 365 days of daily prices (not intraday)."""
    points = chart.get("prices") if isinstance(chart, dict) else None
    if not isinstance(points, list):
        return None
    prices = [p[1] for p in points
              if isinstance(p, (list, tuple)) and len(p) >= 2
              and isinstance(p[1], (int, float)) and not isinstance(p[1], bool) and math.isfinite(p[1])]
    return (min(prices), max(prices)) if prices else None


class CryptoCommand(BaseCommand):
    """Handles cryptocurrency price queries using CoinGecko API."""

    ALIASES = ("!crypto",)
    HELP = "!crypto <coin>"

    def __init__(self):
        self.cg = CoinGeckoAPI()
        self.history_cg = CoinGeckoAPI(retries=0)
        self.history_cg.request_timeout = HISTORY_TIMEOUT_SECONDS

    def _year_range_text(self, crypto_name, currency):
        """' 52W: low - high.' in the price's format, or '' when the history cannot be had."""
        if not COIN_ID.fullmatch(crypto_name):
            return ""
        try:
            chart = self.history_cg.get_coin_market_chart_by_id(
                crypto_name, vs_currency=currency, days=365, interval="daily")
        except Exception as e:
            print(f"!crypto: no 52-week range for {crypto_name}: {type(e).__name__}")
            return ""
        found = year_range(chart)
        return f" 52W: {found[0]:.2f} - {found[1]:.2f}." if found else ""

    def execute(self, args):
        """Handles user request for cryptocurrency price."""
        if not args:
            return "Usage: !crypto <name> (e.g., !crypto bitcoin)."

        crypto_name = args.strip().lower()
        return self.get_crypto_price(crypto_name)

    def get_crypto_price(self, crypto_name, currency="usd"):
        """Fetch cryptocurrency price and handle errors properly."""
        try:
            data = self.cg.get_price(
                ids=crypto_name,
                vs_currencies=currency,
                include_24hr_change="true",
                include_24hr_vol="true"
            )

            if not data or crypto_name not in data:
                return f"Error: Cryptocurrency '{crypto_name}' not found. Check the name and try again."

            # Extract values safely
            price = data[crypto_name].get(currency)
            change_percent = data[crypto_name].get(f"{currency}_24h_change")
            volume = data[crypto_name].get(f"{currency}_24h_vol", 0) / 1_000_000_000  # Convert to billions

            if price is None or change_percent is None:
                return f"Error: Incomplete data for '{crypto_name}'. Try again later."

            # Correct absolute change calculation
            change_currency = (price * change_percent) / 100

            # Choose IRC color formatting
            color = signed_change_color(change_currency)

            return (
                f"{BOLD}{crypto_name.capitalize()} {currency.upper()}:{BOLD} {price:.2f} {currency.upper()}, "
                f"today {color}{change_currency:+.2f} ({change_percent:+.2f}%){RESET}. Volume {volume:.2f}B."
                f"{self._year_range_text(crypto_name, currency)}"
            )

        except Exception as e:
            return format_request_error(e, "CoinGecko")
