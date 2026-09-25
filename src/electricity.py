import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from base_command import BaseCommand
from http_session import DEFAULT_TIMEOUT_SECONDS, make_session
from request_errors import format_request_error


class ElectricityCommand(BaseCommand):
    """Fetches electricity prices in Finland for the current date and time with 15-minute resolution."""

    ALIASES = ("!sähkö", "!sahko")
    ALLOW_ARGS = False

    HELSINKI_TZ = ZoneInfo("Europe/Helsinki")

    def __init__(self):
        self.session = make_session("KukistiBot-Electricity/1.0")
        # Only one ElectricityCommand instance ever exists (command_handler.py
        # creates it once), so an instance-level cache behaves identically
        # to the class-level one this used to be - just without the
        # self.__class__ indirection, which read as sharing across
        # instances that was never actually relevant here.
        self._cached_result = None
        self._cache_until_timestamp = 0  # Use timestamp for faster comparison

    def execute(self, args=None):
        try:
            # Get current time in Helsinki timezone
            now = datetime.datetime.now(self.HELSINKI_TZ)
            now_timestamp = now.timestamp()
            
            # Fast cache check using timestamp comparison
            if self._cached_result is not None and now_timestamp < self._cache_until_timestamp:
                return self._cached_result

            # Convert to UTC and format as ISO 8601 with Z suffix
            now_utc = now.astimezone(datetime.timezone.utc)
            iso_timestamp = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

            # API URL with ISO 8601 UTC timestamp
            url = f"https://api.porssisahko.net/v2/price.json?date={iso_timestamp}"

            # Fetch data from API with a timeout
            response = self.session.get(url, timeout=DEFAULT_TIMEOUT_SECONDS)
            response.raise_for_status()  # Raise exception for HTTP errors
            
            # Parse JSON response safely
            try:
                data = response.json()
            except ValueError:
                return "Error: Could not parse electricity price data."

            # Validate response structure
            if not isinstance(data, dict) or "price" not in data:
                return "Error: Unexpected data format from electricity API."

            # Extract and validate electricity price
            price = data.get("price")
            if not isinstance(price, (int, float)):
                return "Error: Invalid price data received."

            # Round using ROUND_HALF_UP (traditional rounding: 5.125 -> 5.13)
            rounded_price = Decimal(str(price)).quantize(Decimal("0.01"), ROUND_HALF_UP)
            result = f"{rounded_price} snt / kWh"
            
            # Next quarter-hour boundary, computed on the UTC epoch rather
            # than by adding a wall-clock timedelta to `now`: Finland's
            # UTC offset is always a whole number of hours, so epoch and
            # local quarter-hour boundaries coincide, but wall-clock
            # arithmetic doesn't - on DST fall-back night, adding an hour
            # to a wall-clock time can jump across the repeated hour,
            # caching a price for ~70 real minutes instead of ~15.
            self._cached_result = result
            self._cache_until_timestamp = (int(now_timestamp) // 900 + 1) * 900
            
            return result

        except Exception as e:
            return format_request_error(e, "Electricity price API")
