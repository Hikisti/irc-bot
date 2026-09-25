"""Shared requests.Session() construction - every command that talks to
its own HTTP API built one the same way independently (a fixed
User-Agent, usually declaring it only accepts JSON)."""

import requests

# Shared default for commands that don't need a longer budget of their
# own (see live_tracker_command.py/distance_command.py's own
# REQUEST_TIMEOUT_SECONDS = 10 for APIs that warrant more slack) - was
# previously a "timeout=5" literal repeated independently in several
# command modules.
DEFAULT_TIMEOUT_SECONDS = 5


def make_session(user_agent: str, accept_json: bool = True) -> requests.Session:
    """A requests.Session with `user_agent` set, and (by default)
    Accept: application/json - pass accept_json=False to leave requests'
    own permissive default ("*/*") in place instead, for an API that
    doesn't want an explicit Accept header (e.g. DistanceCommand's
    routing endpoint 406s on a bare "application/json" one) or a
    non-JSON target (e.g. URLFetcher, scraping arbitrary web pages)."""
    session = requests.Session()
    headers = {"User-Agent": user_agent}
    if accept_json:
        headers["Accept"] = "application/json"
    session.headers.update(headers)
    return session
