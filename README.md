# irc-bot

A simple IRC bot (KukistiBot) with a handful of chat commands: weather, stock, crypto,
electricity price, local time, F1 schedule, driving distance, IMDb lookups, and live Liiga (ice
hockey) and Superpesis (pesäpallo) score tracking. It also auto-fetches and posts page titles for
links shared in channel.

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
| IMDb | `!imdb` | movie/show title | `!imdb terminator 2` |
| Superpesis live tracker | `!superpesis` | `start`, `stop`, or `next` | `!superpesis start` |
| Ykköspesis live tracker | `!ykkospesis` | `start`, `stop`, or `next` | `!ykkospesis start` |
| Björck | `!bjorck` | none | `!bjorck` |

`!liiga start`/`!superpesis start`/`!ykkospesis start` all refuse to begin tracking more than 15
minutes before the earliest scheduled game/match that day, e.g. `Too early to track — Liiga play
starts at 17:00. You can run !liiga start again from 16:45 onward.` — avoids burning API calls and
poll cycles hours before anything's actually happening. They also recognize when today's slate
exists but every game/match on it is already finished (e.g. run late in the evening), replying with
a single `All of today's Liiga games have already finished.` instead of announcing "Tracking N
games" and then immediately "all finished, stopped" a moment later.

`!liiga start` polls today's Finnish Liiga (ice hockey) games every 30s in the channel it was
started in, and announces goals and final scores as they happen. It stops automatically once
all of today's games have ended, or on `!liiga stop`. `!liiga next` looks up the next upcoming
gameday (today if there's still something scheduled and not already finished, otherwise the next
date with games) and lists its matchups grouped by start time, e.g. `Next Liiga gameday
(tomorrow): 18:30 TPS-Jokerit, Pelicans-KooKoo`. A `FINAL:` line gets an `(OT)` or `(SO)` suffix
when the game was decided in overtime or a shootout, plus the attendance figure when the API
reports one, e.g. `FINAL: Sport 5-4 Jokerit (SO) | Yleisöä: 3532`. Uses the unofficial liiga.fi
JSON API, so no key is needed but the endpoint isn't guaranteed to stay stable. **Only works in
`#smliiga`** — like `!superpesis`, it's restricted to that one channel
(see `"channels"` in `command_handler.py`); typing it elsewhere is silently ignored.

`!distance` looks up driving distance and drive time between two cities anywhere in the world,
via OpenRouteService. City names need a comma between them (`!distance New York, Los Angeles`)
unless both are a single word, in which case a space works too (`!distance Kokkola Vimpeli`) —
this avoids silently misreading a multi-word city name as the wrong split.

`!imdb` looks up a movie or show by title via the OMDb API (a third-party service re-publishing
IMDb's data - IMDb's own official API is an enterprise-only AWS Data Exchange product, not viable
for a free personal bot) and replies with its year, IMDb rating, a short plot summary, and its
IMDb URL, e.g. `Terminator 2: Judgment Day (1991) - IMDb: 8.6/10 - A cyborg from the future...
- https://www.imdb.com/title/tt0103064/`. An unreleased/unrated title shows "not yet rated"
instead of a score.

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

All tests mock outgoing network calls, so no API keys are required to run the suite.

## Project structure

```
src/     - bot and command implementations
tests/   - pytest test suite (mirrors src/, one test file per module)
```

## Deployment

`.github/workflows/ci-cd.yml` runs the test suite on every push and pull
request. A push to `main` (or a manual run from the Actions tab) also
deploys: it SSHes into the production server as a dedicated, unprivileged
deploy user, pulls the latest `main`, updates dependencies, and restarts
the bot's service — that user's `sudo` access is scoped to just restarting
and checking the one service, nothing more. Server-specific setup details
aren't kept in this repo.
