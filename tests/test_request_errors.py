from unittest.mock import MagicMock

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

    def test_non_request_exception_includes_exception_text(self):
        result = format_request_error(ValueError("bad data"), "Weather API")
        assert result == "Error: Unexpected issue with Weather API: bad data"
