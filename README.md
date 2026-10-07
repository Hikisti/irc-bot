# irc-bot

A simple IRC bot (KukistiBot) with a handful of chat commands: weather, stock, crypto,
electricity price, local time, F1 schedule, driving distance, IMDb lookups, and live Liiga (ice
hockey), NHL, and Superpesis (pesäpallo) score tracking. It also auto-fetches and posts page titles
for links shared in channel.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the project root with your API keys:

```
WEATHER_API_KEY=your_weatherapi_com_key
TIME_API_KEY=your_ipgeolocation_io_key
ORS_API_KEY=your_openrouteservice_org_key
OMDB_API_KEY=your_omdbapi_com_key
```

- `WEATHER_API_KEY` — from [weatherapi.com](https://www.weatherapi.com/), needed for `!weather`;
  if unset, `!weather` replies with an error but the rest of the bot still starts and works fine.
- `TIME_API_KEY` — from [ipgeolocation.io](https://ipgeolocation.io/), optional; only needed
  for `!time <city>` lookups (timezone abbreviation lookups like `!time cdt` work without it).
- `ORS_API_KEY` — from [openrouteservice.org](https://openrouteservice.org/dev/#/signup) (free,
  no credit card required as of writing), needed for `!distance`; if unset, `!distance` replies
  with an error but the rest of the bot still starts and works fine.
- `OMDB_API_KEY` — from [omdbapi.com](https://www.omdbapi.com/apikey.aspx) (free tier: 1,000
  requests/day, no credit card required), needed for `!imdb`; if unset, `!imdb` replies with an
  error but the rest of the bot still starts and works fine. OMDb's free tier is licensed
  CC BY-NC 4.0 (non-commercial use only).

None of these keys are required for the bot to start — each missing key only disables the one
command that needs it.

Stock, crypto, and electricity price commands use free public APIs and don't need a key.

## Running

The bot's modules use imports relative to `src/`, so run it from inside that directory:

```bash
cd src
python irc_bot.py            # joins the default production channels
python irc_bot.py --debug    # joins only #bottest123, for local testing
```

By default it connects to `irc.quakenet.org`. Server, port, nickname, and channels are set
in `src/irc_bot.py`.

## Commands

| Command | Aliases | Args | Example |
|---|---|---|---|
| Weather | `!weather`, `!w` | city, or `city,country` | `!weather austin` |
| Stock | `!stock` | ticker symbol | `!stock TSLA` |
| Crypto | `!crypto` | coin name | `!crypto bitcoin` |
| Electricity price | `!sähkö`, `!sahko` | none | `!sähkö` |
| Time | `!time` | city, or timezone abbreviation | `!time austin`, `!time cdt` |
| F1 schedule | `!f1` | none | `!f1` |
| Liiga live tracker | `!liiga` | `start`, `stop`, or `next` | `!liiga start` |
| Distance | `!distance` | `city1,city2` (or two single-word cities) | `!distance Kokkola,Vimpeli` |
| IMDb | `!imdb` | movie/show title, optionally with `series`/`movie` first and/or a year last; or an IMDb ID/link | `!imdb terminator 2`, `!imdb series flipper 1995` |
| NHL live tracker | `!nhl` | `start`, `stop`, `next`, `now`, or `results` | `!nhl now` |
| Superpesis live tracker | `!superpesis` | `start`, `stop`, or `next` | `!superpesis start` |
| Ykköspesis live tracker | `!ykkospesis` | `start`, `stop`, or `next` | `!ykkospesis start` |
| Björck | `!bjorck` | none | `!bjorck` |
| Help | `!help` | none | `!help` |

`!help` lists the commands usable in the channel it's asked in, on one line (channel-restricted ones
like `!nhl` only show up in their own channel). Any command used without its required argument
replies with a `Usage:` line (most include an example), and the no-argument commands (`!f1`,
`!sähkö`, `!bjorck`) answer extra text with `Usage: !f1 (takes no arguments)` rather than staying
silent.

`!liiga start`/`!superpesis start`/`!ykkospesis start` all refuse to begin tracking more than 15
minutes before the earliest scheduled game/match that day, e.g. `Too early to track — Liiga play
starts at 17:00. You can run !liiga start again from 16:45 onward.` — avoids burning API calls and
poll cycles hours before anything's actually happening. They also recognize when today's slate
exists but every game/match on it is already finished (e.g. run late in the evening), replying with
a single `All of today's Liiga games have already finished.` instead of announcing "Tracking N
games" and then immediately "all finished, stopped" a moment later.

For `!liiga start` and `!nhl start`, the "Tracking N games" reply also shows the current score of
any game already underway, e.g. `Tracking 3 NHL game(s) today: 00:00 CAR 0-1 FLA (final) | 02:00
TOR 1-1 MTL | 05:00 EDM-VAN` (`(final)` once it has ended, plain names for games not started yet).
Goals scored before a start are never announced - they're the baseline - so this is how a mid-game
start shows what already happened. `!liiga next`/`!nhl next` keep the plain list, and
`!superpesis`/`!ykkospesis` don't show scores yet (pesäpallo's per-jakso scoring needs its own
format, and its not-yet-started data shape hasn't been seen live).

`!liiga start` polls today's Finnish Liiga (ice hockey) games every 30s in the channel it was
started in, and announces goals and final scores as they happen. It stops automatically once
all of today's games have ended, or on `!liiga stop`. `!liiga next` looks up the next upcoming
gameday (today if there's still something scheduled and not already finished, otherwise the next
date with games) and lists its matchups grouped by start time, e.g. `Next Liiga gameday
(tomorrow): 18:30 TPS-Jokerit, Pelicans-KooKoo`. A `FINAL:` line gets an `(OT)` or `(SO)` suffix
when the game was decided in overtime or a shootout, plus the attendance figure when the API
reports one, e.g. `FINAL: Sport 5-4 Jokerit (SO) | Yleisöä: 3532`. Uses the unofficial liiga.fi
JSON API, so no key is needed but the endpoint isn't guaranteed to stay stable. **Only works in
`#smliiga` and `#veikkaus`** — like `!superpesis`, it's restricted to those channels
(see `CHANNELS` on the command class); typing it elsewhere is silently ignored. `#veikkaus`
is an *exclusive* channel: only `!liiga`, `!nhl` and `!help` work there (`!help` lists just the first two), and posted links get no title reply either.
Such channels are set in `CommandHandler.EXCLUSIVE_CHANNELS`; every other channel keeps the
default of all commands.

`!distance` looks up driving distance and drive time between two cities anywhere in the world,
via OpenRouteService. City names need a comma between them (`!distance New York, Los Angeles`)
unless both are a single word, in which case a space works too (`!distance Kokkola Vimpeli`) —
this avoids silently misreading a multi-word city name as the wrong split.

`!imdb` looks up a movie or show by title via the OMDb API (a third-party service re-publishing
IMDb's data - IMDb's own official API is an enterprise-only AWS Data Exchange product, not viable
for a free personal bot) and replies with its year and type, IMDb rating, a short plot summary,
and its IMDb URL, e.g. `Terminator 2: Judgment Day (1991, movie) - IMDb: 8.6/10 - A cyborg from
the future... - https://www.imdb.com/title/tt0103064/`. An unreleased/unrated title shows "not yet
rated" instead of a score. A bare title returns OMDb's single best match (which tends to prefer
movies), so an ambiguous title can be narrowed down: a leading `series`/`tv`/`movie` picks the type
(`!imdb series flipper`), a trailing year picks the release or start year (`!imdb flipper 1995`),
and the two combine (`!imdb series flipper 1995`). To be exact, paste an IMDb ID or an imdb.com
link (`!imdb tt0111964`). If a year or type hint finds nothing, the whole text is retried as a
plain title, so titles like "Blade Runner 2049" still work. The one known miss: a title that itself
starts with `movie`/`series`/`tv` and whose remainder is also a title (e.g. "Movie 43") - use the
IMDb ID for those.

