import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

class TimeCommand:
    """
    Fetches local time for a given city, or a given timezone abbreviation,
    for IRC bot usage. City lookups use the IPGeolocation Timezone API
    (free tier: 30k req/month); timezone abbreviations are resolved locally.

    Example:
      !time austin
      -> Local time in Austin, United States of America: 02/01/26 13:00:39.
      !time cdt
      -> Local time in CDT (America/Chicago): 02/01/26 06:00:39.
    """

    API_URL = "https://api.ipgeolocation.io/timezone"

    # Timezone abbreviations are inherently ambiguous (e.g. CST could mean
    # US Central, China, or Cuba). This maps each to the most common
    # interpretation for casual use rather than trying to disambiguate.
    TIMEZONE_ABBREVIATIONS = {
        "UTC": "UTC",
        "GMT": "UTC",
        "BST": "Europe/London",
        "WET": "Europe/Lisbon",
        "WEST": "Europe/Lisbon",
        "CET": "Europe/Paris",
        "CEST": "Europe/Paris",
        "EET": "Europe/Helsinki",
        "EEST": "Europe/Helsinki",
        "MSK": "Europe/Moscow",
        "EST": "America/New_York",
        "EDT": "America/New_York",
        "CST": "America/Chicago",
        "CDT": "America/Chicago",
        "MST": "America/Denver",
        "MDT": "America/Denver",
        "PST": "America/Los_Angeles",
        "PDT": "America/Los_Angeles",
        "AKST": "America/Anchorage",
        "AKDT": "America/Anchorage",
        "HST": "Pacific/Honolulu",
        "AST": "America/Halifax",
        "ADT": "America/Halifax",
        "NST": "America/St_Johns",
        "NDT": "America/St_Johns",
        "IST": "Asia/Kolkata",
        "JST": "Asia/Tokyo",
        "KST": "Asia/Seoul",
        "AEST": "Australia/Sydney",
        "AEDT": "Australia/Sydney",
        "ACST": "Australia/Adelaide",
        "ACDT": "Australia/Adelaide",
        "AWST": "Australia/Perth",
        "NZST": "Pacific/Auckland",
        "NZDT": "Pacific/Auckland",
        "BRT": "America/Sao_Paulo",
        "ART": "America/Argentina/Buenos_Aires",
        "WAT": "Africa/Lagos",
        "CAT": "Africa/Harare",
        "EAT": "Africa/Nairobi",
        "SAST": "Africa/Johannesburg",
        "GST": "Asia/Dubai",
        "SGT": "Asia/Singapore",
        "PHT": "Asia/Manila",
        "ICT": "Asia/Bangkok",
        "PKT": "Asia/Karachi",
    }

    def __init__(self):
        self.api_key = os.getenv("TIME_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "KukistiBot-Time/1.0"
        })

    def execute(self, city_name: str) -> str:
        city_name = city_name.strip()
        if not city_name:
            return "Error: Please provide a city name or timezone, e.g. !time austin or !time cdt"

        abbr = city_name.upper()
        if abbr in self.TIMEZONE_ABBREVIATIONS:
            return self._time_for_abbreviation(abbr)

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

        timezone_name = data.get("timezone")
        tz_abbr = None
        if timezone_name and date_str and time_str:
            time_format = "%H:%M:%S" if time_str.count(":") == 2 else "%H:%M"
            try:
                naive_dt = datetime.strptime(f"{date_str} {time_str}", f"%Y-%m-%d {time_format}")
                tz_abbr = naive_dt.replace(tzinfo=ZoneInfo(timezone_name)).tzname()
            except Exception as e:
                print(f"Failed to resolve timezone abbreviation for '{timezone_name}': {e}")
                tz_abbr = None

        if len(time_str) == 5:
            time_str = time_str + ":00"

        timezone_suffix = f" {tz_abbr}" if tz_abbr else ""

        return f"Local time in {location}: {formatted_date} {time_str}{timezone_suffix}"

    def _time_for_abbreviation(self, abbr: str) -> str:
        iana_name = self.TIMEZONE_ABBREVIATIONS[abbr]
        try:
            now = datetime.now(ZoneInfo(iana_name))
        except Exception as e:
            print(f"Failed to resolve timezone '{iana_name}' for abbreviation '{abbr}': {e}")
            return f"Error: Could not resolve timezone for {abbr}."

        formatted_date = now.strftime("%d/%m/%y")
        formatted_time = now.strftime("%H:%M:%S")

        actual_abbr = now.tzname() or abbr
        # Zones without a named abbreviation (e.g. no DST history) report a
        # raw UTC offset like "-03" instead of a name - that's not a real
        # mismatch with the requested abbreviation, so don't flag it.
        note = ""
        if actual_abbr != abbr and not actual_abbr.lstrip("+-").isdigit():
            note = f" (currently observing {actual_abbr}, not {abbr})"

        return f"Local time in {abbr} ({iana_name}): {formatted_date} {formatted_time}{note}"
