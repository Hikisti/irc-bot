import os
import re
from dotenv import load_dotenv

from base_command import BaseCommand
from http_session import DEFAULT_TIMEOUT_SECONDS, make_session
from request_errors import format_request_error


class ImdbCommand(BaseCommand):
    """Looks up a movie/show's IMDb rating, plot, and URL via the OMDb
    API - a third-party service re-publishing IMDb's data, not IMDb
    itself (IMDb's own official API is an enterprise-only AWS Data
    Exchange product, not viable for a free personal bot).

    Usage:
      !imdb <title>                    -> best match (OMDb prefers movies)
      !imdb series|tv|movie <title>    -> restrict to that type
      !imdb <title> <year>             -> match a release/start year
      !imdb tt0111964 / an imdb.com URL -> exact lookup by IMDb ID
    """

    ALIASES = ("!imdb",)
    BASE_URL = "https://www.omdbapi.com/"

    TYPE_KEYWORDS = {"series": "series", "tv": "series", "movie": "movie"}
    IMDB_ID_RE = re.compile(r"(tt\d{7,})", re.I)
    IMDB_URL_RE = re.compile(r"imdb\.com/(?:[a-z]{2}/)?title/(tt\d{7,})", re.I)
    YEAR_RE = re.compile(r"\(?((?:18|19|20)\d{2})\)?")

    def __init__(self):
        load_dotenv()  # Load environment variables from .env
        self.api_key = os.getenv("OMDB_API_KEY")
        self.session = make_session("KukistiBot-Imdb/1.0")

    def _extract_imdb_id(self, text):
        match = self.IMDB_URL_RE.search(text) or self.IMDB_ID_RE.fullmatch(text)
        return match.group(1).lower() if match else None

    def _build_attempts(self, text):
        """The ordered OMDb query params to try for `text`. A leading
        type keyword and/or trailing year become filters (tried first),
        with the whole string as a plain title lookup kept as a fallback
        - so titles that legitimately end in a year ("Blade Runner
        2049", "Wonder Woman 1984") still resolve. The year filter is
        strict, which is why hinted-first is the safe order; a title
        that itself starts with the keyword and whose stripped form also
        exists ("Movie 43" vs "43") is the one known miss - pasting the
        IMDb ID or URL is the exact workaround."""
        imdb_id = self._extract_imdb_id(text)
        if imdb_id:
            return [{"i": imdb_id}]

        tokens = text.split()
        hints = {}
        if len(tokens) >= 2 and tokens[0].lower() in self.TYPE_KEYWORDS:
            hints["type"] = self.TYPE_KEYWORDS[tokens[0].lower()]
            tokens = tokens[1:]
        year_match = self.YEAR_RE.fullmatch(tokens[-1]) if len(tokens) >= 2 else None
        if year_match:
            hints["y"] = year_match.group(1)
            tokens = tokens[:-1]

        plain = {"t": text}
        if not hints:
            return [plain]
        return [{"t": " ".join(tokens), **hints}, plain]

    def execute(self, args):
        text = (args or "").strip()
        if not text:
            return "Usage: !imdb [series|movie] <title> [year], or !imdb <IMDb ID or URL>"

        if not self.api_key:
            return "Error: OMDB_API_KEY is not set in environment."

        try:
            data = None
            for params in self._build_attempts(text):
                response = self.session.get(
                    self.BASE_URL,
                    params={**params, "apikey": self.api_key, "plot": "short"},
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                data = response.json()
                # Only a "... not found!" miss ("Movie not found!"/"Series
                # not found!") is worth retrying with the next attempt;
                # anything else would just fail the same way again.
                if data.get("Response") != "False" or "not found" not in str(data.get("Error", "")).lower():
                    break

            # OMDb reports "not found" as a normal HTTP 200 with
            # Response: "False", not an HTTP error - a bad/invalid API
            # key does come back as a real 401, which raise_for_status()
            # above already turns into a friendly error via the except
            # branch below.
            if data.get("Response") == "False":
                return f"Error: {data.get('Error', 'Movie not found.')}"

            title = data.get("Title", text)
            year = data.get("Year", "N/A")
            kind = data.get("Type")
            when = f"{year}, {kind}" if kind else year
            rating = data.get("imdbRating", "N/A")
            rating_str = f"{rating}/10" if rating != "N/A" else "not yet rated"
            plot = data.get("Plot", "N/A")
            imdb_id = data.get("imdbID")
            url = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "N/A"

            return f"{title} ({when}) - IMDb: {rating_str} - {plot} - {url}"

        except Exception as e:
            return format_request_error(e, "OMDb")
