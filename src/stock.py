import math

import curl_cffi.requests.exceptions as curl_exceptions
import requests
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from base_command import BaseCommand
from irc_format import BOLD, RESET, signed_change_color
from request_errors import format_request_error

class StockCommand(BaseCommand):
    """Fetches stock price and market data for a given ticker symbol."""

    ALIASES = ("!stock",)
    HELP = "!stock <ticker>"

    @staticmethod
    def _is_number(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def _change(self, info, price, prev_close):
        """(change, percent): Yahoo's own figures when it gives both, else price against the previous close."""
        change, percent = info.get("regularMarketChange"), info.get("regularMarketChangePercent")
        if self._is_number(change) and self._is_number(percent):
            return change, percent
        change = price - prev_close
        return change, (change / prev_close) * 100 if prev_close else 0

    @staticmethod
    def _decimals(price) -> int:
        """Decimals that keep a small price readable: two from 1 up, four from a cent, six below."""
        price = abs(price)
        return 2 if price >= 1 else 4 if price >= 0.01 else 6

    @staticmethod
    def _format_volume(volume) -> str:
        if not volume:
            return "0.00k"
        if volume >= 1_000_000_000:
            return f"{volume / 1_000_000_000:.3f}B"
        if volume >= 1_000_000:
            return f"{volume / 1_000_000:.2f}M"
        return f"{volume / 1_000:.2f}k"

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

            change_currency, change_percent = self._change(info, price, prev_close)
            # Yahoo's own change is since the previous close for anything with a market session (the same figure as
            # price - previous close, checked on stocks, ETFs, indexes, a currency pair and futures), but for a
            # coin, which trades round the clock, the "previous close" is the start of the UTC day and the change
            # Yahoo reports is the rolling 24 hours, which is what people compare it with (#69).
            period = "24h" if info.get("quoteType") == "CRYPTOCURRENCY" else "today"
            decimals = self._decimals(price)

            # IRC color: green or red
            color = signed_change_color(change_currency)

            # The 52-week range comes in the same response (no extra request); left out when either end is missing.
            low, high = info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
            week_range = (
                f" 52W: {low:.{decimals}f} - {high:.{decimals}f}."
                if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (low, high))
                else ""
            )

            return (
                f"{BOLD}{short_name} ({symbol}):{BOLD} {price:.{decimals}f} {currency}, "
                f"{period} {color}{change_currency:+.{decimals}f} ({change_percent:+.2f}%){RESET}. "
                f"Volume {self._format_volume(volume)}.{week_range}"
            )

        except (ValueError, TypeError):
            return f"Error: Invalid stock symbol '{symbol}'."
        except YFRateLimitError:
            return "Error: Yahoo Finance rate limit exceeded, try again later."
        except (requests.exceptions.RequestException, curl_exceptions.RequestException) as e:
            # yfinance 1.x does its HTTP via curl_cffi by default, only
            # falling back to requests if curl_cffi isn't installed - its
            # network errors can surface as either hierarchy, not the
            # builtin ConnectionError/TimeoutError this used to (and could
            # never actually) catch. format_request_error() understands
            # both (see request_errors.py).
            return format_request_error(e, "Yahoo Finance")
        except Exception as e:
            return f"Error: Could not retrieve stock data for '{symbol}'. ({str(e)})"
