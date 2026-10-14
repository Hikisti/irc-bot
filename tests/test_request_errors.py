from unittest.mock import MagicMock

import curl_cffi.requests.exceptions as curl_exceptions
import pytest
import requests

from request_errors import format_request_error


class TestFormatRequestError:
    def test_timeout(self):
        result = format_request_error(requests.exceptions.Timeout(), "Weather API")
        assert result == "Error: Weather API request timed out."

    def test_connection_error(self):
        result = format_request_error(requests.exceptions.ConnectionError(), "Weather API")
        assert result == "Error: Could not connect to Weather API."

    def test_http_error_includes_status_and_reason(self):
        response = MagicMock(status_code=404, reason="Not Found")
        error = requests.exceptions.HTTPError(response=response)
        result = format_request_error(error, "Weather API")
        assert result == "Error: Weather API returned HTTP 404 Not Found."

    @pytest.mark.parametrize("status, text", [
        (401, "Unauthorized"), (403, "Forbidden"), (429, "Too Many Requests"), (502, "Bad Gateway"),
    ])
    @pytest.mark.parametrize("empty", ["", None])
    def test_a_missing_status_text_is_filled_in_from_the_standard_one(self, status, text, empty):
        # The time service answered 401 with no text and the line read "returned HTTP 401 ."
        error = requests.exceptions.HTTPError(response=MagicMock(status_code=status, reason=empty))
        assert format_request_error(error, "time service") == f"Error: time service returned HTTP {status} {text}."

    def test_a_missing_status_text_for_a_code_that_has_none_leaves_no_stray_space(self):
        error = requests.exceptions.HTTPError(response=MagicMock(status_code=599, reason=""))
        assert format_request_error(error, "time service") == "Error: time service returned HTTP 599."

    def test_a_status_text_the_server_sent_is_kept(self):
        error = requests.exceptions.HTTPError(response=MagicMock(status_code=403, reason="Too many requests"))
        assert format_request_error(error, "the webpage") == "Error: the webpage returned HTTP 403 Too many requests."

    def test_http_error_with_no_response_object(self):
        # requests.exceptions.HTTPError can in principle be raised without
        # a response attached - must not crash trying to read .status_code
        # off None.
        error = requests.exceptions.HTTPError(response=None)
        result = format_request_error(error, "Weather API")
        assert result == "Error: Weather API returned HTTP unknown Unknown."

    def test_other_request_exception(self):
        result = format_request_error(requests.exceptions.RequestException(), "Weather API")
        assert result == "Error: Failed to contact Weather API."

    def test_json_decode_error_is_not_reported_as_a_generic_request_failure(self):
        # Regression test: requests.exceptions.JSONDecodeError inherits
        # from *both* ValueError and RequestException, so without a
        # dedicated branch checked first, a non-JSON response (the server
        # did respond) would be misreported the same as "failed to
        # contact" the service (it didn't respond at all).
        error = requests.exceptions.JSONDecodeError("Expecting value", "not json", 0)
        result = format_request_error(error, "Weather API")
        assert result == "Error: Invalid response from Weather API."

    def test_non_request_exception_includes_exception_text(self):
        result = format_request_error(ValueError("bad data"), "Weather API")
        assert result == "Error: Unexpected issue with Weather API: bad data"


class TestFormatRequestErrorCurlCffi:
    """StockCommand's own errors can be curl_cffi's, not requests' -
    yfinance 1.x uses curl_cffi for its HTTP by default. curl_cffi
    mirrors requests' exception names/shape closely enough that the same
    wording template applies."""

    def test_timeout(self):
        result = format_request_error(curl_exceptions.Timeout("slow"), "Yahoo Finance")
        assert result == "Error: Yahoo Finance request timed out."

    def test_connection_error(self):
        result = format_request_error(curl_exceptions.ConnectionError("refused"), "Yahoo Finance")
        assert result == "Error: Could not connect to Yahoo Finance."

    def test_http_error_includes_status_and_reason(self):
        response = MagicMock(status_code=500, reason="Internal Server Error")
        error = curl_exceptions.HTTPError("boom", response=response)
        result = format_request_error(error, "Yahoo Finance")
        assert result == "Error: Yahoo Finance returned HTTP 500 Internal Server Error."

    def test_json_decode_error(self):
        error = curl_exceptions.JSONDecodeError("Expecting value", "not json", 0)
        result = format_request_error(error, "Yahoo Finance")
        assert result == "Error: Invalid response from Yahoo Finance."

    def test_other_request_exception(self):
        result = format_request_error(curl_exceptions.RequestException("boom"), "Yahoo Finance")
        assert result == "Error: Failed to contact Yahoo Finance."
