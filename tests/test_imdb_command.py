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


def found(title="Flipper", year="1995–2000", kind="series", imdb_id="tt0111964"):
    return {
        "Response": "True", "Title": title, "Year": year, "Type": kind,
        "imdbRating": "5.3", "Plot": "A dolphin.", "imdbID": imdb_id,
    }


def not_found(error="Movie not found!"):
    return {"Response": "False", "Error": error}


class TestReplyFormat:
    def test_type_is_shown_next_to_the_year(self, imdb_command):
        data = found(title="Flipper", year="1996", kind="movie", imdb_id="tt0116322")
        with patch.object(imdb_command.session, "get", return_value=make_response(data)):
            result = imdb_command.execute("flipper")
        assert result.startswith("Flipper (1996, movie) - IMDb: 5.3/10")

    def test_series_shows_its_year_range_and_type(self, imdb_command):
        with patch.object(imdb_command.session, "get", return_value=make_response(found())):
            result = imdb_command.execute("flipper 1995")
        assert result.startswith("Flipper (1995–2000, series) - IMDb: 5.3/10")
        assert result.endswith("https://www.imdb.com/title/tt0111964/")

    def test_missing_type_is_omitted_rather_than_printed_as_none(self, imdb_command):
        data = found()
        del data["Type"]
        with patch.object(imdb_command.session, "get", return_value=make_response(data)):
            result = imdb_command.execute("flipper")
        assert result.startswith("Flipper (1995–2000) - ")


class TestQueryParsing:
    def _first_call_params(self, imdb_command, args):
        with patch.object(imdb_command.session, "get", return_value=make_response(found())) as mock_get:
            imdb_command.execute(args)
        assert mock_get.call_count == 1  # found on the first attempt
        params = dict(mock_get.call_args_list[0].kwargs["params"])
        params.pop("apikey")
        params.pop("plot")
        return params

    @pytest.mark.parametrize("args, expected", [
        ("flipper 1995", {"t": "flipper", "y": "1995"}),
        ("flipper (1995)", {"t": "flipper", "y": "1995"}),
        ("series flipper", {"t": "flipper", "type": "series"}),
        ("tv flipper", {"t": "flipper", "type": "series"}),
        ("movie flipper", {"t": "flipper", "type": "movie"}),
        ("SERIES Flipper", {"t": "Flipper", "type": "series"}),
        ("series flipper 1995", {"t": "flipper", "type": "series", "y": "1995"}),
        ("  flipper   1995  ", {"t": "flipper", "y": "1995"}),
        # Nothing to extract: sent through untouched, as before.
        ("terminator 2", {"t": "terminator 2"}),
        ("series", {"t": "series"}),
        ("movie", {"t": "movie"}),
        ("1917", {"t": "1917"}),
        ("flipper 1799", {"t": "flipper 1799"}),
        ("flipper 19955", {"t": "flipper 19955"}),
    ])
    def test_hints_are_extracted(self, imdb_command, args, expected):
        assert self._first_call_params(imdb_command, args) == expected

    @pytest.mark.parametrize("args", [
        "tt0111964",
        "TT0111964",
        "https://www.imdb.com/title/tt0111964/",
        "https://m.imdb.com/title/tt0111964/?ref_=fn_al_tt_1",
        "https://www.imdb.com/fi/title/tt0111964/",
        "see https://www.imdb.com/title/tt0111964/ for it",
    ])
    def test_imdb_id_or_url_does_an_exact_lookup(self, imdb_command, args):
        assert self._first_call_params(imdb_command, args) == {"i": "tt0111964"}

    def test_too_short_id_lookalike_is_treated_as_a_title(self, imdb_command):
        assert self._first_call_params(imdb_command, "tt123") == {"t": "tt123"}


class TestFallbackToPlainTitle:
    def test_title_ending_in_a_year_falls_back_to_the_whole_string(self, imdb_command):
        # "Blade Runner 2049": the hinted lookup (title "blade runner",
        # year 2049) finds nothing, so the literal string is tried next.
        responses = [
            make_response(not_found()),
            make_response(found(title="Blade Runner 2049", year="2017", kind="movie", imdb_id="tt1856101")),
        ]
        with patch.object(imdb_command.session, "get", side_effect=responses) as mock_get:
            result = imdb_command.execute("blade runner 2049")

        assert mock_get.call_count == 2
        assert mock_get.call_args_list[0].kwargs["params"]["t"] == "blade runner"
        assert mock_get.call_args_list[0].kwargs["params"]["y"] == "2049"
        assert mock_get.call_args_list[1].kwargs["params"]["t"] == "blade runner 2049"
        assert "Blade Runner 2049 (2017, movie)" in result

    def test_series_not_found_also_triggers_the_fallback(self, imdb_command):
        responses = [make_response(not_found("Series not found!")), make_response(found())]
        with patch.object(imdb_command.session, "get", side_effect=responses) as mock_get:
            imdb_command.execute("series flipper")
        assert mock_get.call_count == 2

    def test_both_attempts_missing_reports_not_found(self, imdb_command):
        responses = [make_response(not_found("Series not found!")), make_response(not_found())]
        with patch.object(imdb_command.session, "get", side_effect=responses) as mock_get:
            result = imdb_command.execute("series zzzz")
        assert mock_get.call_count == 2
        assert result == "Error: Movie not found!"

    def test_other_errors_do_not_trigger_the_fallback(self, imdb_command):
        with patch.object(
            imdb_command.session, "get", return_value=make_response(not_found("Something else broke!"))
        ) as mock_get:
            result = imdb_command.execute("flipper 1995")
        assert mock_get.call_count == 1
        assert result == "Error: Something else broke!"

    def test_plain_title_with_no_hints_is_only_tried_once(self, imdb_command):
        with patch.object(imdb_command.session, "get", return_value=make_response(not_found())) as mock_get:
            result = imdb_command.execute("asdkjaslkdjaslkdj")
        assert mock_get.call_count == 1
        assert result == "Error: Movie not found!"

    def test_nonexistent_imdb_id_reports_the_error_without_retrying(self, imdb_command):
        with patch.object(
            imdb_command.session, "get", return_value=make_response(not_found("Error getting data."))
        ) as mock_get:
            result = imdb_command.execute("tt9999999")
        assert mock_get.call_count == 1
        assert result == "Error: Error getting data."


class TestUsage:
    def test_usage_mentions_the_new_syntax(self, imdb_command):
        result = imdb_command.execute("   ")
        assert result.startswith("Usage:")
        assert "series|movie" in result
        assert "IMDb ID or URL" in result
