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
            # Handle cryptocurrency
            try:
                data = self.cg.get_price(ids=symbol, vs_currencies='usd')
                if symbol in data:
                    price = data[symbol]['usd']
                    return f"The current price of {symbol.capitalize()} is ${price:.2f}."
                else:
                    return f"Could not retrieve data for cryptocurrency '{symbol}'."
            except Exception as e:
                return f"An error occurred while fetching cryptocurrency data: {e}"
        else:
            # Handle stock
            try:
                stock = yf.Ticker(symbol.upper())
                todays_data = stock.history(period='1d')
                if not todays_data.empty:
                    price = todays_data['Close'][0]
                    return f"The current price of {symbol.upper()} is ${price:.2f}."
                else:
                    return f"No data found for stock symbol '{symbol.upper()}'."
            except Exception as e:
                return f"An error occurred while fetching stock data: {e}"

    def get_supported_cryptos(self):
        """Fetch the list of supported cryptocurrencies from CoinGecko."""
        try:
            crypto_list = self.cg.get_coins_list()
            return {coin['id']: coin['symbol'] for coin in crypto_list}
        except Exception as e:
            print(f"Error fetching supported cryptocurrency list: {e}")
            return {}

# Example usage:
# stock_command = StockCommand()
# print(stock_command.execute("TSLA"))  # For stock
# print(stock_command.execute("bitcoin"))  # For cryptocurrency
