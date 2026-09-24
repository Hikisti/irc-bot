import json
import os
import sys
from unittest.mock import MagicMock

import requests

# command_handler.py imports its sibling modules with bare names
# (e.g. "from electricity import ElectricityCommand") rather than
# "from src.electricity import ...", which only resolves when the src/
# directory itself is on sys.path (as it is when the bot is run from
# within src/). Add it here so tests can import command_handler too.
SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


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
