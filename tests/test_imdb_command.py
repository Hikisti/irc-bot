from unittest.mock import patch

import pytest
import requests

from imdb_command import ImdbCommand
from tests.conftest import make_json_response as make_response


@pytest.fixture
def imdb_command(monkeypatch):
    monkeypatch.setenv("OMDB_API_KEY", "test-key")
    return ImdbCommand()


class TestImdbCommand:
    def test_missing_api_key_returns_error_without_crashing(self, monkeypatch):
        monkeypatch.delenv("OMDB_API_KEY", raising=False)
        with patch("imdb_command.load_dotenv"):
            command = ImdbCommand()
        assert "OMDB_API_KEY is not set" in command.execute("terminator 2")

    def test_no_args_returns_usage_error(self, imdb_command):
        assert "Usage:" in imdb_command.execute("")

    def test_happy_path_includes_rating_plot_and_url(self, imdb_command):
        data = {
            "Response": "True",
            "Title": "Terminator 2: Judgment Day",
            "Year": "1991",
            "imdbRating": "8.6",
            "Plot": "A cyborg from the future must protect John Connor.",
            "imdbID": "tt0103064",
        }
        with patch.object(imdb_command.session, "get", return_value=make_response(data)) as mock_get:
            result = imdb_command.execute("terminator 2")

        assert result == (
            "Terminator 2: Judgment Day (1991) - IMDb: 8.6/10 - "
            "A cyborg from the future must protect John Connor. - "
            "https://www.imdb.com/title/tt0103064/"
        )
        params = mock_get.call_args.kwargs["params"]
        assert params["t"] == "terminator 2"

    def test_movie_not_found_returns_omdbs_own_error_message(self, imdb_command):
        # OMDb reports "not found" as a normal HTTP 200 with
        # Response: "False", not an HTTP error status.
        data = {"Response": "False", "Error": "Movie not found!"}
        with patch.object(imdb_command.session, "get", return_value=make_response(data)):
            result = imdb_command.execute("asdkjaslkdjaslkdj")

        assert result == "Error: Movie not found!"

    def test_unrated_title_shows_not_yet_rated_instead_of_na_out_of_10(self, imdb_command):
        # OMDb uses the literal string "N/A" for a missing imdbRating
        # (e.g. an unreleased title) - showing that verbatim as "N/A/10"
        # would read as a broken score rather than "no score yet".
        data = {
            "Response": "True",
            "Title": "Some Upcoming Movie",
            "Year": "2027",
            "imdbRating": "N/A",
            "Plot": "N/A",
            "imdbID": "tt9999999",
        }
        with patch.object(imdb_command.session, "get", return_value=make_response(data)):
            result = imdb_command.execute("some upcoming movie")

        assert "IMDb: not yet rated" in result

    def test_invalid_api_key_returns_friendly_error(self, imdb_command):
        error_response = make_response({"Response": "False", "Error": "Invalid API key!"}, status_code=401)
        with patch.object(imdb_command.session, "get", return_value=error_response):
            result = imdb_command.execute("terminator 2")
        assert "401" in result

    def test_invalid_json_returns_friendly_error_not_a_contact_failure(self, imdb_command):
        resp = make_response({})
        resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "not json", 0)
        with patch.object(imdb_command.session, "get", return_value=resp):
            result = imdb_command.execute("terminator 2")
        assert result == "Error: Invalid response from OMDb."

    def test_timeout_returns_friendly_error(self, imdb_command):
        with patch.object(imdb_command.session, "get", side_effect=requests.exceptions.Timeout):
            result = imdb_command.execute("terminator 2")
        assert "timed out" in result

    def test_connection_error_returns_friendly_error(self, imdb_command):
        with patch.object(imdb_command.session, "get", side_effect=requests.exceptions.ConnectionError):
            result = imdb_command.execute("terminator 2")
        assert "Could not connect" in result
