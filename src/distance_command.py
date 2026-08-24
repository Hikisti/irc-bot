import os

from dotenv import load_dotenv
import requests


class DistanceCommand:
    """
    Calculates driving distance (and drive time) between two cities
    anywhere in the world, via OpenRouteService - one API key covers both
    geocoding (resolving a city name to coordinates) and routing.

    Usage:
      !distance <city1>,<city2>   -> works for any city names, comma required
                                      once either name has more than one word
      !distance <city1> <city2>  -> shortcut for two single-word city names,
                                      e.g. !distance Kokkola Vimpeli

    Example:
      !distance Kokkola,Vimpeli
      -> Driving distance from Kokkola, Finland to Vimpeli, Finland:
         88.4 km (54.9 mi), ~1h 12min drive.
    """

    GEOCODE_URL = "https://api.openrouteservice.org/geocode/search"
    DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions/driving-car"
    REQUEST_TIMEOUT_SECONDS = 10

    USAGE = (
        "Usage: !distance <city1>,<city2> "
        "(or !distance <city1> <city2> for two single-word city names)"
    )

    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("ORS_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "KukistiBot-Distance/1.0",
            "Accept": "application/json",
        })
        if self.api_key:
            self.session.headers["Authorization"] = self.api_key

    def execute(self, args=None, **kwargs) -> str:
        if not self.api_key:
            return "Error: ORS_API_KEY is not set in environment."

        city1, city2, error = self._parse_cities(args)
        if error:
            return error

        origin, error = self._geocode(city1)
        if error:
            return error

        destination, error = self._geocode(city2)
        if error:
            return error

        route, error = self._driving_distance(origin, destination)
        if error:
            return error

        return self._format_result(origin, destination, route)

    # ---- parsing ----------------------------------------------------

    def _parse_cities(self, args):
        """Splits '!distance' args into (city1, city2, error). Comma always
        works (and is required once a name has more than one word); a bare
        two-word input is accepted as a shortcut for two single-word names,
        since anything looser would risk silently misreading a multi-word
        city name (e.g. "New York Los Angeles") as the wrong split."""
        text = (args or "").strip()
        if not text:
            return None, None, self.USAGE

        if "," in text:
            city1, _, city2 = text.partition(",")
            city1, city2 = city1.strip(), city2.strip()
            if not city1 or not city2:
                return None, None, self.USAGE
            return city1, city2, None

        words = text.split()
        if len(words) == 2:
            return words[0], words[1], None

        return None, None, (
            "Error: multi-word city names need a comma, "
            "e.g. !distance New York, Los Angeles"
        )

    # ---- geocoding ----------------------------------------------------

    def _geocode(self, city):
        try:
            resp = self.session.get(
                self.GEOCODE_URL,
                params={"text": city, "size": 1},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            return None, "Error: Distance service (geocoding) request timed out."
        except requests.exceptions.ConnectionError:
            return None, "Error: Could not connect to the distance service (geocoding)."
        except requests.exceptions.HTTPError as e:
            return None, self._describe_http_error(e, "geocoding")
        except requests.exceptions.RequestException as e:
            return None, f"Error: Failed to contact distance service: {e}."
        except ValueError:
            return None, "Error: Invalid response from distance service (geocoding)."

        features = data.get("features") if isinstance(data, dict) else None
        if not features:
            return None, f"Error: Could not find a location matching '{city}'."

        feature = features[0] or {}
        coords = (feature.get("geometry") or {}).get("coordinates")
        if not coords or len(coords) < 2:
            return None, f"Error: Could not resolve coordinates for '{city}'."

        label = (feature.get("properties") or {}).get("label") or city
        return {"label": label, "lon": coords[0], "lat": coords[1]}, None

    # ---- routing --------------------------------------------------------

    def _driving_distance(self, origin, destination):
        try:
            resp = self.session.get(
                self.DIRECTIONS_URL,
                params={
                    "start": f"{origin['lon']},{origin['lat']}",
                    "end": f"{destination['lon']},{destination['lat']}",
                },
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            return None, "Error: Distance service (routing) request timed out."
        except requests.exceptions.ConnectionError:
            return None, "Error: Could not connect to the distance service (routing)."
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                print(f"Distance API routing 400: {e.response.text[:500]}")
                return None, (
                    f"Error: No driving route found between "
                    f"{origin['label']} and {destination['label']}."
                )
            return None, self._describe_http_error(e, "routing")
        except requests.exceptions.RequestException as e:
            return None, f"Error: Failed to contact distance service: {e}."
        except ValueError:
            return None, "Error: Invalid response from distance service (routing)."

        routes = data.get("routes") if isinstance(data, dict) else None
        if not routes:
            return None, (
                f"Error: No driving route found between "
                f"{origin['label']} and {destination['label']}."
            )

        summary = (routes[0] or {}).get("summary") or {}
        distance_m = summary.get("distance")
        if distance_m is None:
            return None, "Error: Distance service returned unexpected data."

        return {"distance_m": distance_m, "duration_s": summary.get("duration")}, None

    def _describe_http_error(self, error, stage) -> str:
        status = error.response.status_code if error.response is not None else None
        # ORS's response body usually explains *why* (bad key format, quota
        # exceeded, profile not enabled, etc.) far better than a bare status
        # code - print it server-side so it shows up in the bot's console
        # log without needing to reproduce the request by hand.
        if error.response is not None:
            body = error.response.text
            print(f"Distance API {stage} error {status}: {body[:500]}")

        if status in (401, 403):
            return "Error: Distance service rejected the API key."
        if status == 429:
            return "Error: Distance service rate limit exceeded, try again later."
        reason = error.response.reason if error.response is not None else "Unknown error"
        return f"Error: Distance service ({stage}) returned HTTP {status or 'unknown'} {reason}."

    # ---- formatting -------------------------------------------------

    def _format_result(self, origin, destination, route) -> str:
        km = route["distance_m"] / 1000
        mi = km * 0.621371
        duration = self._format_duration(route.get("duration_s"))
        duration_str = f", ~{duration} drive" if duration else ""

        return (
            f"Driving distance from {origin['label']} to {destination['label']}: "
            f"{km:.1f} km ({mi:.1f} mi){duration_str}."
        )

    def _format_duration(self, seconds):
        if seconds is None:
            return None
        total_minutes = round(seconds / 60)
        hours, minutes = divmod(total_minutes, 60)
        if hours and minutes:
            return f"{hours}h {minutes}min"
        if hours:
            return f"{hours}h"
        return f"{minutes}min"
