"""Liiga feed sampler. Read-only: it only reads liiga.fi's public games feed and never touches the bot.

Polls the `runkosarja` games of one date every 10 s (one request per poll) and logs every poll as
one line with the response's CDN headers (Age, first-hop CloudFront node, X-Cache) and each live
game's (period / gameTime / score), plus a flag when a game's gameTime, score, period or `ended`
went BACKWARDS compared with the previous poll. FIELD lines record when a game's `spectators` or
`finishedType` changes (to see whether they appear before `ended`). The summary at the end counts
the backwards steps by kind, the Age values and the cache nodes seen.

Usage (from the repository root):
    .venv/bin/python tools/liiga_sampler.py YYYY-MM-DD [max_hours]
Stops when every game of the date has ended, or after max_hours (default 8).
Log: tools/logs/liiga_sampler.log
"""
import collections
from datetime import datetime
import sys
import time

from probe_common import make_logger
from liiga_command import LiigaCommand

POLL_SECONDS = 10

log = make_logger("liiga_sampler")


def edge_of(via):
    """First CloudFront hop id (first 6 characters) from a Via header, or '?'."""
    try:
        return via.split(",")[0].split()[1][:6]
    except Exception:
        return "?"


def judge(prev, cur):
    """Backwards flags between two polls of one game, as a list of strings. Pure."""
    flags = []
    if prev is None:
        return flags
    if cur["gameTime"] is not None and prev["gameTime"] is not None and cur["gameTime"] < prev["gameTime"]:
        flags.append(f'CLOCK-BACK {prev["gameTime"] - cur["gameTime"]}s')
    if cur["home"] < prev["home"] or cur["away"] < prev["away"]:
        flags.append(f'SCORE-BACK {prev["home"]}-{prev["away"]}->{cur["home"]}-{cur["away"]}')
    if prev["ended"] and not cur["ended"]:
        flags.append("ENDED-FLAP")
    if cur["period"] is not None and prev["period"] is not None and cur["period"] < prev["period"]:
        flags.append(f'PERIOD-BACK {prev["period"]}->{cur["period"]}')
    return flags


def state_of(game):
    return {"gameTime": game.get("gameTime"),
            "home": (game.get("homeTeam") or {}).get("goals") or 0,
            "away": (game.get("awayTeam") or {}).get("goals") or 0,
            "ended": bool(game.get("ended")), "period": game.get("currentPeriod")}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command = LiigaCommand()
    date = sys.argv[1]
    deadline = time.time() + float(sys.argv[2] if len(sys.argv) > 2 else 8) * 3600
    season = command._current_season(datetime.strptime(date, "%Y-%m-%d"))
    log(f"liiga sampler start, date {date}")
    previous, field_state = {}, {}
    kinds = collections.Counter()
    ages, back_ages = [], []
    edges, back_edges = collections.Counter(), collections.Counter()
    polls = 0
    while time.time() < deadline:
        try:
            r = command.session.get(command.BASE_URL, params={"tournament": "runkosarja", "season": season,
                                                              "date": date}, timeout=15)
            age = r.headers.get("Age")
            edge = edge_of(r.headers.get("Via", ""))
            cache = r.headers.get("X-Cache", "?")
            games = r.json().get("games") or []
            polls += 1
            edges[edge] += 1
            if age and age.isdigit():
                ages.append(int(age))
            cells, flagged = [], []
            for game in games:
                name = f'{game["homeTeam"]["teamName"]}-{game["awayTeam"]["teamName"]}'
                cur = state_of(game)
                if game.get("started") and not game.get("ended"):
                    cells.append(f'{game["homeTeam"]["teamName"][:4]}:p{cur["period"]}/{cur["gameTime"]}/'
                                 f'{cur["home"]}-{cur["away"]}')
                for flag in judge(previous.get(game["id"]), cur):
                    flagged.append(f"{name}: {flag}")
                    kinds[flag.split()[0]] += 1
                previous[game["id"]] = cur
                fields = (game.get("spectators"), game.get("finishedType"))
                if field_state.get(game["id"]) != fields:
                    log(f'FIELD {name}: spectators={fields[0]} finishedType={fields[1]} (ended={cur["ended"]}, '
                        f'period={cur["period"]}, gameTime={cur["gameTime"]}, score={cur["home"]}-{cur["away"]})')
                    field_state[game["id"]] = fields
            if cells or flagged:
                log(f"age={age} edge={edge} {cache} | " + " ".join(cells)
                    + (" | **" + "; ".join(flagged) + "**" if flagged else ""))
            if flagged:
                back_ages.append(int(age) if age and age.isdigit() else -1)
                back_edges[edge] += 1
            if games and all(g.get("ended") for g in games):
                log("all games ended, done")
                break
        except Exception as e:
            log(f"error: {type(e).__name__}: {e}")
        time.sleep(POLL_SECONDS)
    log(f"summary: {polls} polls; backwards events by kind: {dict(kinds)}")
    if ages:
        log(f"  Age header: min {min(ages)}, median {sorted(ages)[len(ages) // 2]}, max {max(ages)}")
    log(f"  Age at the polls that went backwards: {sorted(back_ages)}")
    log(f"  cache nodes seen: {len(edges)}; nodes at backwards polls: {len(back_edges)}")


if __name__ == "__main__":
    main()
