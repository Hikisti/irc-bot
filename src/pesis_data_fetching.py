import datetime
import json
import time

import requests


class PesisDataFetchingMixin:
    """All of PesisCommand's actual pesistulokset.fi HTTP calls: resolving
    a league name to its current-season seasonSeries id (with an on-disk
    cache), and fetching matches/events/roster for a given id/date. Also
    provides _api_get(), used by PesisPlayerNamesMixin's player lookup
    too (both mixins are only ever combined via PesisCommand)."""

    def _api_get(self, path, params=None, what="request", item=None):
        """GET {BASE_URL}/{path} with the apikey merged into `params`,
        returning the parsed JSON body, or None on any network/HTTP
        failure or non-JSON response (logged either way). `what` names
        the endpoint for that log line (e.g. "series-list"); `item`
        (optional) is a per-call id (e.g. a match or player id) appended
        as "for {item}" - every call site here already handles a None
        result (or any other JSON shape) the same way it would handle
        this failing outright, so there's no separate default to pick."""
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/{path}",
                params={"apikey": self.API_KEY, **(params or {})},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            suffix = f" for {item}" if item is not None else ""
            print(f"{self.DISPLAY_NAME} {what} request failed{suffix}: {e}")
            return None
        except ValueError:
            suffix = f" for {item}" if item is not None else ""
            print(f"{self.DISPLAY_NAME} {what} returned invalid JSON{suffix}")
            return None

    def _resolve_series_id(self):
        """Finds the current season's "Miesten <league>" seasonSeries id
        by name, so this doesn't need updating every season. Returns None
        on any failure (network error, unexpected shape, or just not
        found). Result is cached (SERIES_CACHE_TTL_SECONDS), both
        in-memory and on disk, since the id is stable for an entire
        season and the underlying request is the single heaviest one
        this command makes - the on-disk copy means a bot restart
        doesn't lose the cache either."""
        if self._series_cache is None:
            self._series_cache = self._load_series_cache()

        if self._series_cache is not None:
            series_id, resolved_at = self._series_cache
            if time.time() - resolved_at < self.SERIES_CACHE_TTL_SECONDS:
                return series_id

        data = self._api_get(
            "public/series-list",
            # Restricts the response to the current season only - ~1MB
            # instead of ~5.6MB for the unfiltered (all 82+ historical
            # seasons) response. Same param the site's own frontend uses
            # for this.
            params={"current-season": "true"},
            what="series-list",
        )

        seasons = data.get("seasons") if isinstance(data, dict) else None
        if not seasons:
            return None

        def season_year(s):
            return (s.get("season") or {}).get("season", -1)

        latest = max(seasons, key=season_year)
        # Confirmed live (Ykköspesis): a level/series pair can match more
        # than one seasonSeries - "Ykköspesis"/"Miehet" matches both the
        # real "Miesten Ykköspesis" league and several unrelated
        # same-category tournaments (e.g. "Talven harjoitusotteluita").
        # "shortcut": true reliably marks the actual league (confirmed for
        # both leagues configured so far) - preferred over just taking
        # whichever match happens to sort first, which only coincidentally
        # picked the right one before this was checked.
        candidates = [
            ss for ss in (latest.get("seasonSerieses") or [])
            if (ss.get("level") or {}).get("name") == self.SERIES_LEVEL_NAME
            and (ss.get("series") or {}).get("name") == self.SERIES_NAME
        ]
        chosen = next((ss for ss in candidates if ss.get("seasonSeries", {}).get("shortcut")), None)
        if chosen is None:
            chosen = candidates[0] if candidates else None
        series_id = (chosen.get("seasonSeries") or {}).get("id") if chosen else None

        if series_id is not None:
            self._series_cache = (series_id, time.time())
            self._save_series_cache(self._series_cache)
        return series_id

    def _load_series_cache(self):
        try:
            with open(self.SERIES_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return (data["series_id"], data["resolved_at"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"{self.DISPLAY_NAME}: failed to read series cache file: {e}")
            return None

    def _save_series_cache(self, cache):
        series_id, resolved_at = cache
        try:
            with open(self.SERIES_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"series_id": series_id, "resolved_at": resolved_at}, f)
        except OSError as e:
            # Not fatal - just means this run's cache stays in-memory-only
            # (per _resolve_series_id's in-memory check) instead of also
            # surviving a restart.
            print(f"{self.DISPLAY_NAME}: failed to write series cache file: {e}")

    def _fetch_today_matches(self, series_id):
        """Returns {match_id: match_dict} for today, or None on failure."""
        now = datetime.datetime.now(self.HELSINKI_TZ)
        return self._fetch_matches_for_date(series_id, now.strftime("%Y-%m-%d"))

    def _fetch_matches_for_date(self, series_id, date_str):
        """Returns {match_id: match_dict} for a specific date, or None on
        failure."""
        data = self._api_get(
            "public/matches-list",
            params={"seasonSeriesId": series_id, "date": date_str},
            what="matches-list",
        )
        if data is None:
            return None

        matches = {}
        if not isinstance(data, list):
            return matches
        for entry in data:
            for group in (entry.get("groups") or []):
                for m in (group.get("matches") or []):
                    mid = m.get("id")
                    if mid is not None:
                        matches[mid] = m
        return matches

    def _fetch_next_matchday(self, series_id):
        """Searches forward day by day (bounded by NEXT_SEARCH_MAX_DAYS,
        starting today) for the next date with scheduled matches *that
        aren't all already finished* - so checking !superpesis next hours
        after today's matches ended reports the actual next matchday
        instead of repeating today's now-stale result. Unlike liiga.fi,
        this API has no "next date with matches" hint to jump straight
        to, hence the bounded linear search rather than a single lookup.

        Returns a ("found", date_str, matches_dict) / ("not_found", None,
        None) / ("error", None, None) triple - kept distinct from a plain
        None so the caller can tell "genuinely nothing scheduled soon"
        apart from "couldn't reach the API" instead of conflating them.
        """
        now = datetime.datetime.now(self.HELSINKI_TZ)
        for offset in range(self.NEXT_SEARCH_MAX_DAYS + 1):
            date_str = (now + datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
            matches = self._fetch_matches_for_date(series_id, date_str)
            if matches is None:
                return "error", None, None
            # Only today's (offset 0) result needs the "already finished"
            # check - a future date's matches can't have finished yet.
            if matches and not (offset == 0 and self._all_matches_finished(matches)):
                return "found", date_str, matches
        return "not_found", None, None

    def _all_matches_finished(self, matches) -> bool:
        return bool(matches) and all(
            bool((m.get("liveResult") or {}).get("finished")) for m in matches.values()
        )

    def _fetch_match_events(self, match_id):
        """Returns the full events list for a match, or None on failure."""
        data = self._api_get(f"online/{match_id}/events", what="match-events", item=match_id)
        events = data.get("events") if isinstance(data, dict) else None
        return events if isinstance(events, list) else None

    def _fetch_match_roster(self, match_id):
        """Returns {team_id: {jersey_number: player_name}} for a match, or
        an empty dict on any failure (jersey-number-only player refs then
        fall back to a placeholder name rather than crashing or - worse -
        resolving to a real but unrelated player, which is the bug this
        exists to fix; see _last_player_ref())."""
        data = self._api_get("public/match", params={"id": match_id}, what="match-detail", item=match_id)
        if not isinstance(data, dict):
            return {}

        roster = {}
        for side in ("home", "away"):
            team = data.get(side) or {}
            team_id = team.get("id")
            if team_id is None:
                continue
            by_number = {}
            for p in (team.get("players") or []):
                if not isinstance(p, dict):
                    continue
                number = p.get("number")
                name = p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                if number is not None and name:
                    by_number[number] = name
            roster[team_id] = by_number
        return roster
