# Working on KukistiBot with Claude

This file is about *how* to work on this codebase. `README.md` says what the
bot does and how to run it; `DEPLOY.md` (gitignored, in no repository - ask
Heikki for it) holds the server access details and runbook.

> **`git push` to `main` deploys to production and can restart the live bot**
> (see "Deployment" below). Never push, or merge a PR, without Heikki's
> confirmation. Commit only when asked.

## Commands

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
    .venv/bin/ruff check src tests tools          # lint; CI fails on any finding
    .venv/bin/pytest -q                           # full suite
    .venv/bin/pytest tests/test_whatever.py -q    # one file
    .venv/bin/pytest --cov=src --cov-report=term-missing

- Python 3.14 everywhere (local venv, CI, production); keep them in sync if
  the venv is rebuilt (`DEPLOY.md`, "rebuilding the venv", has the incident).
- `ruff.toml` checks for real mistakes (unused or undefined names, bugbear),
  not formatting. There is no auto-formatter on purpose, to keep `git blame`
  meaningful.
- Tests follow the `src/` modules under `tests/` (the Liiga goal ledger's are
  still in `tests/test_liiga_command.py`). Shared builders: `tests/conftest.py`
  (`make_json_response`, the `nhl_command` fixture), `tests/nhl_test_helpers.py`,
  `tests/pesis_test_helpers.py`. No test touches the network.

## Architecture

- **Commands.** `BaseCommand` (`src/base_command.py`) is the contract:
  `ALIASES`, `ALLOW_ARGS`, `CHANNEL_ONLY`, `HELP`, `execute(args, irc_bot=,
  channel=)`. `CommandHandler` dispatches on `ALIASES`, so a command without
  them can't be reached.
- **Channels.** `channels.toml` lists the channels and the commands each one
  allows. `src/channel_config.py` checks it at startup, and a mistake in it
  stops the bot with a message naming it. Each channel gets every command that
  isn't `CHANNEL_ONLY` and can add, remove or replace that set.
  `CommandHandler.is_allowed()` and `url_titles_allowed()` are the only places
  that read it. The live trackers are `CHANNEL_ONLY`, so a new tracker works
  nowhere until a channel adds it.
- **Live trackers.** `LiveTrackerCommand` (`src/live_tracker_command.py`) is
  the engine behind `LiigaCommand`, `PesisCommand` and `NHLCommand`. It owns
  the start/stop/next lifecycle, the poll loop, and the early-start and
  already-finished guards, so look there first. Each sport supplies
  `_poll_once`, `_fetch_today_items` and `_build_initial_state`. Each sport
  command is built from mixins, one module per job:
  - NHL: `nhl_goal_ledger.py` (what has been announced, `NO GOAL:`
    retractions with a reason), `nhl_goal_format.py` (the GOAL line, its
    PP/SH/EN tags, the shootout tally, names) and `nhl_final.py` (the FINAL
    line, attendance, the end-of-slate list). A game's FINAL is posted by the *second* poll in
    which it is final (#70), so a test that ends a game must poll twice or
    seed `"final_seen": ()` in the previous state.
  - Liiga: `liiga_goal_ledger.py`. Every announced goal is kept under each
    identity it has had: event id; period, game time and scorer; scorer and
    running score. A goal that stays out of the feed for four polls is
    retracted with `NO GOAL:`, without a reason, because the feed gives none.
  - Pesis: event parsing, player names and data fetching mixins;
    `SuperpesisCommand`/`YkkospesisCommand` are thin subclasses.
  - Shared: `TeamNextMixin` (`src/team_next.py`), `next <team> [count]` for
    NHL and Liiga.
- **Auto start.** `AutoStarter` (`src/auto_start.py`) starts each `AUTO_START`
  tracker (Liiga, NHL) at the scheduled start of the day's first game, in
  every channel that has it and hasn't started it. It goes through the same
  `start` path and guards, at most once a day, and never after a `stop` that
  day. The decision is the plain function `due()`.
- **Scoreboards.** The one-shot `now`/`results` lookups never touch tracker
  state. The parts shared by both sports are in `ScoreboardMixin`
  (`src/scoreboard.py`), and each sport supplies its own fetch and labels.
  `nhl_scoreboard.py` reads `/score/{date}`, which has period and clock that
  the schedule lacks. `liiga_scoreboard.py` reads the tracker's games list. A
  subcommand other than start/stop/next goes in `SUBCOMMANDS`, and in an
  `execute()` placed ahead of `LiveTrackerCommand`'s in the MRO.
- **The feeds are unofficial, reverse-engineered APIs** (`liiga.fi`,
  `pesistulokset.fi`, `api-web.nhle.com`) and can change shape without notice.
  Stay defensive: never crash on an unexpected field, degrade to an error
  string instead, and don't tighten assumptions "for cleanliness". For a new
  feed, find out which timezone it buckets its dates by. NHL dates are US
  Eastern (`EASTERN_TZ`, confirmed live), while every displayed time is
  `HELSINKI_TZ`.
- **`tools/`** holds read-only probes and reports, not the bot. Nothing in
  `src/` imports them, and only `bot_watch.py` talks to IRC: to the network,
  never to a channel (see `tools/README.md`). They
  reuse tracker internals, so renaming one of the following means updating
  `tools/`: `_fetch_scores`, `_fetch_play_by_play`, `_current_season`,
  `_fetch_today_games`, `_fetch_day`, `_parse_start_dt`, `START_TIME_KEY`, the
  trackers' timezones, the probes' command-line arguments.
  - `journal_report.py` parses the bot's own message wording (GOAL:/NO
    GOAL:/FINAL:, the `NHL:`/`Liiga:` prints). Changing that wording or adding
    a journal print means updating it.
  - `feed_contract.py`'s field lists mirror what the trackers read, so a
    tracker that starts reading a new field adds it there.

