import requests


class PesisPlayerNamesMixin:
    """Resolves a scorer/batter reference from the event feed (a global
    player id or a per-match jersey number - see
    PesisEventParsingMixin._last_player_ref()) into a display name, via
    /public/player/{id} and the per-match roster fetched by
    PesisDataFetchingMixin._fetch_match_roster()."""

    def _resolve_player_name(self, player_id):
        if player_id in self._player_cache:
            return self._player_cache[player_id]

        name = f"Player {player_id}"
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/public/player/{player_id}",
                params={"apikey": self.API_KEY},
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                name = (
                    data.get("name")
                    or f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
                    or name
                )
        except requests.exceptions.RequestException as e:
            print(f"{self.DISPLAY_NAME}: player lookup failed for {player_id}: {e}")
        except ValueError:
            print(f"{self.DISPLAY_NAME}: player lookup returned invalid JSON for {player_id}")

        self._player_cache[player_id] = name
        return name

    def _resolve_scorer_name(self, player_ref, team_id, batter_fallback, roster):
        """Resolves a scorer's display name from whatever reference the
        event feed actually gave us - see _last_player_ref() for why
        there are two shapes, and _fetch_match_roster() for the
        number->name mapping. batter_fallback may also be the literal
        string "Harhaheitto" (see _extract_runs) rather than a jersey
        number/id - returned as-is, never treated as something to look up."""
        if player_ref:
            if "id" in player_ref:
                return self._resolve_player_name(player_ref["id"])
            if "number" in player_ref:
                name = (roster.get(team_id) or {}).get(player_ref["number"])
                if name:
                    return name

        if batter_fallback is not None:
            if isinstance(batter_fallback, str):
                return batter_fallback
            # Try the roster first (batter is usually a jersey number in
            # the same matches that use "number"-style player refs);
            # only treat it as a global id if that comes up empty.
            name = (roster.get(team_id) or {}).get(batter_fallback)
            if name:
                return name
            return self._resolve_player_name(batter_fallback)

        return None
