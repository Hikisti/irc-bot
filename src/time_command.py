import os
import requests

class TimeCommand:
    """
    Fetches local time for a given city for IRC bot usage.
    Uses IPGeolocation Timezone API (free tier: 30k req/month).

    Example:
      !time austin
      -> Local time in Austin, United States of America: 02/01/26 13:00:39.
    """

    API_URL = "https://api.ipgeolocation.io/timezone"

    def __init__(self):
        self.api_key = os.getenv("TIME_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "KukistiBot-Time/1.0"
        })

    def execute(self, city_name: str) -> str:
        city_name = city_name.strip()
        if not city_name:
            return "Error: Please provide a city name, e.g. !time austin"

        if not self.api_key:
            return "Error: TIME_API_KEY is not set in environment."

        try:
            resp = self.session.get(
                self.API_URL,
                params={"apiKey": self.api_key, "location": city_name},
                timeout=5,
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            return "Error: Time service request timed out."
        except requests.exceptions.ConnectionError:
            return "Error: Could not connect to time service."
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            reason = e.response.reason if e.response is not None else "Unknown error"
            return f"Error: Time service returned HTTP {status} {reason}."
        except requests.exceptions.RequestException as e:
            return f"Error: Failed to contact time service: {e}."
        except Exception as e:
            return f"Error: Unexpected issue while fetching time: {e}"

        try:
            data = resp.json()
        except ValueError:
            return "Error: Invalid response from time service"

        if not isinstance(data, dict) or "time_24" not in data:
            error_msg = data.get("error") or data.get("message")
            if error_msg:
                return f"Error: {error_msg}"
            return "Error: Time service returned unexpected data"

        date_str = data.get("date", "")
        time_str = data.get("time_24", "")

        location_info = (
            data.get("location") or 
            data.get("geo") or 
            {}
        )
        
        city_from_api = (
            location_info.get("city") or
            location_info.get("location_string") or
            location_info.get("city_name") or
            city_name
        )
        
        country_name = (
            location_info.get("country_name") or
            location_info.get("country") or
            data.get("country_name") or
            None
        )

        city_display = city_from_api.strip()
        
        # Remove trailing country name if city already contains it to avoid duplication
        if country_name and city_display.lower().endswith(", " + country_name.lower()):
            city_display = city_display[: -(len(", " + country_name))].rstrip()
        
        if city_display:
            city_display = city_display[0].upper() + city_display[1:]

        if country_name:
            location = f"{city_display}, {country_name}"
        else:
            location = city_display

        formatted_date = date_str
        if len(date_str) == 10 and date_str.count("-") == 2:
            yyyy, mm, dd = date_str.split("-")
            formatted_date = f"{dd}/{mm}/{yyyy[2:]}"

        if len(time_str) == 5:
            time_str = time_str + ":00"

        return f"Local time in {location}: {formatted_date} {time_str}"
