---
name: pre-push
description: The checks before a git push to main, which deploys to production and may restart the live bot - what goes out, whether the deploy restarts the bot, which games are on or about to start, whether a move-only commit really only moved lines, server details or issue numbers in the outgoing diff, lint and the full suite - ending in a go / no-go for Heikki to confirm. Use whenever a push (or merging a PR to main) is about to happen, when Heikki types /pre-push, or asks whether it is safe to push now.
---

# Pre-push check

A push to `main` is a production deploy (CLAUDE.md, "Deployment"): when it changes anything the
bot runs, the bot restarts, leaves and rejoins every channel and stops every running tracker. This
skill never pushes by itself. It ends with a verdict, and the push happens only after Heikki
confirms it in his reply to that verdict - an earlier "push it" does not cover a no-go.

## 1. Run the checks

    .venv/bin/python tools/pre_push.py

It fetches `origin/main` and only reads: the open pull requests (`gh pr list`), the outgoing
commits, the restart decision (the repo's own `.github/needs-restart.sh`, against the commit the
last successful deploy's log says the bot runs),
today's games for every tracker (each tracker's own `_fetch_today_items`), a move check for every
commit whose subject says "move only", everything that goes out (added lines, the commits' full
messages, the changed file paths) scanned for server details (values from the gitignored
`tools/fetch_night.conf`, plus IP addresses), and the added lines scanned for issue numbers in CLAUDE.md.
Exit 1 means it found something to look at; read the whole output either way.

Then run lint and the full suite on what is about to go out (CI would refuse a failing push, but
only after the push has been made):

    .venv/bin/ruff check src tests tools
    .venv/bin/pytest -q

## 2. Read the results

- **Open pull requests** - always mention each one, even when there is nothing to push (CLAUDE.md:
  merging one is a push). "could not be listed" means check `gh pr list` by hand.
- **Nothing to push** - say so (and any open pull requests) and stop.
- **Restart: no** - games do not matter: the server only pulls the files. Still a production
  action, so still ask.
- **Restart: YES** with a game `live` or `soon` - **no-go**. Say when the slate is over (the last
  game's start plus its hours: NHL 4, Liiga 3.5, pesis 3) and offer to wait. Liiga and NHL start
  themselves (`AUTO_START`), so "nobody started the tracker" is no exemption. Pesis trackers are
  started by hand; a `soon` pesis match may already be tracked.
- **Restart: YES**, games only `over` or `later` - fine to push. Mention the next start, and that
  the server's own security updates restart the bot around 06:00-07:00 UTC anyway.
- **"the bot's commit: -"** (no readable deploy log) - the script asks `needs-restart.sh` with no
  commit, which always says restart; treat it as a restart.
- **A schedule not readable** - say which; with a restart, no-go until it reads or Heikki decides.
- **Move check** - a commit that claims to be move-only may differ only in module docstrings,
  imports and class lines. Any other changed line (a method body, a message) means it is not
  move-only: report the lines and do not push it under that subject.
- **Server details** - no-go. Name the file and line, or the commit whose message carries it (the
  script does not print the value); fix the commit (`git commit --amend`, or reword it before the
  push), never push it "to clean up later": the repo is public and a push is permanent.
- **Issue numbers in CLAUDE.md** - fine only where they explain a why that stays true; otherwise
  rewrite in terms of modules and behaviours before pushing.
- **Lint or tests failing** - no-go; fix first.

## 3. Report

A few lines, then wait:

    Open pull requests: #91 Bump ruff (Dependabot, 2 d old).
    Push: 2 commits (137a1cf split of the NHL GOAL line, 6db924a CLAUDE.md).
    Restart: YES (src/nhl_command.py, src/nhl_goal_format.py).
    Games: NHL live now (20:30 UTC), 2 at 23:00, last at 03:00 -> clear from about 07:00 UTC.
    Move check 137a1cf: only headers, imports and the class line changed.
    Server details: none. Lint and 1803 tests: pass.
    -> No-go now. Push after 07:00 UTC?

After a confirmed push, follow the deploy (`gh run watch` on the CI/CD run) and report the restart
line from its log (`restart: ...` or `no restart needed: ...`). A push that changed a live tracker
still needs its first real game checked; say which.
