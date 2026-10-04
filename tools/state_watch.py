"""NHL game-state watcher. Read-only. Logs every change of each game's gameState (FUT, PRE,
LIVE, CRIT, OVER, FINAL, OFF) with the score, period and clock at that moment, from NHL's
/score/{date}. Shows how long the transitional OVER state lasts at the end of a game.

Usage (from the repository root):
    .venv/bin/python tools/state_watch.py YYYY-MM-DD [max_hours]
Stops two minutes after every game is FINAL/OFF, or after max_hours (default 14).
Log: tools/logs/state_watch.log
"""
import sys
import time

from probe_common import make_logger
from nhl_command import NHLCommand

log = make_logger("state_watch")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command = NHLCommand()
    command.SCORE_CACHE_SECONDS = 0
    date = sys.argv[1]
    deadline = time.time() + float(sys.argv[2] if len(sys.argv) > 2 else 14) * 3600
    last = {}
    log(f"state watch start, slate {date}")
    while time.time() < deadline:
        games = command._fetch_scores(date)
        if games:
            for gid, game in games.items():
                key = (game.get("gameState"), game.get("gameScheduleState"))
                if last.get(gid) != key:
                    label = f'{game["awayTeam"]["abbrev"]}@{game["homeTeam"]["abbrev"]}'
                    period = (game.get("periodDescriptor") or {}).get("number")
                    clock = (game.get("clock") or {}).get("timeRemaining")
                    log(f'{label}: {last.get(gid, ("-", "-"))[0]} -> {key[0]} (schedule {key[1]}) '
                        f'score {game["homeTeam"].get("score")}-{game["awayTeam"].get("score")} '
                        f'period {period} clock {clock}')
                    last[gid] = key
            if len(last) == len(games) and all(k[0] in ("FINAL", "OFF") for k in last.values()):
                time.sleep(120)
                log("all games final, done")
                break
        time.sleep(10)


if __name__ == "__main__":
    main()