## Concurrent fetching

Use `LiveTrackerCommand._fetch_concurrently(items, fetch_fn)`, which returns
`{item: result_or_None}`, for any set of independent per-item network calls.
Don't write `ThreadPoolExecutor`/`executor.map()` yourself. A bare `map()`
raises the first failing future's exception as soon as it is reached and
loses every other item's result. Both the NHL play-by-play fetch and Pesis
`_seed_match_extras` had that bug live, independently. `_fetch_concurrently()`
gets each future's result or exception separately.

## Adding a command

1. Subclass `BaseCommand` (or `LiveTrackerCommand`) and add it to
   `COMMAND_CLASSES` in `src/command_handler.py`. That is the only
   registration step. The exception is `HelpCommand`, which needs the finished
   handler and is registered in `CommandHandler.__init__`.
2. Set `HELP`, the usage string `!help` lists (e.g. `"!stock <ticker>"`). A
   test fails if it is missing.
3. If the command takes arguments, calling it bare must reply `Usage: ...
   (e.g., ...)`. A test enforces this.
4. Add tests, plus a row in the README commands table (and a paragraph if the
   behaviour isn't obvious). `tests/test_readme.py` checks that table and the
   README's Channels section against the code and `channels.toml`. A new
   channel goes in `channels.toml` and that table, and channel names appear
   nowhere else in the README.

## How new code should look here

- **Use the existing seams before inventing one**: a `BaseCommand`, a
  `LiveTrackerCommand` hook, or a mixin for behaviour shared across sports.
  Flexibility means a feature fits a seam, not code carrying options nobody
  asked for.
- **One module, one responsibility.** Past about 600 lines, or at the third
  copy of a pattern, propose a split (see Refactoring below).
- **Keep decisions apart from input and output.** The rule that decides (same
  goal? which team matched?) is a plain function tests can call without a
  network. Fetching and sending wrap around it.
- **The code explains itself.** A comment says why (a hidden constraint, a
  workaround for a confirmed live incident), never what.
- No features, refactors or abstractions beyond what the task needs.
- Never silently change observable behaviour (a message's wording, a
  command's output shape); call it out first.

## Methodology on every change

- After editing a file, run *that file's* tests right away, not only at the end.
- After a mutation check (break the code, run the tests, restore), clear the
  `__pycache__` folders. Python judges a compiled file current by source mtime
  (to the second) and size. A same-second, same-length restore keeps the stale
  copy, and a correct change then looks broken. This once hid a correct
  `POLL_INTERVAL_SECONDS = 45`.
- A test that loops on a fake clock or socket needs a call limit or a
  deadline, so a regression fails instead of hanging the suite.
- Tests must not depend on `.env` (CI has none). `tests/conftest.py` sets
  `PYTHON_DOTENV_DISABLED`, so a test that needs a key sets it with
  `monkeypatch.setenv`.
- **Before calling a task done:** run the full suite, check coverage on the
  touched modules hasn't dropped, and where feasible verify against the live
  service (a real `!stock`/`!liiga`/`!superpesis` call). Several real bugs
  were only caught that way.
- For a live-tracker change, start the matching probe from `tools/` before
  the games, compare its log with the channel log afterwards, and put the
  evidence (counts, short timelines) in the issue. Probes and their output
  carry no identifying details, and `*.log` stays ignored.
- **Security audit** for changes to code that handles untrusted input (user
  arguments that reach an outside service, link titles, IRC message handling,
  `send_message`), to the deploy pipeline, or for a major upgrade of the HTTP
  libraries or Python. Use the `security-audit` skill in quick profile,
  limited to the changed files, about 15 agents at most, on a day without
  tracked games. Reports stay outside the repository; keep the ledger for the
  next run. Fix a confirmed problem before any public issue describes it;
  leads that can't be fixed now go in issues without step-by-step detail.
- **Refactoring is triggered, not scheduled.** Propose it (never fold it into
  a feature or fix) at a third copy of a pattern or a file past about 600
  lines. It must not change behaviour, goes in its own move-only commit, and
  never lands right before a live-tracker change is verified against real
  games.

## GitHub issues

The repo is public. Nothing on GitHub (issues, comments, commits, PRs,
tracked files) ever holds server details: IP addresses, hostnames, logins,
paths, the service name, keys, or anything from `DEPLOY.md`. Quote journal
and log excerpts only after stripping host prefixes, user addresses and
chat.

- Anything not being fixed now (a quirk, a deferred option, a parked
  decision) goes in an issue, not in code comments, memory or a TODO file.
  Creating or closing an issue is outward-facing: confirm first.
- Each new issue gets one `priority: high|medium|low` label (high = a visible
  error that has already happened in a channel) and one area label (`nhl`,
  `liiga`, `pesis`, `imdb`; none if nothing fits). The pinned tracking issue
  holds the order of work; update its checklist when a priority changes.
- A description says where the issue stands *now*. When its state changes
  (fixed, deployed, seen live, parked, decided), rewrite its `## Status`
  section in the same session. A comment is not enough. Also update its line
  in the tracking issue: tick it on close and drop it from any waiting list.
- Before starting on issue N, run `git log --grep="#N"`. A stale description
  once made finished work look undone.
- Notes worked out in a session about an issue (an analysis, research, a
  plan, measured data, a decision's reasons) go into that issue in the same
  session, as a comment or in its description, without waiting to be asked.
  Say in the reply what was posted. Notes left only in the chat are lost when
  the session ends.
- At the start of a session, and before any push, run `gh pr list` and
  mention every open pull request (most are Dependabot bumps). Merging one is
  a push: it deploys and can restart the bot.

## Deployment: `git push` to `main` is a production action

Every push to `main` runs the tests, then deploys over SSH to the production
server. If the push changes anything the bot runs (`src/`, `channels.toml`,
`requirements.txt`, or any file not known to be safe), **the live bot
restarts**: it leaves and rejoins every channel and stops every running
tracker, with real users present. A push of only docs, tests, tools,
`.github/`, `.claude/` or the lint and test settings is pulled without a
restart.
`.github/needs-restart.sh` decides, and the deploy log says which happened.
Only add a path to its safe list if the bot never reads it.

- Before any push, run `/pre-push`; its verdict is what Heikki confirms.
  "Implement this" is not "push this."
- Don't push a live-tracker change (`!liiga`/`!nhl`/`!superpesis`/
  `!ykkospesis`) while a game is being tracked. Ask, or wait until the day's
  games are over.
- Merging a PR (a Dependabot bump, say) is a push. The server's automatic
  security updates can also restart the bot around 06:00-07:00 UTC; a tracker
  that stops then with no other cause just needs starting again.
- Never `git add` `DEPLOY.md` or suggest committing it, even when asked to
  "just add everything".
- A change that adds or alters anything on the server (a systemd unit or
  timer, a package, a sudoers rule, a script in a home directory, a GitHub
  secret the workflow reads) updates `DEPLOY.md`'s runbook in the same
  session. Claude has no server access, so it drafts the steps and Heikki runs
  them.
- `DEPLOY.md` in the checkout is a symlink to the real file in iCloud Drive,
  so the copy off this Mac stays current. The edit tools refuse to write
  through a symlink, so edit the target path. Afterwards `ls -l DEPLOY.md`
  must still show `->`; a plain file there means iCloud no longer gets the
  changes.

## Commit conventions

Messages explain the *why*. For a bug fix, give the real symptom, the root
cause (often confirmed against the live API) and why the fix addresses that
cause rather than the symptom. Match the tone and length of recent `git log`.

## Keeping this file current

When a standing rule is established (a correction meant to apply beyond its
task) or the architecture changes so a section here goes stale, propose the
update without waiting to be asked. Never edit this file without explicit
confirmation. It is not a changelog.

Refer to modules and behaviours, not to issue numbers that will close. A
number goes stale the day its issue closes. An issue number is fine only
where it explains a why that stays true.
