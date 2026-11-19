---
name: post-game
description: The morning check after a game night (NHL and/or Liiga) - read the bot's journal, nightcheck output and probe logs fetched by tools/fetch_night.sh, compare every FINAL with the official results, find the journal lines open issues are waiting for, and draft issue updates for approval. Use when Heikki says the night's files are fetched, types /post-game, or asks to check last night's games.
---

# Post-game check

The files come from `tools/fetch_night.sh NAME FROM TO [--probe] [--sampler]`, run by Heikki on the
Mac (Claude has no server access, by his choice). They are in `tools/logs/`:
`journal_NAME.log`, `nightcheck_NAME.log`, and when the extras ran `goal_probe_server.log` /
`liiga_sampler_server.log` (these two append across nights).

The argument is NAME (e.g. `2026-11-19_nhl`). Without one, use the newest `journal_*.log`, and say
which one you picked.

## If the files are not there yet

Run `date -u`, look up which slates were played (NHL `https://api-web.nhle.com/v1/schedule/<date>`,
Liiga `https://www.liiga.fi/api/v2/games?tournament=runkosarja&season=<season>`), and give Heikki the
fetch command with the window filled in: from 1 h before the first game to 2 h after the last game
ends, ending before 06:00 UTC when possible (an automatic update can restart the bot 06:00-07:00
UTC). Add `--probe` on slates of 10+ NHL games and `--sampler` on days of 5+ Liiga games: the
server's daily timer (`tools/start_extras.py`) starts them by itself on those days. Then stop and wait.

## The check, in this order

1. **The report.** `python3 tools/journal_report.py tools/logs/journal_NAME.log --year <year>`.
   Read its per-game sections and its anomalies list.
2. **The nightcheck sections.** Read all six from `nightcheck_NAME.log`, including the empty ones.
   Count the link-title failures in the journal: `grep -ac 'URL title: none' tools/logs/journal_NAME.log`
   (and `gave up after` lines: each should be followed by that fetch's own `none for` line soon after).
3. **Every FINAL against the official result.** NHL: `https://api-web.nhle.com/v1/score/<US-Eastern
   date>` (score, and `gameOutcome.lastPeriodType` for OT/SO). Liiga: the games list for the day (the
   tracker's own feed, `src/liiga_command.py` `BASE_URL`). Check the score, the `(OT)`/`(SO)` mark,
   exactly one FINAL per game per channel, and the attendance figure if present.
4. **The raw journal, not only the report.** Read the GOAL/NO GOAL/FINAL lines and the bot's own
   `NHL:`/`Liiga:` decision lines around anything unusual: a retraction, a late FINAL, a restart
   (`CONNECTED`/`DISCONNECTED`), an error. Times are UTC.
5. **The probe/sampler log, if fetched.** Pick tonight's run by its start line (`goal probe start,
   slate <date>`), and compare its goal timeline with what the bot posted: re-keyed or double-listed
   goals, goals that left and came back, challenge stoppages and how late they appeared. On a day
   the daily timer should have started one (10+ NHL / 5+ Liiga games) and the log has no run for the
   day, ask Heikki for the timer's journal (the command is in DEPLOY.md, "The probes start
   themselves") to see what it planned.
6. **Open issues waiting for live evidence.** `gh issue list --state open`, then read each one whose
   Status says "open until ..." and grep the journal for the line it names (for wording, see the fix
   commit: `git log --grep="#N"`). Typical: a `took the game back from final` line (#73), `the score
   changed between the first FINAL poll` (#70), `retrying once` (#45), a retraction that waited for
   its reason (#64), `attendance ... found <s> s after` (#65), `gave up after` (#88/#89).

## The report to Heikki

- One line per game: teams, the FINAL as posted, the official result, OK or the difference.
- Anomalies, each with a short UTC timeline (no nicks, hosts or addresses).
- Per waiting issue: seen / not seen tonight, with the evidence line(s).
- Then the drafted GitHub changes: evidence comments, rewritten `## Status` sections, closings, and
  the #21 tracking-issue lines. **Post nothing until Heikki says yes** (creating, editing or closing
  issues is outward-facing). New problems get an issue with one `priority:` and one area label.

## Rules that always apply

- The repository is public: never put server details (address, login, unit name, paths), nicks,
  chat or `DEPLOY.md` content in an issue, comment, commit or tracked file. Quote journal lines only
  after stripping host prefixes, user addresses and chat.
- The fetched `*.log` files stay out of git.
- If a new journal line was added to the bot since the last check, remind Heikki to add its pattern
  to the server's `nightcheck.sh` (and update `tools/journal_report.py` if it parses that wording).
