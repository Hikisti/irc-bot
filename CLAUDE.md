# Working on KukistiBot with Claude

This file is for Claude (or anyone else picking this project up cold). See
`README.md` for what the bot does and how to run it, and `DEPLOY.md`
(gitignored, not in this repo's history - ask Heikki for it) for actual
server access details. This file is about *how* to work on this codebase,
not what it is.

## Architecture at a glance

- `BaseCommand` (`src/base_command.py`) is the contract every command
  implements: `ALIASES`, `ALLOW_ARGS`, `CHANNELS`, `execute(args, irc_bot=,
  channel=)`. `CommandHandler` builds its alias-dispatch table from these at
  startup - a command with no `ALIASES` is unreachable.
- `LiveTrackerCommand` (`src/live_tracker_command.py`) is the shared engine
  behind `LiigaCommand` and `PesisCommand` - identical start/stop/next
  lifecycle, poll loop, early-start guard, and already-finished guard. Look
  here first before touching either subclass's lifecycle code; only the
  actual fetch/announce logic (`_poll_once`, `_fetch_today_items`,
  `_build_initial_state`) is sport-specific.
- `PesisCommand` is built from three mixins: `PesisEventParsingMixin`
  (turns the raw event feed into RUN:/JAKSO:/FINAL: text),
  `PesisPlayerNamesMixin` (scorer/batter name resolution), and
  `PesisDataFetchingMixin` (all pesistulokset.fi HTTP calls).
  `SuperpesisCommand`/`YkkospesisCommand` are thin subclasses of it.
- `liiga.fi` and `pesistulokset.fi` are **unofficial, reverse-engineered
  APIs** - not documented, can change shape without notice. This is why the
  code leans defensive throughout (never crash on an unexpected field,
  degrade to an error string instead) - keep that posture in any new code
  touching these APIs, don't tighten assumptions "for cleanliness."

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
- Never silently change observable behavior (a message's wording, a
  command's output shape) without calling it out first.
- Don't add features, refactor, or add abstractions beyond what a task
  actually needs. Comments only when the *why* is genuinely non-obvious
  (a hidden constraint, a workaround for a confirmed live incident) - not
  for what the code already says by being well-named.
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
