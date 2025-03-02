import yfinance as yf

class StockCommand:
    """Fetches stock price and market data for a given ticker symbol."""

    def execute(self, args):
        """Handles stock queries with better error handling."""
        if not args:
            return "Usage: !stock <ticker> (e.g., !stock TSLA)."

        symbol = args.strip().upper()

        try:
            stock = yf.Ticker(symbol)
            data = stock.history(period="1d")  # Fetch today's stock data
            
            if data.empty:
                return f"Error: No stock data available for '{symbol}'."

            # Extract stock information safely
            try:
                info = stock.info
                price = info.get("regularMarketPrice")
                prev_close = info.get("regularMarketPreviousClose")
                volume = info.get("regularMarketVolume", 0)  # Default to 0 if missing
            except Exception:
                return f"Error: Could not retrieve stock information for '{symbol}'."

            # Validate stock prices before calculations
            if price is None or prev_close is None:
                return f"Error: Market price data is missing for '{symbol}'."

            # Calculate price changes
            change_currency = price - prev_close
            change_percent = (change_currency / prev_close) * 100 if prev_close else 0

            # Convert volume to thousands for readability
            volume_k = volume / 1_000 if volume else 0

            # Choose color formatting for IRC messages
            color = "\x0309" if change_currency >= 0 else "\x0304"  # Green for positive, Red for negative

            return f"\x02{info.get('shortName', symbol)} ({symbol}):\x02 {price:.2f} USD, today {color}{change_currency:+.2f} ({change_percent:+.2f}%)\x03. Volume {volume_k:.2f}k."

        except ValueError:
            return f"Error: Invalid stock symbol '{symbol}'."
        except (ConnectionError, TimeoutError):
            return "Error: Unable to connect to stock data provider. Please try again later."
        except Exception:
            return f"Error: An unexpected issue occurred while retrieving stock data for '{symbol}'."
