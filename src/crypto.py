from pycoingecko import CoinGeckoAPI
import requests

class CryptoCommand:
    """Handles cryptocurrency price queries using CoinGecko API."""

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
            try:
                price = data[crypto_name].get(currency)
                change_percent = data[crypto_name].get(f"{currency}_24h_change")
                volume = data[crypto_name].get(f"{currency}_24h_vol", 0) / 1_000_000_000  # Convert to billions

                if price is None or change_percent is None:
                    return f"Error: Incomplete data for '{crypto_name}'. Try again later."
                
                # Correct absolute change calculation
                change_currency = (price * change_percent) / 100

            except KeyError:
                return f"Error: Unexpected API response for '{crypto_name}'."

            # Choose IRC color formatting
            color = "\x0309" if change_currency >= 0 else "\x0304"  # Green for positive, Red for negative

            return f"\x02{crypto_name.capitalize()} {currency.upper()}:\x02 {price:.2f} {currency.upper()}, today {color}{change_currency:+.2f} ({change_percent:+.2f}%)\x03. Volume {volume:.2f}B."

        except requests.exceptions.HTTPError as e:
            return f"Error: HTTP error from CoinGecko ({e.response.status_code}). Try again later."
        except requests.exceptions.ConnectionError:
            return "Error: Cannot connect to CoinGecko. Check your internet connection."
        except requests.exceptions.Timeout:
            return "Error: Request to CoinGecko timed out. Try again later."
        except requests.exceptions.RequestException:
            return "Error: Failed to fetch cryptocurrency data. Try again later."
        except Exception as e:
            return f"Unexpected error: {e}"
