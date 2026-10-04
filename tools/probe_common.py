"""Shared helpers for the read-only probes in this folder: the import path to
`src/`, a timestamped logger, and the time zone used for the second timestamp."""
import os
import sys
from datetime import datetime, timezone

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TOOLS_DIR, "..", "src"))

from live_tracker_command import LiveTrackerCommand  # noqa: E402  (needs src/ on the path)

LOG_DIR = os.path.join(TOOLS_DIR, "logs")
LOCAL_TZ = LiveTrackerCommand.HELSINKI_TZ  # the zone the bot itself shows times in


def make_logger(name):
    """log(message): prints and appends to tools/logs/<name>.log, each line stamped
    with UTC and local time."""
    path = os.path.join(LOG_DIR, f"{name}.log")

    def log(message):
        os.makedirs(LOG_DIR, exist_ok=True)
        now = datetime.now(timezone.utc)
        line = f"{now:%Y-%m-%d %H:%M:%S}Z ({now.astimezone(LOCAL_TZ):%H:%M:%S} local) {message}"
        print(line, flush=True)
        with open(path, "a") as f:
            f.write(line + "\n")

    return log
