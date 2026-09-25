import os
from dotenv import load_dotenv

from base_command import BaseCommand
from http_session import DEFAULT_TIMEOUT_SECONDS, make_session
from request_errors import format_request_error


class ImdbCommand(BaseCommand):
    """Looks up a movie/show's IMDb rating, plot, and URL via the OMDb
    API - a third-party service re-publishing IMDb's data, not IMDb
    itself (IMDb's own official API is an enterprise-only AWS Data
    Exchange product, not viable for a free personal bot)."""

    ALIASES = ("!imdb",)
    BASE_URL = "https://www.omdbapi.com/"

    def __init__(self):
        load_dotenv()  # Load environment variables from .env
        self.api_key = os.getenv("OMDB_API_KEY")
        self.session = make_session("KukistiBot-Imdb/1.0")

    def execute(self, args):
        if not args:
            return "Usage: !imdb <title>"

        if not self.api_key:
            return "Error: OMDB_API_KEY is not set in environment."

        try:
            response = self.session.get(
                self.BASE_URL,
                params={"t": args, "apikey": self.api_key, "plot": "short"},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()

            # OMDb reports "not found" as a normal HTTP 200 with
            # Response: "False", not an HTTP error - a bad/invalid API
            # key does come back as a real 401, which raise_for_status()
            # above already turns into a friendly error via the except
            # branch below.
            if data.get("Response") == "False":
                return f"Error: {data.get('Error', 'Movie not found.')}"

            title = data.get("Title", args)
            year = data.get("Year", "N/A")
            rating = data.get("imdbRating", "N/A")
            rating_str = f"{rating}/10" if rating != "N/A" else "not yet rated"
            plot = data.get("Plot", "N/A")
            imdb_id = data.get("imdbID")
            url = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "N/A"

            return f"{title} ({year}) - IMDb: {rating_str} - {plot} - {url}"

        except Exception as e:
            return format_request_error(e, "OMDb")
