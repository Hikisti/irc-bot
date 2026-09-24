import requests
import yfinance as yf

from base_command import BaseCommand
from irc_format import BOLD, RESET, signed_change_color
from request_errors import format_request_error

class StockCommand(BaseCommand):
    """Fetches stock price and market data for a given ticker symbol."""

    ALIASES = ("!stock",)

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
            color = signed_change_color(change_currency)

            return (
                f"{BOLD}{short_name} ({symbol}):{BOLD} {price:.2f} {currency}, "
                f"today {color}{change_currency:+.2f} ({change_percent:+.2f}%){RESET}. "
                f"Volume {volume_k:.2f}k."
            )

        except (ValueError, TypeError):
            return f"Error: Invalid stock symbol '{symbol}'."
        except requests.exceptions.RequestException as e:
            # yfinance makes its actual HTTP calls via requests under the
            # hood, so its network errors surface as
            # requests.exceptions.* - not the builtin ConnectionError/
            # TimeoutError this used to (and could never actually) catch.
            return format_request_error(e, "Yahoo Finance")
        except Exception as e:
            return f"Error: Could not retrieve stock data for '{symbol}'. ({str(e)})"
