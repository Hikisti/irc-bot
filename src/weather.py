import os
from dotenv import load_dotenv
import requests

class WeatherCommand:
    """Fetches current weather for a given city."""

    def __init__(self):
        load_dotenv()  # Load environment variables from .env
        self.api_key = os.getenv("WEATHER_API_KEY")
        self.base_url = "http://api.weatherapi.com/v1/current.json"

        if not self.api_key:
            raise ValueError("Error: WEATHER_API_KEY is missing. Set it in the .env file.")

    def execute(self, args):
        if not args:
            return "Usage: !weather <city> or !weather <city>,<country>"

        try:
            # Parse city and optional country
            location_parts = args.split(",", 1)
            city = location_parts[0].strip()
            country = location_parts[1].strip() if len(location_parts) > 1 else ""

            # Construct API request
            query = f"{city},{country}" if country else city
            params = {"key": self.api_key, "q": query, "aqi": "no"}

            response = requests.get(self.base_url, params=params, timeout=5)
            response.raise_for_status()  # Raises error for HTTP 4xx and 5xx responses
            data = response.json()

            # Validate response structure
            if "location" not in data or "current" not in data:
                return "Error: Unexpected response from weather API."

            location = data["location"].get("name", "Unknown location")
            country = data["location"].get("country", "Unknown country")
            condition = data["current"].get("condition", {}).get("text", "Unknown condition")
            temp_c = data["current"].get("temp_c", "?")
            temp_f = data["current"].get("temp_f", "?")
            feels_like_c = data["current"].get("feelslike_c", "?")
            feels_like_f = data["current"].get("feelslike_f", "?")
            wind_kph = data["current"].get("wind_kph", 0)
            wind_dir = data["current"].get("wind_dir", "?")

            return (
                f"Current weather in {location}, {country}: {condition}, "
                f"{temp_c}°C ({temp_f}°F) (feels like {feels_like_c}°C/{feels_like_f}°F). "
                f"Wind: {wind_dir} {wind_kph / 3.6:.1f} m/s."
            )

        except requests.exceptions.HTTPError as e:
            return f"Error fetching weather data: {e.response.status_code} {e.response.reason}"
        except requests.exceptions.Timeout:
            return "Error: Weather API request timed out. Please try again later."
        except requests.exceptions.ConnectionError:
            return "Error: Unable to connect to the weather API."
        except requests.exceptions.RequestException:
            return "Error: Failed to retrieve weather data."
        except Exception:
            return "Error: An unexpected issue occurred while fetching weather data."
