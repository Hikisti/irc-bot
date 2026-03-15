import requests
import datetime
import pytz


class F1Command:
    """
    Returns the ongoing and/or next F1 event (practice, qualifying, sprint or race)
    with location, date and time in Finnish timezone.

    Usage:
      !f1
      -> Ongoing: Chinese GP (Race) - Shanghai, China | Sun 15/03/26 09:00 EET || Next: Japanese GP (Practice 1) - Suzuka, Japan | Fri 27/03/26 04:30 EET
    """

    JOLPICA_URL = "https://api.jolpi.ca/ergast/f1"
    HELSINKI_TZ = pytz.timezone("Europe/Helsinki")
    RACE_DURATION_HOURS = 2

    SESSION_ORDER = [
        ("FirstPractice",  "Practice 1"),
        ("SecondPractice", "Practice 2"),
        ("ThirdPractice",  "Practice 3"),
        ("Sprint",         "Sprint"),
        ("Qualifying",     "Qualifying"),
    ]

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "KukistiBot-F1/1.0",
            "Accept": "application/json",
        })

    def execute(self, args=None) -> str:
        try:
            year = datetime.datetime.now(self.HELSINKI_TZ).year
            resp = self.session.get(
                f"{self.JOLPICA_URL}/{year}/races.json",
                params={"limit": 30},
                timeout=5,
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            return "Error: F1 API request timed out."
        except requests.exceptions.ConnectionError:
            return "Error: Could not connect to F1 API."
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "unknown"
            reason = e.response.reason if e.response else "Unknown"
            return f"Error: F1 API returned HTTP {status} {reason}."
        except requests.exceptions.RequestException as e:
            return f"Error: F1 API request failed: {e}."
        except Exception as e:
            return f"Error: Unexpected issue fetching F1 data: {e}."

        try:
            data = resp.json()
        except ValueError:
            return "Error: Could not parse F1 API response."

        try:
            races = data["MRData"]["RaceTable"]["Races"]
        except (KeyError, TypeError):
            return "Error: Unexpected F1 API response format."

        if not isinstance(races, list):
            return "Error: Invalid race data received."

        if not races:
            return "No F1 races found for current season."

        try:
            now = datetime.datetime.now(pytz.UTC)
            all_events = []

            for race in races:
                if not isinstance(race, dict):
                    continue
                for key, label in self.SESSION_ORDER:
                    if key in race:
                        dt = self._parse_session_dt(race[key])
                        if dt:
                            all_events.append((label, dt, race))
                race_dt = self._parse_dt(race.get("date"), race.get("time"))
                if race_dt:
                    all_events.append(("Race", race_dt, race))

            all_events.sort(key=lambda x: x[1])

        except Exception as e:
            return f"Error: Failed to process F1 schedule: {e}."

        if not all_events:
            return "No F1 sessions found for current season."

        ongoing_event = None
        next_event = None

        try:
            for i, (label, dt, race_info) in enumerate(all_events):
                end_dt = dt + datetime.timedelta(hours=self.RACE_DURATION_HOURS)

                if dt <= now < end_dt:
                    ongoing_event = (label, dt, race_info)
                    if i + 1 < len(all_events):
                        next_event = all_events[i + 1]
                    break

                if dt > now and next_event is None:
                    next_event = (label, dt, race_info)
                    break
        except Exception as e:
            return f"Error: Failed to determine next F1 event: {e}."

        parts = []

        try:
            if ongoing_event:
                parts.append("Ongoing: " + self._format_event(*ongoing_event))
            if next_event:
                parts.append("Next: " + self._format_event(*next_event))
        except Exception as e:
            return f"Error: Failed to format F1 event: {e}."

        if not parts:
            return "No upcoming F1 events found."

        return " || ".join(parts)

    def _format_event(self, label, dt, race_info) -> str:
        local_dt = dt.astimezone(self.HELSINKI_TZ)
        race_name = race_info.get("raceName", "Unknown GP")
        circuit = race_info.get("Circuit", {})
        location = circuit.get("Location", {})
        locality = location.get("locality", "")
        country = location.get("country", "")
        place = f"{locality}, {country}" if locality and country else locality or country
        date_str = local_dt.strftime("%a %d/%m/%y %H:%M")
        tz_abbr = local_dt.strftime("%Z")
        return f"{race_name} ({label}) - {place} | {date_str} {tz_abbr}"

    def _parse_session_dt(self, session_dict: dict):
        if not isinstance(session_dict, dict):
            return None
        return self._parse_dt(session_dict.get("date"), session_dict.get("time"))

    def _parse_dt(self, date_str, time_str):
        if not date_str:
            return None
        try:
            time_part = (time_str or "00:00:00Z").rstrip("Z")
            dt_str = f"{date_str}T{time_part}+00:00"
            return datetime.datetime.fromisoformat(dt_str)
        except (ValueError, AttributeError):
            return None
