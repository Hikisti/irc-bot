import yfinance as yf

class StockCommand:
    def execute(self, args):
        """Handles stock queries, ensuring correct calculations."""
        if not args:
            return "Please provide a stock ticker (e.g., !stock TSLA)."

        symbol = args.strip().upper()

        try:
            stock = yf.Ticker(symbol)
            data = stock.history(period="1d")  # Get today's stock data
            
            if data.empty:
                return f"Could not retrieve data for stock '{symbol}'."

            price = stock.info.get("regularMarketPrice", 0)  # Current stock price
            prev_close = stock.info.get("regularMarketPreviousClose", 0)  # Previous close price
            volume = stock.info.get("regularMarketVolume", 0) / 1_000  # Convert to thousands

            # Ensure correct change calculations
            change_currency = price - prev_close  
            change_percent = (change_currency / prev_close) * 100 if prev_close else 0  

            # Correct color codes
            color = "\x0309" if change_currency >= 0 else "\x0304"  # Bright Green for positive, Bright Red for negative

            return f"\x02{stock.info.get('shortName', symbol)} ({symbol}):\x02 {price:.2f} USD, today {color}{change_currency:+.2f} ({change_percent:+.2f}%)\x03. Volume {volume:.2f}k."

        except Exception as e:
            return f"Error fetching stock data: {e}"
