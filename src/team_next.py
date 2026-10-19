"""`!liiga next <team> [count]` and `!nhl next <team> [count]` (#50, #51): a
team's next few games from the sport's own season schedule. The argument
parsing, the team matching and the reply packing are shared here; each sport
supplies where the schedule comes from (`_team_directory`, `_upcoming_games`,
`_format_team_game`)."""
import threading
import time
import unicodedata

DEFAULT_COUNT = 3
# Ten games of the longest team names fit one IRC line (measured on the real
# schedules, 2026-10-15: 360 bytes for Liiga, 303 for the NHL).
MAX_COUNT = 10
MAX_LINE_BYTES = 380
MAX_CANDIDATES = 8
CACHE_SECONDS = 300


def normalise(text) -> str:
    """Casefolded, without diacritics, letters and digits only: 'Kärpät',
    'karpat' and 'K-Espoo' / 'kespoo' compare equal."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(ch for ch in decomposed if ch.isalnum() and not unicodedata.combining(ch))


def parse_next_args(rest):
    """(team words, count, clamped) from what follows `next`, or None when
    the usage text is due. The last word is the count if it is all digits
    (no team name contains a digit); a count of 0, or a number with no team,
    is a usage error. A negative number is not all digits, so it stays part
    of the team name."""
    words = rest.split()
    count, clamped = DEFAULT_COUNT, False
    if words and words[-1].isascii() and words[-1].isdigit():
        count = int(words.pop())
        if count == 0 or not words:
            return None
        if count > MAX_COUNT:
            count, clamped = MAX_COUNT, True
    return " ".join(words), count, clamped


def match_team(query, teams):
    """("ok" | "ambiguous" | "unknown", [labels]). `teams` maps a team's
    label to every name it answers to. The first of these that matches
    anything decides: the whole name, a prefix of it, a part of it."""
    wanted = normalise(query)
    if not wanted:
        return "unknown", []
    names = {label: [n for n in (normalise(name) for name in variants) if n] for label, variants in teams.items()}
    for test in (lambda n: n == wanted, lambda n: n.startswith(wanted), lambda n: wanted in n):
        hits = [label for label, variants in names.items() if any(test(n) for n in variants)]
        if len(hits) == 1:
            return "ok", hits
        if hits:
            return "ambiguous", hits
    return "unknown", []


def pack(header, parts, sep=" | "):
    """The reply as one line, or as several split only between games when it
    would pass MAX_LINE_BYTES. Later lines carry no header."""
    lines, current = [], header
    for part in parts:
        candidate = current + (sep if current != header else "") + part
        if current != header and len(candidate.encode("utf-8")) > MAX_LINE_BYTES:
            lines.append(current)
            current = part
        else:
            current = candidate
    lines.append(current)
    return lines


class TeamNextMixin:
    """The `next <team> [count]` lookup of a live-tracker command. The class
    sets SUPPORTS_TEAM_NEXT and implements the three schedule hooks."""

    SUPPORTS_TEAM_NEXT = True

    def _next_team(self, irc_bot, channel, rest):
        if irc_bot is None or channel is None:
            return "Error: this command needs channel context."
        parsed = parse_next_args(rest)
        if parsed is None:
            return f"Usage: {self.COMMAND_NAME} next [team [count]] (e.g., {self.COMMAND_NAME} next {self.TEAM_NEXT_EXAMPLE} 5)"
        query, count, clamped = parsed
        threading.Thread(target=self._run_next_team, args=(irc_bot, channel, query, count, clamped), daemon=True).start()
        return f"Checking the next {self.DISPLAY_NAME} {self.TRACKED_NOUN} of {query}..."

    def _run_next_team(self, irc_bot, channel, query, count, clamped):
        for line in self._team_next_lines(query, count, clamped):
            self._safe_send(irc_bot, channel, line)

    def _team_next_lines(self, query, count, clamped) -> list:
        unreachable = [f"Error: could not reach the {self.DISPLAY_NAME} API."]
        try:
            teams = self._team_directory()
        except Exception as e:
            print(f"{self.DISPLAY_NAME} team list error: {e}")
            teams = None
        if not teams:
            return unreachable
        status, hits = match_team(query, teams)
        if status == "unknown":
            return [f"Unknown team '{query}'. Teams: {', '.join(sorted(teams))}"]
        if status == "ambiguous":
            shown = ", ".join(hits[:MAX_CANDIDATES]) + (", ..." if len(hits) > MAX_CANDIDATES else "")
            return [f"Which team? {shown}"]
        label = hits[0]
        try:
            games = self._upcoming_games(label)
        except Exception as e:
            print(f"{self.DISPLAY_NAME} schedule error for {label}: {e}")
            games = None
        if games is None:
            return unreachable
        if not games:
            return [f"No upcoming {label} {self.TRACKED_NOUN} found."]
        shown = games[:count]
        # The number is named when it is not the default, or fewer are left than asked.
        counted = f"{len(shown)} " if clamped or count != DEFAULT_COUNT or len(shown) < count else ""
        limit = " (the maximum)" if clamped else ""
        header = f"Next {counted}{label} {self.TRACKED_NOUN}{limit}: "
        return pack(header, [self._format_team_game(game) for game in shown])

    def _cached(self, key, fetch):
        """fetch()'s result, kept for CACHE_SECONDS (in memory only); a
        failed fetch (None) is not kept."""
        cache = self.__dict__.setdefault("_team_next_cache", {})
        kept = cache.get(key)
        if kept is not None and time.monotonic() - kept[0] < CACHE_SECONDS:
            return kept[1]
        value = fetch()
        if value is not None:
            cache[key] = (time.monotonic(), value)
        return value
