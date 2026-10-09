"""NHL goal probe. Read-only: it only reads NHL's public web API and never touches the bot.

For every game of one US-Eastern slate date it polls the play-by-play and logs, per goal:
  FIRST SEEN  scorer and assists the first time the goal appears;
  SCORER / ASSISTS / CLOCK  every later change, with names and seconds since first seen
              (SCORER also says whether the old scorer became one of the assists);
  REMOVED / REAPPEARED  a goal that leaves the play list (disallowed, or a feed flicker);
  DOUBLY LISTED  two goal plays with the same team and running score;
and, when a game reaches OVER/FINAL, whether the header score agreed with the play list.

Usage (from the repository root):
    .venv/bin/python tools/goal_probe.py YYYY-MM-DD [max_hours] [poll_seconds]
It polls every poll_seconds (default 10; half of that, at least 5, while a game is ending). It stops
when every game is finished and settled, after max_hours (default 14), or when the API has not
answered for MAX_SILENT_CYCLES cycles in a row (so an outage or a throttle is not hammered). Run
from the machine that also runs the bot, use a slower interval (20): its requests add to the bot's.
Log: tools/logs/goal_probe.log
"""
import statistics
import sys
import time

from probe_common import make_logger
from nhl_command import NHLCommand

LIVE_POLL, ENDING_POLL = 10, 5      # seconds; faster while a game is ending
WATCH_AFTER_END = 300               # keep watching a finished game this long
MAX_SILENT_CYCLES = 10              # stop after this many cycles in a row without an answer from the API

log = make_logger("goal_probe")


def names_of(pbp):
    out = {}
    for r in pbp.get("rosterSpots", []):
        first = (r.get("firstName") or {}).get("default", "")
        last = (r.get("lastName") or {}).get("default", "")
        out[r["playerId"]] = f"{first} {last}".strip()
    return out


def snap(play, names):
    d = play.get("details") or {}
    assists = tuple(d.get(k) for k in ("assist1PlayerId", "assist2PlayerId") if d.get(k))
    return {"scorer": d.get("scoringPlayerId"), "assists": assists, "clock": play.get("timeInPeriod"),
            "label": f'{names.get(d.get("scoringPlayerId"), "?")} '
                     f'[{", ".join(names.get(i, "?") for i in assists) or "no assists"}]'}


def observe(goals, label, pbp, now):
    """Updates `goals` ({(label, eventId): record}) from one play-by-play payload and returns
    human-readable event strings. Pure, so it can be tested with made-up payloads."""
    names = names_of(pbp)
    events = []
    current = {p.get("eventId"): p for p in pbp.get("plays", []) if p.get("typeDescKey") == "goal"}

    seen_scores = goals.setdefault("_doubly_listed", set())
    keys = {}
    for eid, play in current.items():
        if (play.get("periodDescriptor") or {}).get("periodType") == "SO":
            continue  # shootout goals all carry the same running score: not a sign of a doubly listed goal
        d = play.get("details") or {}
        keys.setdefault((d.get("eventOwnerTeamId"), d.get("homeScore"), d.get("awayScore")), []).append(eid)
    for key, ids in keys.items():
        if len(ids) > 1 and key[1] is not None and (label, key) not in seen_scores:
            seen_scores.add((label, key))
            events.append(f"{label}: DOUBLY LISTED goal, same team and running score {key[1]}-{key[2]}, event ids {ids}")

    for eid, play in current.items():
        key = (label, eid)
        s = snap(play, names)
        period = (play.get("periodDescriptor") or {}).get("number")
        g = goals.get(key)
        if g is None:
            goals[key] = {"first": s, "last": s, "t0": now, "removed": None, "period": period,
                          "scorer_changes": [], "assist_events": []}
            events.append(f'{label} P{period} {s["clock"]}: FIRST SEEN {s["label"]}')
            continue
        if g["removed"] is not None:
            events.append(f'{label} P{period} {s["clock"]}: REAPPEARED in the feed after '
                          f'{now - g["t0"] - g["removed"]:.0f}s away')
            g["removed"] = None
        if s["scorer"] != g["last"]["scorer"]:
            # The pattern seen so far: the first scorer in the feed ends up credited with an assist.
            was_assist = g["last"]["scorer"] in s["assists"]
            g["scorer_changes"].append((now - g["t0"], was_assist))
            events.append(f'{label} P{period} {g["first"]["clock"]}: SCORER '
                          f'{g["last"]["label"].split(" [")[0]} -> {s["label"].split(" [")[0]} '
                          f'after {now - g["t0"]:.0f}s (old scorer is now one of the assists: '
                          f'{"YES" if was_assist else "no"})')
        if s["assists"] != g["last"]["assists"]:
            g["assist_events"].append((now - g["t0"], len(g["last"]["assists"]), len(s["assists"])))
            before = ", ".join(names.get(i, "?") for i in g["last"]["assists"]) or "none"
            after = ", ".join(names.get(i, "?") for i in s["assists"]) or "none"
            events.append(f'{label} P{period} {g["first"]["clock"]}: ASSISTS [{before}] -> [{after}] '
                          f'after {now - g["t0"]:.0f}s')
        if s["clock"] != g["last"]["clock"]:
            events.append(f'{label} P{period} {g["first"]["clock"]}: CLOCK {g["last"]["clock"]} -> '
                          f'{s["clock"]} after {now - g["t0"]:.0f}s')
        g["last"] = s

    for key, g in goals.items():
        if key == "_doubly_listed":
            continue
        if key[0] == label and key[1] not in current and g["removed"] is None:
            g["removed"] = now - g["t0"]
            events.append(f'{label} P{g["period"]} {g["last"]["clock"]}: REMOVED from the feed '
                          f'{now - g["t0"]:.0f}s after first seen (last: {g["last"]["label"]})')
    return events


