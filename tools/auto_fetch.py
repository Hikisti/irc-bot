"""Fetches the game nights not fetched yet, run on the laptop each morning by launchd (tools/README.md).
It works out each night's window from the public schedules and calls fetch_night.sh once for all of
them; nothing here talks to the server except through that script, and nothing talks to IRC.

A night D is the Liiga games of Helsinki date D and the NHL games of US-Eastern date D. Its window
runs from LEAD before the first start to AFTER past the last game's end, ending before the 06:00 UTC
update window when the games allow. The nights are those after the newest fetched journal in
tools/logs/ up to yesterday (at most MAX_NIGHTS), so a missed morning or a failed fetch is caught up
by the next run; nights without games, or not over yet, are skipped.

    .venv/bin/python tools/auto_fetch.py [--dry-run]

--dry-run prints the plan and the fetch command without running it.
"""
import datetime
import os
import re
import subprocess
import sys
from dataclasses import dataclass

import probe_common  # noqa: F401  (puts src/ on the path)
from liiga_command import LiigaCommand  # noqa: E402
from nhl_command import NHLCommand  # noqa: E402
from start_extras import LIIGA_MIN_GAMES, NHL_MIN_GAMES  # noqa: E402

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(TOOLS_DIR, "logs")
UTC = datetime.timezone.utc

LEAD = datetime.timedelta(hours=1)
AFTER = datetime.timedelta(hours=2)
GAME_LENGTH = datetime.timedelta(hours=3)  # a typical game, start to end, either league
MIN_AFTER = datetime.timedelta(minutes=30)  # past the last end, even when that crosses the cutoff
CUTOFF_HOUR = 6  # UTC: an automatic update can restart the bot 06:00-07:00
MAX_NIGHTS = 7
JOURNAL_NAME = re.compile(r"^journal_(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class Night:
    name: str
    start: datetime.datetime
    end: datetime.datetime
    probe: bool
    sampler: bool


def night_window(night, liiga_starts, nhl_starts):
    """The Night for date `night` (a date) from its scheduled starts (aware datetimes), or None
    when neither league played."""
    starts = list(liiga_starts) + list(nhl_starts)
    if not starts:
        return None
    last_end = max(starts) + GAME_LENGTH
    end = last_end + AFTER
    cutoff = datetime.datetime.combine(night + datetime.timedelta(days=1), datetime.time(CUTOFF_HOUR), UTC)
    if end > cutoff:
        end = max(cutoff, last_end + MIN_AFTER)
    suffix = "" if liiga_starts and nhl_starts else ("_liiga" if liiga_starts else "_nhl")
    return Night(f"{night:%Y-%m-%d}{suffix}", (min(starts) - LEAD).astimezone(UTC), end.astimezone(UTC),
                 len(nhl_starts) >= NHL_MIN_GAMES, len(liiga_starts) >= LIIGA_MIN_GAMES)


def nights_to_check(today, last_fetched):
    """The dates after `last_fetched` (a date or None) up to yesterday, the last MAX_NIGHTS at most."""
    first = today - datetime.timedelta(days=MAX_NIGHTS)
    if last_fetched is not None:
        first = max(first, last_fetched + datetime.timedelta(days=1))
    return [first + datetime.timedelta(days=n) for n in range((today - first).days)]


def last_fetched(names):
    """The newest night among journal file names (journal_YYYY-MM-DD...), or None. A name whose date
    is not a real one (journal_2026-13-01) is ignored, not fatal: the job runs with nobody watching."""
    dates = []
    for match in filter(None, map(JOURNAL_NAME.match, names)):
        try:
            dates.append(datetime.date.fromisoformat(match.group(1)))
        except ValueError:
            continue
    return max(dates, default=None)


def fetch_args(nights):
    """fetch_night.sh's arguments for these nights: NAME FROM TO per night, then the flags."""
    args = []
    for night in nights:
        args += [night.name, f"{night.start:%Y-%m-%d %H:%M}", f"{night.end:%Y-%m-%d %H:%M}"]
    if any(night.probe for night in nights):
        args.append("--probe")
    if any(night.sampler for night in nights):
        args.append("--sampler")
    return args


def starts_on(night, liiga, nhl):
    """(Liiga starts, NHL starts) for one date, or None if a schedule could not be read."""
    day = liiga._fetch_day(f"{night:%Y-%m-%d}")
    scores = nhl._fetch_scores(f"{night:%Y-%m-%d}")
    if day is None or scores is None:
        return None
    parse = lambda command, games: [dt for dt in (command._parse_start_dt(g.get(command.START_TIME_KEY))  # noqa: E731
                                                  for g in games.values()) if dt is not None]
    return parse(liiga, day[0]), parse(nhl, scores)


def main(argv, now=None, run=subprocess.run):
    dry_run = "--dry-run" in argv
    now = now or datetime.datetime.now(UTC)
    print(f"auto fetch {now:%Y-%m-%d %H:%M} UTC")
    liiga, nhl = LiigaCommand(), NHLCommand()
    nights = []
    for night in nights_to_check(now.date(), last_fetched(os.listdir(LOG_DIR) if os.path.isdir(LOG_DIR) else [])):
        try:
            starts = starts_on(night, liiga, nhl)
            window = night_window(night, *starts) if starts is not None else None
        except Exception as e:  # an odd feed value must not end the unattended run with a traceback
            print(f"  {night}: could not work out the night ({type(e).__name__}: {e}), left for the next run")
            break
        if starts is None:
            print(f"  {night}: a schedule could not be read, left for the next run")
            break  # a later night must not be fetched past a gap, or this one is never caught up
        if window is None:
            print(f"  {night}: no games")
        elif window.end > now:
            print(f"  {window.name}: not over yet (window ends {window.end:%H:%M} UTC), left for the next run")
            break
        else:
            print(f"  {window.name}: {window.start:%H:%M} - {window.end:%Y-%m-%d %H:%M} UTC"
                  f"{' probe' if window.probe else ''}{' sampler' if window.sampler else ''}")
            nights.append(window)
    if not nights:
        print("  nothing to fetch")
        return 0
    command = [os.path.join(TOOLS_DIR, "fetch_night.sh")] + fetch_args(nights)
    if dry_run:
        print("  would run: " + " ".join(f'"{a}"' if " " in a else a for a in command))
        return 0
    return run(command).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