`!nhl start` polls today's NHL games every 30s in the channel it was started in (same lifecycle,
early-start guard, and already-finished guard as `!liiga start`), announcing goals and final scores
as they happen, e.g. `GOAL: Carolina Hurricanes 1-0 Florida Panthers 04:31 1st | Carolina
Hurricanes — Bradly Nadeau (assists: Mike Reilly)` and `FINAL: Carolina Hurricanes 3-2 Florida
Panthers (SO) | Yleisöä: 19250`. A goal scored in a special situation is tagged after the scorer -
`(PP)` power play, `(SH)` shorthanded, `(EN)` into an empty net, combined like `(SH/EN)` - and
shootout goals get none. The tag is decoded from the goal's on-ice situation code, so it costs no
extra request, and was checked against NHL's own labels on 250 real goals with no mismatches; a
team scoring with its own goalie pulled at even strength gets no tag, matching NHL, and penalty
shots aren't distinguishable in this feed. The attendance isn't in NHL's JSON API at all, so it's
read from NHL's HTML game-summary report with one extra request when a game ends, and simply left
off if that report isn't available yet or NHL changes the page. The report's figure often appears only
1.5-3 minutes after the final horn (about two games in three), so a missing figure is looked up again every
poll for seven minutes, and when the last game is over one `NHL results D.M.: DET 2-3 WPG (18347), ...` list
with every figure follows - but only if some figure arrived after its `FINAL:` line - before the "all
finished" message, which waits up to those seven minutes. Each goal is remembered by its team
and running score (with its event id as a second check), not by a count per team, because the feed
was seen listing one goal twice under different ids and dropping a goal for a poll and bringing it
back: neither is announced a second time, and the next goal after a disallowed one (same running score)
still is. A goal that was announced and then stays out of the feed for four polls in a row (about two
minutes; a feed flicker is shorter) is retracted with a `NO GOAL:` line giving the score as the remaining
goals show it, e.g. `NO GOAL: Pittsburgh Penguins 5-3 Montréal Canadiens 05:25 3rd | Egor Chinakhov (Pittsburgh
Penguins) was disallowed (offside challenge)` (clock and period right after the score, as in the
`GOAL:` line). The reason comes from the
coach's-challenge stoppage the feed records near the goal (offside, goaltender interference, or just
"challenge"); a challenge that failed, which is followed by a bench penalty, is not taken as a reason.
If a new goal takes the running score of a goal that has only just left the feed (seen when the feed corrects a wrong
first scorer: the first entry disappears and a new one appears seconds later), nothing is retracted: the new goal is
announced and the first line stays, since the goal was not disallowed. Goals that were already in the feed when
tracking started are never retracted. A feed with no plays at
all, or with no goals while two or more announced ones are missing at once, looks like a broken response
and is not counted as a miss. The scorer or assists
in a line can still be early guesses. `!nhl next` looks up the next
upcoming NHL gameday and lists its matchups grouped
by start time using team abbreviations (`CAR-FLA`), e.g. `Next NHL gameday (Wed 30/09): 00:00
CAR-FLA | 02:00 TOR-MTL`. Uses `api-web.nhle.com`, the NHL's own public web API (the same one that
powers the NHL's official site) - undocumented and unofficial, so it isn't guaranteed to stay
stable, same caveat as liiga.fi/pesistulokset.fi. Note the times shown are always Helsinki-local,
but the API's own notion of "today" is anchored to US Eastern time - most NHL games actually fall
into the small hours of the following Helsinki calendar day, which is why the gameday label above
is computed from the games' own Helsinki-converted times rather than the API's own (Eastern) date,
and can end up showing a date further out than "tomorrow" even for the very next NHL gameday.
Because Eastern midnight falls in the middle of the late games, a tracker keeps following the
schedule day it started on (not "today" at each poll) until those games finish, and `!nhl start`
run right after Eastern midnight also picks up a game still in progress from the previous day.
`!nhl now` and `!nhl results` are one-shot looks that don't start any tracking (and work while a
tracker is running). `!nhl now` is today's board: `Live:` games with score, period and time left
(`TOR 2-1 NYI 2nd 12:34 left`, `1st int.`, `OT`, `SO`; `1st end` for the moment a period's clock is at
00:00 and stopped, and `over` for the short state between the end of play and the final), then `Final:`
and `Upcoming:` (Helsinki start times; only games that have not started, or will not be played). A game in
a state the code has never seen is listed under `Live:` with that state in brackets, never as upcoming.
Empty groups are left out, and with no games at all it points at the next gameday.
`!nhl results` lists only the finished games of the latest slate that has any (`NHL results 1.10.: PHI
0-7 PIT, TOR 2-1 NYI (OT)`, dated as Helsinki sees it) and adds `(N game(s) still on: !nhl now)` when
games of that slate are still being played. Both read `api-web.nhle.com`'s `/score/{date}` (Eastern
dates, briefly cached) and split long output into several lines.

