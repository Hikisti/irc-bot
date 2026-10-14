import os
import re

from dotenv import load_dotenv
import requests

from base_command import BaseCommand
from http_session import make_session
from request_errors import format_request_error


class DistanceCommand(BaseCommand):
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

    ALIASES = ("!distance",)
    HELP = "!distance <city1>,<city2>"

    GEOCODE_URL = "https://api.heigit.org/pelias/v1/search"
    DIRECTIONS_URL = "https://api.heigit.org/openrouteservice/v2/directions/driving-car"
    REQUEST_TIMEOUT_SECONDS = 10
    SNAP_RADIUS_METERS = 5000  # how far from a city's centre point the nearest road may be

    USAGE = (
        "Usage: !distance <city1>,<city2> "
        "(or !distance <city1> <city2> for two single-word city names), "
        "e.g., !distance Kokkola,Vimpeli"
    )

    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("ORS_API_KEY")
        # Deliberately no explicit Accept header: ORS's directions endpoint
        # currently only serves GeoJSON for driving-car and returns 406 for
        # a bare "application/json" Accept, so we let requests' default
        # (Accept: */*) through and just parse whatever comes back.
        self.session = make_session("KukistiBot-Distance/1.0", accept_json=False)
        if self.api_key:
            self.session.headers["Authorization"] = self.api_key

    def execute(self, args=None, **kwargs) -> str:
        # Arguments are validated before the API key is checked - same
        # order as !weather/!time/!imdb - so a bare "!distance" always
        # answers with usage, whether or not the key happens to be set.
        city1, city2, error = self._parse_cities(args)
        if error:
            return error

        if not self.api_key:
            return "Error: ORS_API_KEY is not set in environment."

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
                # Ask for a few candidates, not just the top one - Pelias
                # ranks by text-match relevance, which can rank a wrongly
                # matched place above the correct city of that name (see
                # _best_match() for a real example this was built against).
                params={"text": city, "size": 5},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            return None, self._describe_http_error(e, "geocoding")
        except Exception as e:
            return None, format_request_error(e, "Distance service (geocoding)")

        try:
            data = resp.json()
        except ValueError:
            return None, "Error: Invalid response from distance service (geocoding)."

        features = data.get("features") if isinstance(data, dict) else None
        if not features:
            return None, f"Error: Could not find a location matching '{city}'."

        feature = self._best_match(features, city)
        coords = (feature.get("geometry") or {}).get("coordinates")
        if not coords or len(coords) < 2:
            return None, f"Error: Could not resolve coordinates for '{city}'."

        label = (feature.get("properties") or {}).get("label") or city
        return {"label": label, "lon": coords[0], "lat": coords[1]}, None

    # Pelias layer values, roughly biggest-to-smallest for the layers that
    # can plausibly be "the city someone means" by a bare name.
    LAYER_RANK = {"locality": 3, "localadmin": 2, "borough": 1}

    def _best_match(self, features, query):
        """Picks the best candidate among the geocoder's top matches.

        Confirmed live (via a real incident): "!distance austin miami"
        geocoded "miami" to a candidate whose own properties.name is
        "Barranquilla" (a Colombian city), at confidence 1.0 and
        match_type "exact" - identical scoring to the correct
        "Miami, FL, USA" candidate, presumably matched via some alternate
        name in Pelias's WhosOnFirst index that isn't reflected in `name`
        or `label`. Neither confidence, match_type, layer, nor population
        distinguish it from a correct match, so the only reliable signal
        left is: does this candidate's own name actually correspond to
        what was searched for? Filters to candidates whose name matches
        the query first (falling back to the full list if none do, e.g.
        for queries that don't literally appear in any candidate's name),
        then breaks ties by layer (city-level beats county/neighbourhood)
        and population, keeping Pelias's own relevance order beyond that.
        """
        def normalize(text):
            return (text or "").strip().casefold()

        query_norm = normalize(query)
        named_matches = [
            f for f in features
            if normalize((f.get("properties") or {}).get("name")) == query_norm
        ]
        candidates = named_matches or features

        def rank(feature):
            props = (feature or {}).get("properties") or {}
            layer_rank = self.LAYER_RANK.get(props.get("layer"), 0)
            population = props.get("population")
            population = population if isinstance(population, (int, float)) else -1
            return (layer_rank, population)

        return max(candidates, key=rank)

    # ---- routing --------------------------------------------------------

    @staticmethod
    def _unroutable_point(response, origin, destination):
        """The label of the point the router could not place on a road (its error 2010, "Could not find
        routable point within a radius of ... of specified coordinate 0: ..."), or None for any other
        answer. A geocoder that picked a faraway namesake (an unaccented Finnish name that matches a
        village in Russia) ends up here."""
        if response is None or response.status_code != 404:
            return None
        try:
            error = response.json().get("error") or {}
        except ValueError:
            return None
        if not isinstance(error, dict) or error.get("code") != 2010:
            return None
        found = re.search(r"specified coordinate (\d+)", str(error.get("message", "")))
        if not found or int(found.group(1)) not in (0, 1):
            return None
        return (origin, destination)[int(found.group(1))]["label"]

    def _driving_distance(self, origin, destination):
        try:
            # A city's geocoded point is its centre, which for some cities (Helsinki, San Antonio) is
            # not within the router's default 350 m of a road: "Could not find routable point" (issue
            # #44). The snapping radius can only be set in a POST body (a GET ignores it).
            resp = self.session.post(
                self.DIRECTIONS_URL,
                json={
                    "coordinates": [[origin["lon"], origin["lat"]], [destination["lon"], destination["lat"]]],
                    "radiuses": [self.SNAP_RADIUS_METERS, self.SNAP_RADIUS_METERS],
                },
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                print(f"Distance API routing 400: {e.response.text[:500]}")
                return None, (
                    f"Error: No driving route found between "
                    f"{origin['label']} and {destination['label']}."
                )
            unreachable = self._unroutable_point(e.response, origin, destination)
            if unreachable:
                print(f"Distance API routing 404: {e.response.text[:300]}")
                return None, f"Error: No road found near {unreachable}."
            return None, self._describe_http_error(e, "routing")
        except Exception as e:
            return None, format_request_error(e, "Distance service (routing)")

        try:
            data = resp.json()
        except ValueError:
            return None, "Error: Invalid response from distance service (routing)."

        summary = self._extract_route_summary(data)
        if summary is None:
            return None, (
                f"Error: No driving route found between "
                f"{origin['label']} and {destination['label']}."
            )

        distance_m = summary.get("distance")
        if distance_m is None:
            return None, "Error: Distance service returned unexpected data."

        return {"distance_m": distance_m, "duration_s": summary.get("duration")}, None

    def _extract_route_summary(self, data):
        """ORS's directions endpoint has returned two different response
        shapes for driving-car depending on content negotiation: the
        GeoJSON FeatureCollection shape (currently the default) and an
        older plain "routes" shape. Handle both."""
        if not isinstance(data, dict):
            return None

        features = data.get("features")
        if features:
            feature = features[0] or {}
            summary = (feature.get("properties") or {}).get("summary")
            if summary:
                return summary

        routes = data.get("routes")
        if routes:
            summary = (routes[0] or {}).get("summary")
            if summary:
                return summary

        return None

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
        return format_request_error(error, f"Distance service ({stage})")

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
