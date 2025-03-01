import yfinance as yf
from pycoingecko import CoinGeckoAPI

class StockCommand:
    def __init__(self):
        self.cg = CoinGeckoAPI()

    def execute(self, args):
        if not args:
            return "Please provide a stock ticker or cryptocurrency symbol (e.g., !stock TSLA or !stock BTC-USD)."

        symbol = args.strip().upper()

        if '-' in symbol:
            # Handle cryptocurrency
            try:
                crypto_id = self.get_crypto_id(symbol)
                if not crypto_id:
                    return f"Cryptocurrency symbol '{symbol}' not recognized."
                
                data = self.cg.get_price(ids=crypto_id, vs_currencies='usd')
                if crypto_id in data:
                    price = data[crypto_id]['usd']
                    return f"The current price of {symbol} is ${price:.2f}."
                else:
                    return f"Could not retrieve data for cryptocurrency symbol '{symbol}'."
            except Exception as e:
                return f"An error occurred while fetching cryptocurrency data: {e}"
        else:
            # Handle stock
            try:
                stock = yf.Ticker(symbol)
                todays_data = stock.history(period='1d')
                if not todays_data.empty:
                    price = todays_data['Close'][0]
                    return f"The current price of {symbol} is ${price:.2f}."
                else:
                    return f"No data found for stock symbol '{symbol}'."
            except Exception as e:
                return f"An error occurred while fetching stock data: {e}"

    def get_crypto_id(self, symbol):
        # CoinGecko uses lowercase IDs for cryptocurrencies
        symbol = symbol.lower()
        # Common cryptocurrency IDs on CoinGecko
        crypto_ids = {
            'btc-usd': 'bitcoin',
            'eth-usd': 'ethereum',
            'ltc-usd': 'litecoin',
            'xrp-usd': 'ripple',
            'ada-usd': 'cardano',
            # Add more mappings as needed
        }
        return crypto_ids.get(symbol)

# Example usage:
# stock_command = StockCommand()
# print(stock_command.execute("TSLA"))  # For stock
# print(stock_command.execute("BTC-USD"))  # For cryptocurrency