**Only works in `#nhl.fi` and `#veikkaus`** - like `!superpesis`, it's restricted to those channels; typing it
elsewhere is silently ignored.

`!superpesis start` polls today's Miesten Superpesis (pesäpallo, men's top division) matches
every 30s and announces runs and final results as they happen, the same way `!liiga start` does
for hockey — stops automatically once all of today's matches have finished, or on
`!superpesis stop`. `!superpesis next` looks up the next upcoming matchday (today if there's
still something scheduled and not already finished, otherwise the next date with matches,
searched day by day up to 21 days ahead) and lists it the same way `!liiga next` does. **Only
works in `#pesis.fi`** — unlike
every other command, it's restricted to that one channel (see `"channels"` in
`command_handler.py`); typing it elsewhere is silently ignored. Uses pesistulokset.fi's
unofficial JSON API. Each run announcement names the lyöjä (batter) and etenijä (scorer), in
that order matching pesistulokset.fi's own column order, when they're different people, plus the
jakso and vuoropari (batting turn, e.g. "3. lopettava") it happened in, e.g.
`RUN: Joensuun Maila — Joosua Rättö → Konsta Piironen | Joensuun Maila 3-2 Sotkamon Jymy (2.
jakso, 3. lopettava)`; period transitions (jakso 1/2, supervuoro, kotiutuslyöntikilpailu) get
their own `JAKSO:` announcement. The score shown in `RUN:`/`JAKSO:` lines is scoped to the
current jakso only (pesäpallo scores each period independently, not as a running match total).
`FINAL:` reports the whole match in the real pesäpallo convention — jaksovoitot (periods won) as
the headline, per-period run breakdown in parens, e.g. `FINAL: Joensuun Maila - Sotkamon Jymy
1 - 0 (4 - 2, 2 - 2)`. A match stops being touched entirely once it's finished, so a
provider-side correction to the event feed after the fact can't produce more messages for it.
Individual run announcements are best-effort (pesäpallo's scoring vocabulary — regular hits, wild
throws, home runs, tie-break rounds — wasn't necessarily fully catalogued, so an unseen pattern
could still be missed as a chat line, and the provider has been observed retracting and
reissuing a play mid-game with different details) — the running score in `RUN:`/`JAKSO:` lines
never shows more than the provider's own authoritative period total confirms, so a missed chat
line means that score temporarily trails by the same one run rather than ever overstating it.
`FINAL:` is always fully accurate regardless, since it's read straight from the API's own
authoritative live-result summary rather than computed locally.

