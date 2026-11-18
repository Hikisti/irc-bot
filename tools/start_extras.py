"""Starts the day's feed probes on the server by itself, run once a day by a timer (DEPLOY.md): the
Liiga sampler on a day of LIIGA_MIN_GAMES or more Liiga games, the NHL goal probe on a slate of
NHL_MIN_GAMES or more, each from LEAD_MINUTES before its league's first game, with its date and
hours worked out. A probe already running (started by hand) is left alone. It waits for the probes,
so the timer's service lasts as long as they do. Like the probes it only reads the public feeds:
nothing here talks to IRC.

    .venv/bin/python tools/start_extras.py [--dry-run]

--dry-run prints today's plan and starts nothing.
"""
import datetime
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import probe_common  # noqa: F401  (puts src/ on the path)
from liiga_command import LiigaCommand  # noqa: E402
from nhl_command import NHLCommand  # noqa: E402

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
UTC = datetime.timezone.utc

# The probes are worth their extra requests on big days only (the same rule as by hand).
LIIGA_MIN_GAMES = 5
NHL_MIN_GAMES = 10
LEAD_MINUTES = 30
# How long after the last start a game is surely over; each probe also stops by itself once every
# game has ended, so these only cap a run that never sees the end.
LIIGA_GAME_HOURS = 3.5
NHL_GAME_HOURS = 4
NHL_POLL_SECONDS = 20  # the server probe's interval: its requests add to the bot's, from one address


@dataclass(frozen=True)
class Run:
    script: str
    args: tuple
    start_at: datetime.datetime


def plan(now, liiga_starts, nhl_starts):
    """The probe runs for today, earliest first: [Run]. `*_starts` are the day's scheduled starts
    (aware datetimes), or None when that schedule could not be read."""
    runs = []
    for starts, minimum, game_hours, script, local_tz, extra in (
        (liiga_starts, LIIGA_MIN_GAMES, LIIGA_GAME_HOURS, "liiga_sampler.py", LiigaCommand.HELSINKI_TZ, ()),
        (nhl_starts, NHL_MIN_GAMES, NHL_GAME_HOURS, "goal_probe.py", NHLCommand.EASTERN_TZ,
         (str(NHL_POLL_SECONDS),)),
    ):
        if not starts or len(starts) < minimum:
            continue
        first, last = min(starts), max(starts)
        start_at = max(now, first - datetime.timedelta(minutes=LEAD_MINUTES)).astimezone(UTC)
        end = last + datetime.timedelta(hours=game_hours)
        if end <= start_at:
            continue
        hours = math.ceil((end - start_at).total_seconds() / 3600)
        # The slate's own date: Helsinki for Liiga, US Eastern for the NHL (its schedule is bucketed so).
        date = first.astimezone(local_tz).strftime("%Y-%m-%d")
        runs.append(Run(script, (date, str(hours)) + extra, start_at))
    return sorted(runs, key=lambda run: run.start_at)


def todays_starts(command):
    """The scheduled starts of the command's games today, or None if the schedule could not be read."""
    items = command._fetch_today_games()
    if items is None:
        return None
    starts = (command._parse_start_dt(item.get(command.START_TIME_KEY)) for item in items.values())
    return [dt for dt in starts if dt is not None]


def already_running(script):
    return subprocess.run(["pgrep", "-f", f"tools/{script}"], capture_output=True).returncode == 0


def main(clock=lambda: datetime.datetime.now(UTC), sleep=time.sleep, launch=subprocess.Popen, dry_run=False):
    now = clock()
    runs = plan(now, todays_starts(LiigaCommand()), todays_starts(NHLCommand()))
    if not runs:
        print(f"extras: nothing to start today ({now:%Y-%m-%d})")
        return []
    for run in runs:
        print(f"extras: {run.script} {' '.join(run.args)} from {run.start_at:%H:%M} UTC")
    if dry_run:
        return []
    started = []
    for run in runs:
        wait = (run.start_at - clock()).total_seconds()
        if wait > 0:
            sleep(wait)
        if already_running(run.script):
            print(f"extras: {run.script} is already running, not starting another")
            continue
        print(f"extras: starting {run.script} {' '.join(run.args)}")
        started.append(launch([sys.executable, os.path.join(TOOLS_DIR, run.script), *run.args],
                              cwd=os.path.dirname(TOOLS_DIR), stdout=subprocess.DEVNULL))
    for process in started:
        process.wait()
    return started


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv[1:])