def header_state(pbp):
    """(header score, running score of the last non-shootout goal, header outcome, last period type)"""
    header = ((pbp.get("homeTeam") or {}).get("score"), (pbp.get("awayTeam") or {}).get("score"))
    last = None
    for p in pbp.get("plays", []):
        if p.get("typeDescKey") == "goal" and (p.get("periodDescriptor") or {}).get("periodType") != "SO":
            d = p.get("details") or {}
            last = (d.get("homeScore"), d.get("awayScore"))
    period_type = None
    for p in pbp.get("plays", []):
        period_type = (p.get("periodDescriptor") or {}).get("periodType") or period_type
    return header, last if last else (0, 0), (pbp.get("gameOutcome") or {}).get("lastPeriodType"), period_type


def check_header(ends, label, state, pbp, now):
    """Logs header-vs-plays agreement once a game is OVER/FINAL/OFF, and when it catches up.
    Returns True while this game still needs fast polling."""
    if state not in ("OVER", "FINAL", "OFF"):
        return False
    rec = ends.setdefault(label, {"t0": now, "ok_at": None, "first": None, "first_agrees": None})
    header, plays, outcome, period_type = header_state(pbp)
    if period_type == "SO":
        # The shootout winner's goal is only in the header, never in the running score: a header that
        # is exactly one goal ahead of the plays, with the shootout outcome, is the right one; a header
        # equal to the plays is the stale one.
        gap = tuple(h - r for h, r in zip(header, plays)) if all(isinstance(v, int) for v in header + plays) else None
        agrees = gap in ((1, 0), (0, 1)) and outcome == "SO"
    else:
        agrees = header == plays and (period_type != "OT" or outcome in ("OT", "SO"))
    if rec["first"] is None:
        rec["first"] = (header, plays, outcome, period_type, state)
        rec["first_agrees"] = agrees
        log(f"{label}: first check in state {state}: header score {header[0]}-{header[1]}, plays' running "
            f"score {plays[0]}-{plays[1]}, header outcome {outcome}, last play period type {period_type} -> "
            f'{"AGREE" if agrees else "MISMATCH"}')
    if agrees and rec["ok_at"] is None:
        rec["ok_at"] = now
        if not rec["first_agrees"]:
            log(f"{label}: header caught up {now - rec['t0']:.0f}s after the first check")
    return not agrees