`!ykkospesis` tracks Miesten Ykköspesis (pesäpallo's second division) exactly the same way —
same commands, same message formats, same `#pesis.fi` restriction — since both share one
implementation (`src/pesis_command.py`'s `PesisCommand`, with `SuperpesisCommand`/
`YkkospesisCommand` as thin per-league subclasses); a fix to one fixes both. Both trackers can
run at once, including in the same channel, without interfering with each other.

The bot also watches every message for `http(s)://` links and replies with the page title,
unless the domain is blacklisted (see `BLACKLISTED_DOMAINS` in `src/url_fetcher.py`).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

All tests mock outgoing network calls, so no API keys are required to run the suite. A local `.env`
is deliberately ignored while the tests run (see `tests/conftest.py`), so the suite behaves the
same on a developer machine as in CI.

## Probing the live feeds

`tools/` holds small **read-only** scripts for watching the NHL and Liiga feeds while real games are
on: `goal_probe.py` (NHL goals: changes, disappearances, a goal listed twice, header vs plays),
`state_watch.py` (NHL game states, including the short `OVER` state) and `liiga_sampler.py` (the Liiga
feed going backwards, cache headers, when `ended` and the attendance appear). They are not part of the
bot and never talk to IRC; they exist so a tracker change can be compared with what the feed really did
on a live night. Run them from the repository root, for example

```bash
.venv/bin/python tools/goal_probe.py 2026-10-04 14      # NHL slate by US-Eastern date, run up to 14 h
```

Logs go to `tools/logs/` (git-ignored). See [tools/README.md](tools/README.md) for each script.

## Project structure

```
src/     - bot and command implementations
tests/   - pytest test suite (mirrors src/, one test file per module; test_tools.py covers tools/)
tools/   - read-only probes of the live NHL/Liiga feeds (not part of the bot)
```

## Deployment

`.github/workflows/ci-cd.yml` runs the test suite on every push and pull
request. A push to `main` (or a manual run from the Actions tab) also
deploys: it SSHes into the production server as a dedicated, unprivileged
deploy user, pulls the latest `main`, updates dependencies, and restarts
the bot's service — that user's `sudo` access is scoped to just restarting
and checking the one service, nothing more. Server-specific setup details
aren't kept in this repo.
