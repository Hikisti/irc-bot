import yfinance as yf
from pycoingecko import CoinGeckoAPI

class StockCommand:
    def __init__(self):
        self.cg = CoinGeckoAPI()
        self.crypto_list = self.get_supported_cryptos()

    def execute(self, args):
        if not args:
            return "Please provide a stock ticker or cryptocurrency name (e.g., !stock TSLA or !stock bitcoin)."

        symbol = args.strip().lower()

        if symbol in self.crypto_list:
            return self.get_crypto_price(symbol)
        else:
            return self.get_stock_price(symbol.upper())

    def get_stock_price(self, symbol):
        """Fetch stock price with correct colors and today's change."""
        try:
            stock = yf.Ticker(symbol)
            info = stock.info

            if "regularMarketPrice" not in info:
                return f"Could not retrieve data for stock '{symbol}'."

            name = info.get("shortName", symbol)
            price = info["regularMarketPrice"]
            currency = info.get("currency", "Unknown")
            change = info.get("regularMarketChange", 0)  # Use the correct field
            change_percent = info.get("regularMarketChangePercent", 0) * 100  # Convert to percentage
            volume = info.get("regularMarketVolume", 0) / 1_000  # Convert to thousands

            # Ensure correct color codes
            color = "\x0309" if change >= 0 else "\x0304"  # Bright Green for positive, Bright Red for negative
            
            return f"\x02{name} ({symbol}):\x02 {price:,.2f} {currency}, today {color}{change:+.2f} ({change_percent:+.2f}%)\x03. Volume {volume:.2f}k."

        except Exception as e:
            return f"Error fetching stock data: {e}"

    def get_crypto_price(self, crypto_name, currency="usd"):
        """Fetch cryptocurrency price in the specified currency with proper formatting and colors."""
        try:
            data = self.cg.get_price(
                ids=crypto_name, 
                vs_currencies=currency, 
                include_24hr_change="true", 
                include_24hr_vol="true", 
                include_market_cap="false",
                include_last_updated_at="true"
            )

            if crypto_name not in data:
                return f"Could not retrieve data for cryptocurrency '{crypto_name}'."

            price = data[crypto_name].get(currency, 0)
            change_currency = data[crypto_name].get("price_change_24h", 0)  # 24h change in currency value
            change_percent = data[crypto_name].get(f"{currency}_24h_change", 0)  # 24h change in percentage
            volume = data[crypto_name].get(f"{currency}_24h_vol", 0) / 1_000_000_000  # Convert to billions

            # Ensure correct color codes
            color = "\x0309" if change_currency >= 0 else "\x0304"  # Bright Green for positive, Bright Red for negative

            return f"\x02{crypto_name.capitalize()} {currency.upper()}:\x02 {price:.2f} {currency.upper()}, today {color}{change_currency:+.2f} ({change_percent:+.2f}%)\x03. Volume {volume:.2f}B."

        except Exception as e:
            return f"Error fetching cryptocurrency data: {e}"
        
    def get_supported_cryptos(self):
        """Fetch the list of supported cryptocurrencies from CoinGecko."""
        try:
            crypto_list = self.cg.get_coins_list()
            return {coin["id"]: coin["symbol"] for coin in crypto_list}
        except Exception as e:
            print(f"Error fetching supported cryptocurrency list: {e}")
            return {}

# Example usage:
# stock_command = StockCommand()
# print(stock_command.execute("EUNL.DE"))  # Stock
# print(stock_command.execute("ethereum"))  # Crypto
