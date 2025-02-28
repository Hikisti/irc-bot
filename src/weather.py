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
            response.raise_for_status()
            data = response.json()

            if "location" not in data or "current" not in data:
                return f"Error: Unexpected response from weather API."

            location = data["location"]["name"]
            country = data["location"]["country"]
            condition = data["current"]["condition"]["text"]
            temperature_celsius = data["current"]["temp_c"]
            temperature_celsius_feelslike = data["current"]["feelslike_c"]
            wind_kph = data["current"]["wind_kph"]
            wind_dir = data["current"]["wind_dir"]

            return "Current weather in {0}, {1}: {2}, {3} °C (feels like {4} °C). Wind: {5} {6:.1f} m/s.".format(
                location, country, condition, temperature_celsius, temperature_celsius_feelslike, wind_dir, wind_kph / 3.6
            )

        except requests.exceptions.RequestException as e:
            return f"Error fetching weather data: {e}"
        except (ValueError, KeyError):
            return "Error: Could not parse weather data."
        except Exception as e:
            return f"Unexpected error: {e}"
