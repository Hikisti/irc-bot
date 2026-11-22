"""The checks before a push to main, which deploys and may restart the live bot (CLAUDE.md,
"Deployment"). Run by the /pre-push skill; it pushes nothing and changes nothing.

    .venv/bin/python tools/pre_push.py

It says what would go out, whether the deploy would restart the bot (asking the repository's own
.github/needs-restart.sh, against the commit the last deploy's log says the bot runs), which games
are on or about to start, whether a "move only" commit moved its lines unchanged, and whether an
added line carries a server detail (the values in the gitignored tools/fetch_night.conf, or an IP
address) or an issue number in CLAUDE.md. Exit 0 = nothing to look at, 1 = something to look at.
"""
import collections
import datetime
import json
import os
import re
import subprocess
import sys

import probe_common  # noqa: F401  (puts src/ on the path)
from liiga_command import LiigaCommand  # noqa: E402
from nhl_command import NHLCommand  # noqa: E402
from pesis_command import SuperpesisCommand, YkkospesisCommand  # noqa: E402
from start_extras import LIIGA_GAME_HOURS, NHL_GAME_HOURS  # noqa: E402

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF_PATH = os.path.join(REPO_DIR, "tools", "fetch_night.conf")
UTC = datetime.timezone.utc

PESIS_GAME_HOURS = 3
# A tracker started by hand before its game is already running, so a restart this close to a start
# still stops something.
SOON_HOURS = 1
TRACKERS = (
    (LiigaCommand, LIIGA_GAME_HOURS),
    (NHLCommand, NHL_GAME_HOURS),
    (SuperpesisCommand, PESIS_GAME_HOURS),
    (YkkospesisCommand, PESIS_GAME_HOURS),
)

DECISION_RE = re.compile(r"\dZ (no restart needed: .*started from ([0-9a-f]{7,40})|restart: .*)$")
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# The ranges reserved for documentation (RFC 5737): what tests and examples use instead of a real address.
DOCUMENTATION_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")
ISSUE_RE = re.compile(r"(?<![\w&])#\d+\b")
MOVE_ONLY_RE = re.compile(r"move[ -]only", re.I)


def started_from(deploy_log, run_sha):
    """The commit the bot runs after the deploy whose log this is: the one its restart check names
    when it did not restart, the run's own commit when it did, None when the log has no decision."""
    decision = None
    for line in deploy_log.splitlines():
        match = DECISION_RE.search(line)
        if match:
            decision = match
    if decision is None:
        return None
    return decision.group(2) or run_sha


def game_status(now, start, game_hours):
    """"live", "soon", "later" or "over" for one game starting at `start` (aware datetimes)."""
    if start <= now <= start + datetime.timedelta(hours=game_hours):
        return "live"
    if now < start <= now + datetime.timedelta(hours=SOON_HOURS):
        return "soon"
    return "later" if start > now else "over"


def added_lines(diff_text):
    """[(path, line_number, text)] for every line a unified diff (zero context is fine) adds."""
    added, path, number = [], None, 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            number = int(match.group(1)) if match else 0
        elif line.startswith("+") and path:
            added.append((path, number, line[1:]))
            number += 1
        elif not line.startswith("-"):
            number += 1
    return added


def move_differences(diff_text):
    """(only_removed, only_added): the non-blank lines a commit's diff removes but does not add
    back anywhere, and the other way round. Both empty means a pure move."""
    removed, added = collections.Counter(), collections.Counter()
    for line in diff_text.splitlines():
        if line.startswith(("+++ ", "--- ")):
            continue
        if line.startswith("-") and line[1:].strip():
            removed[line[1:]] += 1
        elif line.startswith("+") and line[1:].strip():
            added[line[1:]] += 1
    return sorted((removed - added).elements()), sorted((added - removed).elements())


def server_values(conf_text):
    """{name: value} of the server details in fetch_night.conf: the login and host of SERVER
    separately, SERVICE and BOT_DIR. Placeholders and empty values are left out."""
    values = {}
    for line in conf_text.splitlines():
        match = re.match(r'\s*(SERVER|SERVICE|BOT_DIR)="?([^"]*)"?\s*$', line)
        if not match or not match.group(2) or "<" in match.group(2):
            continue
        name, value = match.groups()
        if name == "SERVER" and "@" in value:
            values["login"], values["host"] = value.split("@", 1)
        else:
            values[name.lower()] = value
    return values


def server_hits(lines, values):
    """[(path, line_number, what)] for added lines carrying a server value or an IP address. The
    value itself is not repeated, so the report can be pasted anywhere."""
    hits = []
    for path, number, text in lines:
        for name, value in values.items():
            if len(value) >= 3 and value in text:
                hits.append((path, number, name))
        if any(not ip.startswith(DOCUMENTATION_NETS) for ip in IPV4_RE.findall(text)):
            hits.append((path, number, "an IP address"))
    return hits


def issue_refs(lines):
    """[(line_number, text)] for added CLAUDE.md lines that name an issue number."""
    return [(number, text.strip()) for path, number, text in lines
            if path == "CLAUDE.md" and ISSUE_RE.search(text)]


def pr_lines(prs, now):
    """One line per open pull request: number, title, author and age in days, oldest first."""
    lines = []
    for pr in sorted(prs, key=lambda p: p.get("createdAt") or ""):
        try:
            opened = datetime.datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
            age = f"{(now - opened).days} d old"
        except (KeyError, TypeError, ValueError, AttributeError):
            age = "age unknown"
        author = (pr.get("author") or {}).get("login") or "?"
        lines.append(f"  #{pr.get('number')} {pr.get('title')} ({author}, {age})")
    return lines


