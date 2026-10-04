# tools/ - read-only probes of the live feeds

Small scripts for watching the NHL and Liiga feeds while real games are on, so a change to a
tracker can be checked against what the feed actually does, not only against mocks. They are **not
part of the bot**: nothing in `src/` imports them and they never send anything to IRC or to the
bot. They only read the same public feeds the trackers read, and reuse the trackers' fetch code
(`NHLCommand._fetch_scores` / `_fetch_play_by_play`, `LiigaCommand`), so a change to those needs
the matching change here (`tests/test_tools.py` covers the pure parts).

Run them from the repository root with the project's virtualenv, ideally with the Mac kept awake
(`caffeinate -i`), in the background:

    caffeinate -i nohup .venv/bin/python tools/goal_probe.py 2026-10-04 14 > /dev/null 2>&1 &

Each writes a log to `tools/logs/<name>.log` (the folder is created on first use; `*.log` is
git-ignored) and prints the same lines. Every line carries UTC and the bot's display time.

| script | reads | logs |
|---|---|---|
| `goal_probe.py DATE [hours]` | NHL play-by-play of every game of a US-Eastern slate date | per goal: first sight (scorer, assists), later changes of scorer / assists / clock with names and the seconds since first seen, goals that **leave** the play list (disallowed or a feed flicker) and **come back**, goals listed **twice**; per game ending, whether the header score agreed with the play list; a summary at the end |
| `state_watch.py DATE [hours]` | NHL `/score/DATE` | every change of a game's state (`LIVE`, `CRIT`, `OVER`, `FINAL`, `OFF`, ...) with score, period and clock; shows how long the transitional `OVER` state lasts |
| `liiga_sampler.py DATE [hours]` | Liiga games feed (`runkosarja`) every 10 s | every poll with the response's CDN headers (`Age`, cache node) and each live game's period / `gameTime` / score; flags every **backwards** step of clock, score, period or `ended`; `FIELD` lines when `spectators` or `finishedType` change; a summary at the end |

`DATE` is `YYYY-MM-DD` (for the NHL, the **US-Eastern** date of the slate; for Liiga, the local
game date). `hours` is the longest the script runs; each also stops by itself when its games are
over. Running a script without arguments prints its usage.

## How they are used

1. Start the matching probe before the games (it can run all day), and `!nhl start` / `!liiga start`
   in the channel as usual.
2. Afterwards compare the probe's log with the channel log: which goals the bot announced, what it
   got wrong, and what the feed did around it.
3. Put what was found in the relevant GitHub issue (counts and short timelines; no identifying
   details).

## Keep these free of identifying details

The repository is public. The scripts only print public sports data. Do not hard-code local paths,
user names, hostnames, keys or addresses in them, and do not commit their logs (`*.log` is ignored;
keep it that way).
