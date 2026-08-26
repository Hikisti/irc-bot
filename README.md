# irc-bot

A simple IRC bot (KukistiBot) with a handful of chat commands: weather, stock, crypto,
electricity price, local time, F1 schedule, driving distance, and live Liiga (ice hockey) and
Superpesis (pesäpallo) score tracking. It also auto-fetches and posts page titles for links
shared in channel.

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
```

- `WEATHER_API_KEY` — from [weatherapi.com](https://www.weatherapi.com/), needed for `!weather`;
  if unset, `!weather` replies with an error but the rest of the bot still starts and works fine.
- `TIME_API_KEY` — from [ipgeolocation.io](https://ipgeolocation.io/), optional; only needed
  for `!time <city>` lookups (timezone abbreviation lookups like `!time cdt` work without it).
- `ORS_API_KEY` — from [openrouteservice.org](https://openrouteservice.org/dev/#/signup) (free,
  no credit card required as of writing), needed for `!distance`; if unset, `!distance` replies
  with an error but the rest of the bot still starts and works fine.

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
| Superpesis live tracker | `!superpesis` | `start`, `stop`, or `next` | `!superpesis start` |
| Björck | `!bjorck` | none | `!bjorck` |

`!liiga start` polls today's Finnish Liiga (ice hockey) games every 30s in the channel it was
started in, and announces goals and final scores as they happen. It stops automatically once
all of today's games have ended, or on `!liiga stop`. `!liiga next` looks up the next upcoming
gameday (today if there's still something scheduled, otherwise the next date with games) and
lists its matchups grouped by start time, e.g. `Next Liiga gameday (tomorrow): 18:30 TPS-Jokerit,
Pelicans-KooKoo`. Uses the unofficial liiga.fi JSON API, so no key is needed but the endpoint
isn't guaranteed to stay stable.

`!distance` looks up driving distance and drive time between two cities anywhere in the world,
via OpenRouteService. City names need a comma between them (`!distance New York, Los Angeles`)
unless both are a single word, in which case a space works too (`!distance Kokkola Vimpeli`) —
this avoids silently misreading a multi-word city name as the wrong split.

`!superpesis start` polls today's Miesten Superpesis (pesäpallo, men's top division) matches
every 30s and announces runs and final results as they happen, the same way `!liiga start` does
for hockey — stops automatically once all of today's matches have finished, or on
`!superpesis stop`. `!superpesis next` looks up the next upcoming matchday (today if there's
still something scheduled, otherwise the next date with matches, searched day by day up to 21
days ahead) and lists it the same way `!liiga next` does. **Only works in `#pesis.fi`** — unlike
every other command, it's restricted to that one channel (see `"channels"` in
`command_handler.py`); typing it elsewhere is silently ignored. Uses pesistulokset.fi's
unofficial JSON API. Each run announcement names both the etenijä (scorer) and, when different,
the lyöjä (the batter whose hit caused it), e.g. `RUN: Joensuun Maila — Konsta Piironen (lyöjä:
Joosua Rättö) | ...`; period transitions (jakso 1/2, supervuoro, kotiutuslyöntikilpailu) get
their own `JAKSO:` announcement. The score shown in `RUN:`/`JAKSO:` lines is scoped to the
current jakso only (pesäpallo scores each period independently, not as a running match total).
`FINAL:` reports the whole match in the real pesäpallo convention — jaksovoitot (periods won) as
the headline, per-period run breakdown in parens, e.g. `FINAL: Joensuun Maila - Sotkamon Jymy
1 - 0 (4 - 2, 2 - 2)`. A match stops being touched entirely once it's finished, so a
provider-side correction to the event feed after the fact can't produce more messages for it.
Individual run announcements are best-effort (pesäpallo's
scoring vocabulary wasn't fully catalogued — roughly 85-100% of a match's runs get an individual
chat line in testing), but the score and final result are always accurate since they come from
the API's own authoritative live-result summary, never computed locally.

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
