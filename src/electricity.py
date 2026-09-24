import requests
import datetime
import pytz
from decimal import Decimal, ROUND_HALF_UP

from request_errors import format_request_error


class ElectricityCommand:
    """Fetches electricity prices in Finland for the current date and time with 15-minute resolution."""

    HELSINKI_TZ = pytz.timezone("Europe/Helsinki")

    def __init__(self):
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
            now_utc = now.astimezone(pytz.UTC)
            iso_timestamp = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

            # API URL with ISO 8601 UTC timestamp
            url = f"https://api.porssisahko.net/v2/price.json?date={iso_timestamp}"

            # Fetch data from API with a timeout
            response = requests.get(url, timeout=5)
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
            
            # Calculate next quarter hour and cache
            minute = now.minute
            next_quarter = ((minute // 15) + 1) * 15
            
            if next_quarter == 60:
                next_time = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
            else:
                next_time = now.replace(minute=next_quarter, second=0, microsecond=0)
            
            # Store result and expiration as timestamp for faster comparison
            self._cached_result = result
            self._cache_until_timestamp = next_time.timestamp()
            
            return result

        except Exception as e:
            return format_request_error(e, "Electricity price API")
