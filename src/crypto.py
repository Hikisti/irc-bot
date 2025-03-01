from pycoingecko import CoinGeckoAPI

class CryptoCommand:
    def __init__(self):
        self.cg = CoinGeckoAPI()

    def execute(self, args):
        """Handles cryptocurrency queries using CoinGecko."""
        if not args:
            return "Please provide a cryptocurrency name (e.g., !crypto bitcoin)."

        crypto_name = args.strip().lower()
        return self.get_crypto_price(crypto_name)

    def get_crypto_price(self, crypto_name, currency="usd"):
        """Fetch cryptocurrency price with correct 24h absolute price change calculations."""
        try:
            data = self.cg.get_price(
                ids=crypto_name, 
                vs_currencies=currency, 
                include_24hr_change="true", 
                include_24hr_vol="true"
            )

            if crypto_name not in data:
                return f"Could not retrieve data for cryptocurrency '{crypto_name}'."

            price = data[crypto_name].get(currency, 0)
            change_percent = data[crypto_name].get(f"{currency}_24h_change", 0)  # % change
            change_currency = (price * change_percent) / 100  # Correct absolute change in currency
            volume = data[crypto_name].get(f"{currency}_24h_vol", 0) / 1_000_000_000  # Convert to billions

            # Correct color codes
            color = "\x0309" if change_currency >= 0 else "\x0304"  # Bright Green for positive, Bright Red for negative

            return f"\x02{crypto_name.capitalize()} {currency.upper()}:\x02 {price:.2f} {currency.upper()}, today {color}{change_currency:+.2f} ({change_percent:+.2f}%)\x03. Volume {volume:.2f}B."

        except Exception as e:
            return f"Error fetching cryptocurrency data: {e}"
