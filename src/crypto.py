from pycoingecko import CoinGeckoAPI

from base_command import BaseCommand
from irc_format import BOLD, RESET, signed_change_color
from request_errors import format_request_error

class CryptoCommand(BaseCommand):
    """Handles cryptocurrency price queries using CoinGecko API."""

    ALIASES = ("!crypto",)

    def __init__(self):
        self.cg = CoinGeckoAPI()

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
            )

        except Exception as e:
            return format_request_error(e, "CoinGecko")
