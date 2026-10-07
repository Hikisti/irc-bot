# Working on KukistiBot with Claude

This file is for Claude (or anyone else picking this project up cold). See
`README.md` for what the bot does and how to run it, and `DEPLOY.md`
(gitignored, not in this repo's history - ask Heikki for it) for actual
server access details. This file is about *how* to work on this codebase,
not what it is.

## Architecture at a glance

- `BaseCommand` (`src/base_command.py`) is the contract every command
  implements: `ALIASES`, `ALLOW_ARGS`, `CHANNELS`, `HELP`, `execute(args,
  irc_bot=, channel=)`. `CommandHandler` builds its alias-dispatch table from
  these at startup - a command with no `ALIASES` is unreachable.
- `LiveTrackerCommand` (`src/live_tracker_command.py`) is the shared engine
  behind `LiigaCommand`, `PesisCommand`, and `NHLCommand` - identical
  start/stop/next lifecycle, poll loop, early-start guard, and
  already-finished guard. Look here first before touching any of their
  lifecycle code; only the actual fetch/announce logic (`_poll_once`,
  `_fetch_today_items`, `_build_initial_state`) is sport-specific. It also
  owns `_fetch_concurrently()` - the one and only way to fetch N
  independent per-item network calls in parallel here (see "Concurrent
  fetching" below); never hand-roll `ThreadPoolExecutor`/`executor.map()`
  again in a new subclass.
- `NHLCommand` also mixes in `NHLScoreboardMixin` (`src/nhl_scoreboard.py`):
  the one-shot `!nhl now`/`!nhl results` lookups, which read the
  `/score/{date}` endpoint (it carries period and clock; the schedule the
  tracker polls doesn't) and never touch tracker state. A subcommand that
  isn't start/stop/next goes in `SUBCOMMANDS` (what `!help` and the usage
  reply list) and the subclass's own `execute()`.
- `PesisCommand` is built from three mixins: `PesisEventParsingMixin`
  (turns the raw event feed into RUN:/JAKSO:/FINAL: text),
  `PesisPlayerNamesMixin` (scorer/batter name resolution), and
  `PesisDataFetchingMixin` (all pesistulokset.fi HTTP calls).
  `SuperpesisCommand`/`YkkospesisCommand` are thin subclasses of it.
- `tools/` holds read-only probes of the live feeds (`goal_probe.py`,
  `state_watch.py` for the NHL, `liiga_sampler.py` for Liiga, shared
  `probe_common.py`; see `tools/README.md`). They are not part of the bot:
  nothing in `src/` imports them and they never talk to IRC. They reuse the
  trackers' own fetch methods (`_fetch_scores`, `_fetch_play_by_play`,
  `_current_season`), so renaming one of those means updating `tools/` too.
  `tests/test_tools.py` covers their pure parts; the polling loops are
  verified by running them. `journal_report.py` is the odd one out: it reads
  no feed, it turns the bot's own journal (`journalctl -a` text) into a
  per-game report and an anomalies list. It parses the bot's message
  wording (GOAL:/NO GOAL:/FINAL: and the `NHL:` prints), so changing one of
  those means updating it; `tests/test_journal_report.py` round-trips the
  bot's own message builders to catch that.
- `liiga.fi`, `pesistulokset.fi`, and `api-web.nhle.com` are **unofficial,
  reverse-engineered APIs** - not documented, can change shape without
  notice. This is why the code leans defensive throughout (never crash on
  an unexpected field, degrade to an error string instead) - keep that
  posture in any new code touching these APIs, don't tighten assumptions
  "for cleanliness." When integrating a *new* one of these, verify what
  timezone it buckets/anchors its own dates by before assuming it matches
  Helsinki - `NHLCommand` needed its own `EASTERN_TZ` for exactly this
  reason (NHL schedule dates are bucketed by US Eastern time, confirmed
  live), separate from `HELSINKI_TZ` used for every displayed time.

## Adding a command

1. Subclass `BaseCommand` (or `LiveTrackerCommand` for a live tracker) and
   add it to `COMMAND_CLASSES` in `src/command_handler.py` - the only
   registration step. (`HelpCommand` is the one exception: it needs the
   finished handler, so `CommandHandler.__init__` registers it directly.)
2. Set `HELP` - the short usage string `!help` lists, e.g. `"!stock
   <ticker>"`. There is no separate help file to update; a test fails if a
   registered command has none.
3. If it takes arguments, calling it bare must reply `Usage: ... (e.g.,
   ...)`. A test enforces the `Usage:` prefix for every argument-taking
   command.
4. Add tests, plus a row in the README commands table and a paragraph if
   the behavior isn't obvious.

## Concurrent fetching

`LiveTrackerCommand._fetch_concurrently(items, fetch_fn)` runs `fetch_fn`
across `items` in parallel and returns `{item: result_or_None}`. Use it for
any "N independent per-item network calls" case (seeding a match/game's
extra detail, polling several tracked items' own per-item state) instead
of writing `ThreadPoolExecutor`/`executor.map()` directly. Confirmed live
as a real, recurring bug shape: a bare `executor.map()`'s returned iterator
raises the *first* failing future's exception as soon as it's reached,
aborting collection for every other item's result too - both
`NHLCommand`'s per-game play-by-play fetch and `PesisCommand`'s
`_seed_match_extras` had this independently before `_fetch_concurrently()`
existed. `_fetch_concurrently()` submits every future up front and
retrieves each one's own result (or exception) individually, so one item's
failure never affects any other's.

## Development workflow

- Python 3.14 everywhere (local venv, CI, production) - keep these in sync
  if the venv is ever rebuilt (see `DEPLOY.md`'s "rebuilding the venv" note
  for a real incident this caused and how to avoid it).
- `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r
  requirements-dev.txt`
- `pytest` for the suite; `pytest --cov=src --cov-report=term-missing` for
  coverage. Tests mirror `src/` one-to-one under `tests/`. Shared test
  builders/fixtures live in `tests/conftest.py` (e.g. `make_json_response`)
  and `tests/pesis_test_helpers.py`. No test hits a real network call -
  everything's mocked.

## The methodology to follow on every change here

This project has been refactored and extended over many sessions under a
consistent, deliberate discipline - keep following it:

- After editing a file, run *that file's* tests immediately
  (`pytest tests/test_whatever.py -q`), not just at the very end.
- Before considering any task done: run the **full** suite, check coverage
  hasn't regressed on touched modules, and - where feasible - verify the
  actual behavior against the real live API/data, not just mocks (e.g. a
  real `!stock`/`!liiga`/`!superpesis` call against the live service).
  Several real bugs in this codebase were only caught this way, not by
  mocked tests alone.
- To check a live-tracker change against real games, start the matching
  probe from `tools/` before the games, compare its log with the channel
  log afterwards, and put what it showed (counts, short timelines) in the
  relevant issue. The repo is public: probes and their output must carry no
  identifying details (no names, local paths, hostnames, keys), and their
  logs stay out of git (`*.log` is ignored; keep it that way).
- Tests must not depend on a local `.env`: it's gitignored, so CI never
  has it, and a test that only passes because of a key in it fails there
  (this happened once). `tests/conftest.py` disables `.env` loading during
  tests (`PYTHON_DOTENV_DISABLED`), so a test that needs a key must set it
  itself with `monkeypatch.setenv`.
- Never silently change observable behavior (a message's wording, a
  command's output shape) without calling it out first.
- Don't add features, refactor, or add abstractions beyond what a task
  actually needs. Comments only when the *why* is genuinely non-obvious
  (a hidden constraint, a workaround for a confirmed live incident) - not
  for what the code already says by being well-named.
- Refactoring is triggered, not scheduled. Propose one (never fold it
  into a feature or fix) when a third copy of a pattern appears or a
  file passes about 600 lines. Keep it behavior-neutral, in its own
  commit, and never right before a live-tracker change is verified
  against real games.
- Problems and ideas that aren't being fixed now (a known quirk, a
  deferred option, a parked decision) go in GitHub Issues, not in code
  comments, memory, or a TODO file. The repo is public, so nothing on
  GitHub - an issue or its comments, a commit message, a PR, a tracked
  file - ever holds server details: IP addresses, hostnames, login or user
  names, paths, the service name, keys, or anything from `DEPLOY.md`.
  Journal and log excerpts are quoted only after stripping host prefixes,
  user addresses and chat. Creating or closing an issue is outward-facing:
  confirm first.
  Every new issue gets one `priority: high|medium|low` label (high = a
  visible error in a channel that has already happened) and one area label
  (`nhl`, `liiga`, `pesis`, `imdb`); the pinned tracking issue holds the
  order of work, so update its checklist when a priority changes.
- Only commit when explicitly asked. Never push proactively either - see
  below for why that's a bigger deal here than in most repos.

## Deployment is not free - `git push` to `main` is a live production action

Every push to `main` runs the test suite, then - if it passes - deploys
over SSH to the production VPS and **restarts the live bot**, dropping and
rejoining every IRC channel it's in, with real users present. Treat a push
here the same way you'd treat any other production deploy, not a plain git
operation:

- Confirm with Heikki before pushing, even if you were explicitly asked to
  implement something - "implement this" is not the same as "push this."
- For anything touching the live-tracker commands (`!liiga`/`!superpesis`/
  `!ykkospesis`) specifically: avoid pushing while a game/match is actively
  being tracked in production. Ask, or wait until today's games are over,
  the same way this was handled for the Liiga fixes in this project's
  history.
- `DEPLOY.md` is gitignored on purpose - it holds real server access
  details (paths, service names, SSH setup). Never `git add` it, never
  suggest committing it, even if asked to "just add everything."

## Commit conventions

Commit messages in this repo explain the *why*, not just the *what* -
especially for bug fixes: what the real symptom was, what the root cause
turned out to be (often confirmed against the live API), and why the fix
addresses that root cause rather than just the symptom. Look at recent
`git log` output here for the tone/length to match before writing one from
scratch.

## Keeping this file current

When a new standing rule gets established (a correction meant to
generalize beyond the one task it came up in) or the architecture changes
in a way that makes a section above stale, propose adding it here - don't
wait to be asked to consider it. But never edit this file without explicit
confirmation first, same as any other commit in this repo. Don't use it as
a changelog - individual bug fixes and features belong in commit messages
and `git log`, not here.
