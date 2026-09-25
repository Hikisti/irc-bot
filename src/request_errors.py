"""Shared error-message formatting for commands that call an HTTP API
directly via requests - turns whatever went wrong into one consistent,
user-facing IRC message shape, regardless of which service it was.

Previously each command (WeatherCommand, ElectricityCommand, F1Command,
TimeCommand, CryptoCommand, URLFetcher) duplicated its own 4-5 branch
except-chain for this, each with its own slightly different wording for
the same underlying failure (e.g. a connection error read "Unable to
connect", "Could not connect", or "Cannot connect" depending on which
file you were reading) - not just repeated code, but an inconsistent
user-facing experience with no reason to pick one wording over another.
"""

import requests

# yfinance 1.x (StockCommand's dependency) does its HTTP via curl_cffi by
# default, falling back to requests only if curl_cffi isn't installed -
# so StockCommand's own errors can be instances of either hierarchy.
# curl_cffi deliberately mirrors requests' own exception names and shape
# (each has a Timeout/ConnectionError/HTTPError/JSONDecodeError/
# RequestException, and HTTPError/JSONDecodeError carry a `.response` the
# same way), so both are checked here rather than duplicating this
# module's wording a second time just for StockCommand.
import curl_cffi.requests.exceptions as curl_exceptions

_TIMEOUT_TYPES = (requests.exceptions.Timeout, curl_exceptions.Timeout)
_CONNECTION_ERROR_TYPES = (requests.exceptions.ConnectionError, curl_exceptions.ConnectionError)
_HTTP_ERROR_TYPES = (requests.exceptions.HTTPError, curl_exceptions.HTTPError)
_JSON_DECODE_ERROR_TYPES = (requests.exceptions.JSONDecodeError, curl_exceptions.JSONDecodeError)
_REQUEST_EXCEPTION_TYPES = (requests.exceptions.RequestException, curl_exceptions.RequestException)


def format_request_error(e: Exception, service_name: str) -> str:
    """Turns any exception raised while calling `service_name`'s API into
    one consistent "Error: ..." message. Handles the requests (and
    curl_cffi - see above) exception hierarchy specifically (Timeout/
    ConnectionError/HTTPError/JSONDecodeError/RequestException) and falls
    back to a generic message - including the exception text - for
    anything else, so a single `except Exception as e: return
    format_request_error(e, "...")` is enough for a whole call site,
    including one that calls response.json() without a separate
    try/except of its own."""
    if isinstance(e, _TIMEOUT_TYPES):
        return f"Error: {service_name} request timed out."
    if isinstance(e, _CONNECTION_ERROR_TYPES):
        return f"Error: Could not connect to {service_name}."
    if isinstance(e, _HTTP_ERROR_TYPES):
        response = e.response
        status = response.status_code if response is not None else "unknown"
        reason = response.reason if response is not None else "Unknown"
        return f"Error: {service_name} returned HTTP {status} {reason}."
    if isinstance(e, _JSON_DECODE_ERROR_TYPES):
        # Checked before the plain RequestException below: JSONDecodeError
        # is *also* a RequestException (it inherits from both that and
        # the stdlib json.JSONDecodeError/ValueError), so without this a
        # non-JSON response would be misreported as "failed to contact"
        # the service even though it did respond.
        return f"Error: Invalid response from {service_name}."
    if isinstance(e, _REQUEST_EXCEPTION_TYPES):
        return f"Error: Failed to contact {service_name}."
    return f"Error: Unexpected issue with {service_name}: {e}"