def summarize(goals, ends):
    real = {k: g for k, g in goals.items() if k != "_doubly_listed"}
    removed = sorted(round(g["removed"]) for g in real.values() if g["removed"] is not None)
    log(f"summary: {len(real)} goals seen in the feed, {len(removed)} removed, "
        f"{len(goals.get('_doubly_listed', ()))} doubly listed")
    for n in (0, 1, 2):
        log(f"  first seen with {n} assist(s): {sum(1 for g in real.values() if len(g['first']['assists']) == n)}")
    changes = [(t, w) for g in real.values() for t, w in g["scorer_changes"]]
    log(f"  scorer changes: {len(changes)} (at s: {sorted(round(t) for t, _ in changes)}); the old scorer "
        f"became one of the assists in {sum(1 for _, w in changes if w)} of them")
    gained = sorted(round(t) for g in real.values() for t, a, b in g["assist_events"] if b > a)
    lost = sorted(round(t) for g in real.values() for t, a, b in g["assist_events"] if b < a)
    log(f"  assists gained after first seen: {len(gained)} at s {gained}")
    log(f"  assists lost after first seen: {len(lost)} at s {lost}")
    log(f"  removed goals, seconds after first seen: {removed}")
    if gained:
        log(f"  median seconds until assists were gained: {statistics.median(gained):.0f}")
    for label, rec in ends.items():
        agreed = rec["first_agrees"]
        log(f"  {label}: header at first check in an end state: {'agreed' if agreed else 'MISMATCH'}")


def parse_args(argv):
    """(date, max_hours, poll_seconds) from the command line, or None for a usage error."""
    if len(argv) < 2 or len(argv) > 4:
        return None
    try:
        max_hours = float(argv[2]) if len(argv) > 2 else 14
        poll_seconds = float(argv[3]) if len(argv) > 3 else LIVE_POLL
    except ValueError:
        return None
    if poll_seconds < ENDING_POLL or max_hours <= 0:
        return None
    return argv[1], max_hours, poll_seconds


def run(command, date, deadline, poll_seconds, sleep=time.sleep, clock=time.time):
    """Polls until every game is settled, `deadline` (a clock() value) passes, or the API goes silent for
    MAX_SILENT_CYCLES cycles in a row. Returns (goals, ends, why it stopped)."""
    ending_poll = max(ENDING_POLL, poll_seconds / 2)
    goals, ends, ended_at = {}, {}, {}
    silent = 0
    reason = "deadline"
    while clock() < deadline:
        fast = False
        answered = False
        pbp_tried = pbp_ok = 0
        try:
            scores = command._fetch_scores(date)
            games = scores or {}
            all_done = bool(games)
            for gid, game in games.items():
                label = f'{game["awayTeam"]["abbrev"]}@{game["homeTeam"]["abbrev"]}'
                state = game.get("gameState")
                if state in ("FUT", "PRE"):
                    all_done = False
                    continue
                if state in ("FINAL", "OFF"):
                    ended_at.setdefault(gid, clock())
                    if clock() - ended_at[gid] > WATCH_AFTER_END:
                        continue
                all_done = False
                pbp = command._fetch_play_by_play(gid)
                pbp_tried += 1
                if not pbp:
                    continue
                pbp_ok += 1
                now = clock()
                for event in observe(goals, label, pbp, now):
                    log(event)
                fast = check_header(ends, label, state, pbp, now) or state == "OVER" or fast
            answered = scores is not None and (pbp_tried == 0 or pbp_ok > 0)
            if all_done:
                reason = "all games settled"
                break
        except Exception as e:
            log(f"error: {type(e).__name__}: {e}")
        silent = 0 if answered else silent + 1
        if silent >= MAX_SILENT_CYCLES:
            log(f"stopping: the API has not answered in {silent} cycles in a row")
            reason = "API silent"
            break
        sleep(ending_poll if fast else poll_seconds)
    return goals, ends, reason


def main():
    args = parse_args(sys.argv)
    if args is None:
        sys.exit(__doc__)
    date, max_hours, poll_seconds = args
    command = NHLCommand()
    command.SCORE_CACHE_SECONDS = 0
    log(f"goal probe start, slate {date}, every {poll_seconds:g} s")
    goals, ends, _ = run(command, date, time.time() + max_hours * 3600, poll_seconds)
    summarize(goals, ends)


if __name__ == "__main__":
    main()