def open_prs():
    """The open pull requests as gh lists them, or None when gh could not list them."""
    try:
        result = subprocess.run(["gh", "pr", "list", "--state", "open", "--json", "number,title,author,createdAt"],
                                cwd=REPO_DIR, capture_output=True, text=True, timeout=60)
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True, text=True).stdout


def deployed_commit():
    """(sha, how it was found) the running bot was started from, per the last successful deploy."""
    try:
        listing = subprocess.run(
            ["gh", "run", "list", "--workflow", "CI/CD", "--branch", "main", "--status", "success",
             "--limit", "1", "--json", "databaseId,headSha", "-q", '.[0] | "\\(.databaseId) \\(.headSha)"'],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=60).stdout.split()
        if len(listing) == 2:
            run_id, run_sha = listing
            log = subprocess.run(["gh", "run", "view", run_id, "--log"], cwd=REPO_DIR,
                                 capture_output=True, text=True, timeout=120).stdout
            sha = started_from(log, run_sha)
            if sha:
                return sha, f"the last deploy's log (run {run_id})"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None, "unknown (no readable deploy log)"


def todays_games(now):
    """[(tracker name, status, start)] for each tracker's games today, the trackers whose schedule
    could not be read, and those that cannot start at all now (no series found: Pesis off-season,
    or its feed down - the tracker itself refuses to start then)."""
    games, unreadable, idle = [], [], []
    for cls, hours in TRACKERS:
        command = cls()
        try:
            context = command._resolve_context()
            if command.REQUIRES_CONTEXT and context is None:
                idle.append(command.DISPLAY_NAME)
                continue
            items = command._fetch_today_items(context)
        except Exception as e:  # an unofficial feed: a failure is reported, never fatal
            print(f"  ({command.DISPLAY_NAME}: {e})")
            items = None
        if items is None:
            unreadable.append(command.DISPLAY_NAME)
            continue
        for item in items.values():
            start = command._parse_start_dt(item.get(command.START_TIME_KEY))
            if start is not None:
                games.append((command.DISPLAY_NAME, game_status(now, start, hours), start))
    return games, unreadable, idle


def main(now=None):
    now = now or datetime.datetime.now(UTC)
    attention = []
    print(f"pre-push: {now:%Y-%m-%d %H:%M} UTC")

    prs = open_prs()
    if prs is None:
        print("\nOpen pull requests: could not be listed (gh failed); check with `gh pr list`")
    else:
        print(f"\nOpen pull requests: {len(prs) or 'none'}{' (merging one is a push too)' if prs else ''}")
        for line in pr_lines(prs, now):
            print(line)

    git("fetch", "-q", "origin", "main")
    commits = git("log", "--reverse", "--format=%h %s", "origin/main..HEAD").strip().splitlines()
    if not commits:
        print("\nNothing to push: HEAD is not ahead of origin/main.")
        return 0
    print(f"\nOutgoing ({len(commits)}):")
    for commit in commits:
        print(f"  {commit}")
    if git("status", "--porcelain", "--untracked-files=no").strip():
        print("  (the working tree has uncommitted changes; they do not go out)")

    sha, source = deployed_commit()
    result = subprocess.run(["sh", ".github/needs-restart.sh", sha or ""], cwd=REPO_DIR,
                            capture_output=True, text=True)
    restart = result.returncode == 0
    print(f"\nRestart: {'YES' if restart else 'no'} - {result.stdout.strip()}")
    print(f"  the bot's commit: {sha or '-'} from {source}")

    games, unreadable, idle = todays_games(now)
    print("\nGames today (UTC):")
    for name, status, start in sorted(games, key=lambda g: g[2]):
        print(f"  {name:<11} {start.astimezone(UTC):%m-%d %H:%M}  {status}")
    if not games:
        print("  none")
    if unreadable:
        print(f"  schedule not readable: {', '.join(unreadable)}")
    if idle:
        print(f"  no series found, cannot be tracked now: {', '.join(idle)}")
    busy = sorted({name for name, status, _ in games if status in ("live", "soon")})
    if restart and (busy or unreadable):
        attention.append("a restart while games are on or about to start" if busy
                         else "a restart, and a schedule could not be read")

    for commit in commits:
        short, subject = commit.split(" ", 1)
        if MOVE_ONLY_RE.search(subject):
            only_removed, only_added = move_differences(git("show", "--format=", "-U0", short))
            print(f"\nMove check {short}: {'pure move' if not (only_removed or only_added) else 'lines that changed:'}")
            for line in only_removed:
                print(f"  - {line}")
            for line in only_added:
                print(f"  + {line}")

    lines = added_lines(git("diff", "-U0", "origin/main..HEAD"))
    conf = open(CONF_PATH, encoding="utf-8").read() if os.path.exists(CONF_PATH) else ""
    hits = server_hits(lines, server_values(conf))
    print(f"\nServer details in added lines: {'none' if not hits else ''}")
    for path, number, what in hits:
        print(f"  {path}:{number}: {what}")
    if not conf:
        print("  (tools/fetch_night.conf missing: only IP addresses were looked for)")
    if hits:
        attention.append("server details in the outgoing diff")
    refs = issue_refs(lines)
    if refs:
        print("\nIssue numbers added to CLAUDE.md (fine only where they explain a lasting why):")
        for number, text in refs:
            print(f"  CLAUDE.md:{number}: {text}")

    print(f"\nTo look at: {'; '.join(attention)}" if attention else "\nNothing to look at.")
    return 1 if attention else 0


if __name__ == "__main__":
    sys.exit(main())
