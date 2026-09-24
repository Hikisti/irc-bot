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


def format_request_error(e: Exception, service_name: str) -> str:
    """Turns any exception raised while calling `service_name`'s API into
    one consistent "Error: ..." message. Handles the requests exception
    hierarchy specifically (Timeout/ConnectionError/HTTPError/
    RequestException) and falls back to a generic message - including the
    exception text - for anything else, so a single `except Exception as
    e: return format_request_error(e, "...")` is enough for a whole call
    site."""
    if isinstance(e, requests.exceptions.Timeout):
        return f"Error: {service_name} request timed out."
    if isinstance(e, requests.exceptions.ConnectionError):
        return f"Error: Could not connect to {service_name}."
    if isinstance(e, requests.exceptions.HTTPError):
        response = e.response
        status = response.status_code if response is not None else "unknown"
        reason = response.reason if response is not None else "Unknown"
        return f"Error: {service_name} returned HTTP {status} {reason}."
    if isinstance(e, requests.exceptions.RequestException):
        return f"Error: Failed to contact {service_name}."
    return f"Error: Unexpected issue with {service_name}: {e}"
