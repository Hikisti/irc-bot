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

        """Determines whether the symbol is a stock or a cryptocurrency and fetches data accordingly."""
        stock_data = self.get_stock_price(symbol.upper())
        
        if stock_data:
            return stock_data  # If stock is found, return it
        
        crypto_data = self.get_crypto_price(symbol)

        if symbol in self.crypto_list:
            if crypto_data:
                return crypto_data  # Otherwise, return crypto data
        
        return f"Could not find stock or crypto for '{symbol}'." 

    def get_stock_price(self, symbol):
        """Fetch stock price, ensuring correct percentage and absolute price change."""
        try:
            stock = yf.Ticker(symbol)
            data = stock.history(period="1d")  # Get today's stock data
            
            if data.empty:
                return f"Could not retrieve data for stock '{symbol}'."

            price = stock.info.get("regularMarketPrice", 0)  # Current stock price
            prev_close = stock.info.get("regularMarketPreviousClose", 0)  # Previous close price
            volume = stock.info.get("regularMarketVolume", 0) / 1_000  # Convert to thousands

            # Ensure the change calculation is correct
            change_currency = price - prev_close  # Absolute change
            change_percent = (change_currency / prev_close) * 100 if prev_close else 0  # Percentage change

            # Ensure correct color codes
            color = "\x0309" if change_currency >= 0 else "\x0304"  # Bright Green for positive, Bright Red for negative

            return f"\x02{stock.info.get('shortName', symbol)} ({symbol}):\x02 {price:.2f} USD, today {color}{change_currency:+.2f} ({change_percent:+.2f}%)\x03. Volume {volume:.2f}k."

        except Exception as e:
            return f"Error fetching stock data: {e}"

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
