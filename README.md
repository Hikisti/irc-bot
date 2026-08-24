# irc-bot

A simple IRC bot (KukistiBot) with a handful of chat commands: weather, stock, crypto,
electricity price, local time, and F1 schedule lookups. It also auto-fetches and posts
page titles for links shared in channel.

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
```

- `WEATHER_API_KEY` — from [weatherapi.com](https://www.weatherapi.com/), required (the bot
  fails to start without it).
- `TIME_API_KEY` — from [ipgeolocation.io](https://ipgeolocation.io/), optional; only needed
  for `!time <city>` lookups (timezone abbreviation lookups like `!time cdt` work without it).

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
| Björck | `!bjorck` | none | `!bjorck` |

`!liiga start` polls today's Finnish Liiga (ice hockey) games every 30s in the channel it was
started in, and announces goals and final scores as they happen. It stops automatically once
all of today's games have ended, or on `!liiga stop`. `!liiga next` looks up the next upcoming
gameday (today if there's still something scheduled, otherwise the next date with games) and
lists its matchups grouped by start time, e.g. `Next Liiga gameday (tomorrow): 18:30 TPS-Jokerit,
Pelicans-KooKoo`. Uses the unofficial liiga.fi JSON API, so no key is needed but the endpoint
isn't guaranteed to stay stable.

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
