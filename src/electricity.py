import requests
import datetime

class ElectricityCommand:
    """Fetches electricity prices in Finland for the current date and hour."""

    def execute(self, args=None):
        try:
            # Get current date and hour
            now = datetime.datetime.now()
            date = now.strftime("%Y-%m-%d")
            hour = now.strftime("%H")  # 24-hour format

            # API URL with formatted date and hour
            URL = "https://api.porssisahko.net/v1/price.json?date={0}&hour={1}".format(date, hour)
            
            # Fetch data from API
            response = requests.get(URL, timeout=5)  # 5-second timeout
            response.raise_for_status()  # Raise exception for HTTP errors
            
            # Parse JSON response
            data = response.json()
            
            # Extract electricity price
            if "price" not in data:
                return "Error: Price data not found in API response."
            
            price = data["price"]  # Assuming API returns price in cents
            return "{0} snt / kWh".format(price)

        except requests.exceptions.RequestException as e:
            return f"Error fetching electricity prices: {e}"
        except ValueError:
            return "Error: Invalid response format from electricity API."
        except Exception as e:
            return f"Unexpected error: {e}"
