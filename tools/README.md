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
| `goal_probe.py DATE [hours] [poll_seconds]` | NHL play-by-play of every game of a US-Eastern slate date | per goal: first sight (scorer, assists), later changes of scorer / assists / clock with names and the seconds since first seen, goals that **leave** the play list (disallowed or a feed flicker) and **come back**, goals listed **twice**; per game ending, whether the header score agreed with the play list; a summary at the end |
| `state_watch.py DATE [hours]` | NHL `/score/DATE` | every change of a game's state (`LIVE`, `CRIT`, `OVER`, `FINAL`, `OFF`, ...) with score, period and clock; shows how long the transitional `OVER` state lasts |
| `liiga_sampler.py DATE [hours]` | Liiga games feed (`runkosarja`) every 10 s | every poll with the response's CDN headers (`Age`, cache node) and each live game's period / `gameTime` / score; flags every **backwards** step of clock, score, period or `ended`; `FIELD` lines when `spectators` or `finishedType` change; a summary at the end |

### `journal_report.py` - a report on the bot's own journal

Not a probe: it reads text, not the feeds. It turns a night's `journalctl -a` output (a file or
stdin) into a plain-text report per channel and game: the `GOAL:` / `NO GOAL:` / `FINAL:` lines the
bot posted, the delay from the last goal to the FINAL, whether and when the attendance arrived, the
lines the bot journals about its own decisions (a repeated goal ignored, a goal retracted, the FINAL
score source), the end-of-slate results list (told apart from someone's `!nhl results`), the
`!nhl now` boards it saw (their live statuses), and an **anomalies** list: a duplicate `GOAL:` line, a goal retracted and then
announced again, a `NO GOAL:` with no reason, a FINAL that differs from the last goal, a FINAL that
never got its attendance, goals without a FINAL, restarts and errors.

    <journalctl command for the bot's unit> -a --since "..." --until "..." | python3 tools/journal_report.py
    python3 tools/journal_report.py journal.txt --year 2026

It uses only the bot's own lines and ignores incoming chat; host names, process ids, nicks and
addresses never reach the report, and printed free text is scrubbed of home paths, addresses and
user@host strings. Standard library only, so it runs on the server or on a laptop with a copy of the
journal. NHL and Liiga lines are covered, the Pesis trackers' are not. A raw journal file contains
chat and addresses: keep it in `tools/logs/` (ignored by git), never commit or post it.
(`tests/test_journal_report.py` includes round trips through the bot's own message builders, so a
reworded message breaks a test there.)

`goal_probe.py` polls every `poll_seconds` (default 10, half of that while a game is ending) and stops by
itself when the games are settled, after `hours`, or when the API has not answered for 10 cycles in a row
(so an outage or a throttle is not hammered). It makes about 1 + N requests per cycle for N live games:
run from the machine that also runs the bot, use a slower interval (20) so that its requests add less to the
bot's own.

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
