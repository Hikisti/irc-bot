import yfinance as yf

class StockCommand:
    """Fetches stock price and market data for a given ticker symbol."""

    def execute(self, args):
        """Handles stock queries with improved error handling and currency support."""
        if not args:
            return "Usage: !stock <ticker> (e.g., !stock TSLA)."

        symbol = args.strip().upper()

        try:
            stock = yf.Ticker(symbol)

            # Try to get basic price data first
            info = stock.info
            if not info or "regularMarketPrice" not in info:
                return f"Error: Stock information unavailable for '{symbol}'."

            price = info.get("regularMarketPrice")
            prev_close = info.get("regularMarketPreviousClose")
            volume = info.get("regularMarketVolume", 0)
            currency = info.get("currency", "USD")
            short_name = info.get("shortName", symbol)

            # Check required fields
            if price is None or prev_close is None:
                return f"Error: Market data incomplete for '{symbol}'."

            # Calculate price changes
            change_currency = price - prev_close
            change_percent = (change_currency / prev_close) * 100 if prev_close else 0
            volume_k = volume / 1_000 if volume else 0

            # IRC color: green or red
            color = "\x0309" if change_currency >= 0 else "\x0304"

            return (
                f"\x02{short_name} ({symbol}):\x02 {price:.2f} {currency}, "
                f"today {color}{change_currency:+.2f} ({change_percent:+.2f}%)\x03. "
                f"Volume {volume_k:.2f}k."
            )

        except (ValueError, TypeError):
            return f"Error: Invalid stock symbol '{symbol}'."
        except (ConnectionError, TimeoutError):
            return "Error: Network issue while retrieving stock data."
        except Exception as e:
            return f"Error: Could not retrieve stock data for '{symbol}'. ({str(e)})"
