import json
import os
import sys
from unittest.mock import MagicMock

import pytest
import requests

# command_handler.py imports its sibling modules with bare names
# (e.g. "from electricity import ElectricityCommand") rather than
# "from src.electricity import ...", which only resolves when the src/
# directory itself is on sys.path (as it is when the bot is run from
# within src/). Add it here so tests can import command_handler too -
# and so every test file's own imports (bare, matching this style) load
# the exact same module object command_handler.py itself uses, rather
# than "src.X" and "X" ending up as two independent module instances
# with two independent copies of the same class.
SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# python-dotenv turns load_dotenv() into a no-op when this is set. Several
# commands call it in __init__, so without this a developer's local .env
# (gitignored, never present in CI) silently supplies API keys and can make
# a test pass locally that fails in CI - which is exactly how a CI-only
# failure slipped through once. A test that needs a key sets it itself.
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

from nhl_command import NHLCommand  # noqa: E402 - needs src/ on the path, set above
from tests.nhl_test_helpers import html_response  # noqa: E402


def make_json_response(json_data, status_code=200, reason="Not Found"):
    """Shared mock for a requests.Response carrying a JSON body - used
    across every command's tests that talk to a JSON API (previously
    redefined nearly identically in six separate test files). `text` is
    set to the JSON-serialized body, matching what a real Response.text
    would hold, since at least one caller (DistanceCommand) reads it
    directly on an error path.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = reason
    resp.json.return_value = json_data
    resp.text = json.dumps(json_data)
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


def join_channel_thread(command, channel, timeout=2):
    """Waits for the background thread LiveTrackerCommand._start() spawned
    for `channel` to finish - used by LiigaCommand/PesisCommand tests that
    exercise a real !command start end-to-end rather than calling _run()
    directly (previously redefined identically in both test files)."""
    entry = command._channels.get(channel)
    if entry and entry.get("thread"):
        entry["thread"].join(timeout=timeout)


@pytest.fixture
def nhl_command():
    command = NHLCommand()
    # A FINAL: announcement fetches nhl.com's game report for attendance, so
    # by default every GET here answers 404 (no attendance) - a test that
    # cares patches session.get itself. Nothing in the NHL test files may
    # reach the real network.
    command.session.get = MagicMock(return_value=html_response("", status_code=404))
    return command
