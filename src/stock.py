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
        """Fetch stock data using Yahoo Finance."""
        try:
            stock = yf.Ticker(symbol)
            info = stock.info
            history = stock.history(period="1d")

            if history.empty:
                return f"No data found for stock symbol '{symbol}'."

            name = info.get("longName", symbol)  # Get full name, fallback to symbol
            currency = info.get("currency", "N/A")
            price = history["Close"][0]  # Last closing price
            change = history["Close"][0] - history["Open"][0]  # Price change
            percent_change = (change / history["Open"][0]) * 100 if history["Open"][0] else 0
            volume = info.get("volume", 0) / 1000  # Convert to thousands

            color = "\x033" if change >= 0 else "\x034"  # Green for positive, red for negative
            return f"\x02{name} ({symbol}):\x02 {price:.2f} {currency}, today {color}{change:.2f} ({percent_change:.2f}%)\x03. Volume {volume:.2f}k."

        except Exception as e:
            return f"Error fetching stock data: {e}"

    def get_crypto_price(self, crypto_name):
        """Fetch cryptocurrency price from CoinGecko."""
        try:
            data = self.cg.get_price(ids=crypto_name, vs_currencies="usd", include_24hr_change="true", include_24hr_vol="true")

            if crypto_name not in data:
                return f"Could not retrieve data for cryptocurrency '{crypto_name}'."

            price = data[crypto_name]["usd"]
            change = data[crypto_name].get("usd_24h_change", 0)
            volume = data[crypto_name].get("usd_24h_vol", 0) / 1_000_000_000  # Convert to billions

            color = "\x033" if change >= 0 else "\x034"  # Green for positive, red for negative
            return f"\x02{crypto_name.capitalize()} USD:\x02 {price:,.2f} USD, today {color}{change:.2f} ({change:.2f}%)\x03. Volume {volume:.2f}B."

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
