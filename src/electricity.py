import requests
import datetime
import pytz


class ElectricityCommand:
    """Fetches electricity prices in Finland for the current date and time with 15-minute resolution."""

    def execute(self, args=None):
        try:
            # Define Helsinki timezone
            helsinki_tz = pytz.timezone("Europe/Helsinki")

            # Get current time in Helsinki timezone
            now = datetime.datetime.now(helsinki_tz)
            
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

            return f"{price:.2f} snt / kWh"

        except requests.exceptions.Timeout:
            return "Error: Electricity price request timed out. Please try again later."
        except requests.exceptions.ConnectionError:
            return "Error: Unable to connect to the electricity price API."
        except requests.exceptions.HTTPError as e:
            return f"Error: API returned {e.response.status_code} {e.response.reason}."
        except requests.exceptions.RequestException:
            return "Error: Failed to retrieve electricity price data."
        except Exception:
            return "Error: An unexpected issue occurred while fetching electricity prices."
