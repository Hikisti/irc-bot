import json


class PesisEventParsingMixin:
    """Turns pesistulokset.fi's raw event feed into RUN:/JAKSO:/FINAL:
    text, plus the run/period-end grouping helper _process_match() uses
    to know how many of each have already been announced.

    Split out of PesisCommand as a mixin, not standalone functions, since
    every method here reads instance state already established on
    PesisCommand/LiveTrackerCommand (self.PERIOD_LABELS, self.RUN_PREFIX,
    self.FINAL_PREFIX, self.PERIOD_END_PREFIX) - PesisCommand still owns
    those class attributes, this just organizes the methods that use them.
    """

    # Finnish scoring vocabulary confirmed live in pesistulokset.fi's
    # event feed - see _is_run_sub_event()'s own docstring for the full
    # story behind each pattern. Named here rather than left as scattered
    # literals since this vocabulary is explicitly disclosed as
    # incomplete (see the class docstring on PesisCommand) and every
    # addition so far came from a real report of a missed run - a new one
    # only ever needs adding in this one place.
    TEXT_HOME_BASE = "kotipesään"              # "to home base" - a run's destination
    TEXT_ADVANCED_PREFIX = "eteni"             # "advanced" - regular baserunning
    TEXT_HOME_RUN = "löi kunnarin!"            # "hit a home run!"
    TEXT_TIEBREAK_RUN = "juoksu"               # "run" - scoring-contest tie-break decider
    TEXT_WILD_THROW = "harhaheitolla"          # "by a wild throw" - suffix on an "eteni" event
    TEXT_FORCED_WALK = "vapaataipaleen"        # "free passage" - a bases-loaded walk forcing a run home
    BATTER_WILD_THROW = "Harhaheitto"          # pesistulokset.fi's own placeholder "batter" for a wild-throw run
    BATTER_FORCED_WALK = "Vapaataival"         # pesistulokset.fi's own placeholder "batter" for a forced-walk run
    PERIOD_END_FALLBACK_TEXT = "Jakso päättyi"  # marker present but no text of its own - fallback

    def _extract_runs(self, event):
        """Yields (player_ref, batter_fallback, sub_event) for every run
        scored within this event - a single event can contain more than
        one (e.g. a hit that scores multiple runners already on base).
        player_ref is {"id": N}, {"number": N}, or None - see
        _resolve_scorer_name() for why there are two shapes. sub_event is
        the raw matched sub-event, exposed for
        _group_runs_and_period_ends()'s intra-poll duplicate check.

        Best-effort against real data, not a guarantee: pesäpallo's event
        feed has a rich Finnish scoring vocabulary that a handful of real
        matches couldn't fully catalog. Any run this misses still shows
        up correctly in the final/authoritative score (see
        _process_match) - only the individual "RUN:" chat announcement
        for that specific play could be missing, not the score itself.
        """
        for sub_event in event.get("events") or []:
            texts = sub_event.get("texts") or []
            if self._is_run_sub_event(texts):
                player_ref = self._last_player_ref(texts)
                if self._is_error_driven_run(texts):
                    # A run caused by a wild throw isn't attributable to a
                    # batter's own hit at all - confirmed live that the
                    # parent event's "batter" field for one of these
                    # pointed at a completely unrelated player's earlier
                    # at-bat, and pesistulokset.fi's own site shows
                    # literally "Harhaheitto" instead of a name for these.
                    batter = self.BATTER_WILD_THROW
                elif self._is_walk_forced_run(texts):
                    # Same idea for a bases-loaded walk forcing a run home
                    # - confirmed live (Ykköspesis) pesistulokset.fi's own
                    # page shows literally "Vapaataival" as the batter for
                    # these too, not the name of whoever actually drew the
                    # walk.
                    batter = self.BATTER_FORCED_WALK
                else:
                    batter = event.get("batter")
                yield player_ref, batter, sub_event

    def _is_run_sub_event(self, texts) -> bool:
        # Confirmed ways pesäpallo's event feed records a run reaching
        # home plate:
        #  - regular play: an "eteni..." (advanced) event - "eteni" alone,
        #    or with a suffix like "eteni harhaheitolla" (wild throw) -
        #    whose destination is the string "kotipesään" (home base).
        #    Crucially NOT "paloi" (put out) reaching home, which uses the
        #    same destination string for a runner who was retired instead
        #    of scoring.
        #  - a home run: "löi kunnarin!" - doesn't pair with a "kotipesään"
        #    destination string like regular advancement does.
        #  - a bases-loaded walk forcing a run home: "sai vapaataipaleen
        #    väärien syöttöjen johdosta" (confirmed live, Ykköspesis) -
        #    paired with a "kotipesään" destination the same way "eteni"
        #    is.
        #  - a scoring-contest / tie-break decider: a "juoksu" (run) event.
        event_texts = [
            t.get("text") for t in texts
            if isinstance(t, dict) and t.get("type") == "event" and isinstance(t.get("text"), str)
        ]
        if self.TEXT_TIEBREAK_RUN in event_texts or self.TEXT_HOME_RUN in event_texts:
            return True
        if any(
            text.startswith(self.TEXT_ADVANCED_PREFIX) or self.TEXT_FORCED_WALK in text
            for text in event_texts
        ):
            plain_texts = {t for t in texts if isinstance(t, str)}
            if self.TEXT_HOME_BASE in plain_texts:
                return True
        return False

    def _is_error_driven_run(self, texts) -> bool:
        return any(
            isinstance(t, dict) and t.get("type") == "event"
            and isinstance(t.get("text"), str) and self.TEXT_WILD_THROW in t.get("text")
            for t in texts
        )

    def _is_walk_forced_run(self, texts) -> bool:
        return any(
            isinstance(t, dict) and t.get("type") == "event"
            and isinstance(t.get("text"), str) and self.TEXT_FORCED_WALK in t.get("text")
            for t in texts
        )

    def _last_player_ref(self, texts):
        """Two confirmed formats a player reference in the event feed can
        take, apparently varying by match/data source: some matches embed
        a global player "id" directly (used with _resolve_player_name());
        others only give a per-match jersey "number" - confirmed live
        that treating the latter as a global id resolves to a real but
        completely unrelated player, so it must instead be looked up
        against that match's own roster (see _resolve_scorer_name())."""
        ref = None
        for t in texts:
            if isinstance(t, dict) and t.get("type") == "player":
                if t.get("id") is not None:
                    ref = {"id": t.get("id")}
                elif t.get("number") is not None:
                    ref = {"number": t.get("number")}
        return ref

    def _format_run(self, event, home_name, away_name, home_runs, away_runs, scoring_team_id, home_id,
                     scorer_name, batter_name=None):
        scoring_team = home_name if scoring_team_id == home_id else away_name
        suffix = self._format_run_suffix(event)
        scorer_str = scorer_name or "Unknown"
        # Lyöjä (batter) before etenijä (scorer), matching pesistulokset.fi's
        # own column order. Skipped entirely if it's the same person as the
        # scorer (e.g. a home run) or unresolvable - no point naming
        # "Unknown" twice or repeating a name for no information gain.
        namestr = f"{batter_name} → {scorer_str}" if batter_name and batter_name != scorer_name else scorer_str
        return (
            f"{self.RUN_PREFIX} {scoring_team} — {namestr} | "
            f"{home_name} {home_runs}-{away_runs} {away_name}{suffix}"
        )

    def _format_run_suffix(self, event) -> str:
        """" (2. jakso, 3. lopettava)" - the period label plus the
        vuoropari (batting turn) pesistulokset.fi's own page shows this
        exact play under, e.g. "3. lopettava" for the second (away) side
        of the match's 3rd inning. Built from "inning" (0-indexed, so
        +1) and "batTurn" (0 = "aloittava", the side that bats first in
        that inning, 1 = "lopettava", the side that bats second) -
        confirmed live against several real matches' own play-by-play
        pages that this pairing (not e.g. home/away) is what the Finnish
        "aloittava"/"lopettava" labels track. Falls back to just the
        period label if either field is missing (older/incomplete event
        data)."""
        period_label = self._format_period_suffix(event.get("period"))
        inning, bat_turn = event.get("inning"), event.get("batTurn")
        if inning is None or bat_turn not in (0, 1):
            return f" ({period_label})" if period_label else ""
        vuoropari = f"{inning + 1}. {'aloittava' if bat_turn == 0 else 'lopettava'}"
        parts = [p for p in (period_label, vuoropari) if p]
        return f" ({', '.join(parts)})" if parts else ""

    def _format_period_suffix(self, period) -> str:
        if period is None:
            return ""
        return self.PERIOD_LABELS.get(period, f"jakso {period + 1}")

    def _format_period_end(self, text, home_name, away_name, home_runs, away_runs) -> str:
        return (
            f"{self.PERIOD_END_PREFIX} {text} | "
            f"{home_name} {home_runs}-{away_runs} {away_name}"
        )

    def _format_final(self, live_result, home_name, away_name) -> str:
        """Real pesäpallo results are headlined by jaksovoitot (periods
        won), not total runs - e.g. pesistulokset.fi's own result_string
        is "1-0k (0-0, 0-0, 0-0, 2-1)". "periods" (confirmed live) gives
        the jaksovoitot; the parenthetical is each played period's own
        run tally via _period_runs(), skipping periods that never
        happened (e.g. a match decided without needing a tie-break)."""
        periods = live_result.get("periods") or {}
        home_won, away_won = periods.get("home"), periods.get("away")
        headline = f"{home_won} - {away_won}" if home_won is not None and away_won is not None else "? - ?"

        breakdown_parts = []
        runs = live_result.get("runs")
        if isinstance(runs, list):
            for index in range(len(runs)):
                h = self._period_runs(live_result, "home", index)
                a = self._period_runs(live_result, "away", index)
                if h is None and a is None:
                    continue  # period never played (e.g. no tie-break needed)
                breakdown_parts.append(f"{h if h is not None else 0} - {a if a is not None else 0}")
        breakdown = f" ({', '.join(breakdown_parts)})" if breakdown_parts else ""

        return f"{home_name} - {away_name} {headline}{breakdown}"

    def _extract_period_end_text(self, event):
        """Returns the human-readable text for a period-ending event (e.g.
        "Ensimmäinen jakso päättyi", "Supervuoro päättyi"), or None.
        Detected via a {"type":"stat","periodend":...} marker - confirmed
        present (and reliable) across every period transition checked
        live, including into "Supervuoro" - rather than matching the
        Finnish wording itself, which would be one more guess at a
        vocabulary this API doesn't document. Distinct from match-end,
        which uses its own "match-ended" stat key, not "periodend"."""
        for sub_event in event.get("events") or []:
            texts = sub_event.get("texts") or []
            has_periodend = any(
                isinstance(t, dict) and t.get("type") == "stat" and "periodend" in t
                for t in texts
            )
            if not has_periodend:
                continue
            for t in texts:
                if isinstance(t, dict) and t.get("type") == "event" and t.get("text"):
                    return t.get("text")
            return self.PERIOD_END_FALLBACK_TEXT  # marker present but no text of its own
        return None

    def _sum_values(self, values):
        """Sums a list of per-play run counts, treating a missing/non-list
        value or one with no numeric entries as "no data" (None) rather
        than 0 - the inner loop _period_runs() scopes to a single
        period."""
        if not isinstance(values, list):
            return None
        total = 0
        found_any = False
        for v in values:
            if isinstance(v, (int, float)):
                total += v
                found_any = True
        return total if found_any else None

    def _period_runs(self, live_result, side, period_index):
        """Scoped to a single period, not summed across the whole match -
        used for RUN:/JAKSO:, since pesäpallo scores each jakso
        independently (see _process_match). A naive match-wide cumulative
        sum would double-count once a match reaches its second period."""
        if period_index is None:
            return None
        runs = live_result.get("runs")
        if not isinstance(runs, list) or not (0 <= period_index < len(runs)):
            return None
        return self._sum_values((runs[period_index] or {}).get(side))

    def _group_runs_and_period_ends(self, events, home_id, away_id):
        """Full-rescan helper shared by _process_match() and
        _seed_match_extras(): groups every currently-recognized run by
        (period, side) in array order (returned as
        {(period, side): [(event, player_ref, batter), ...]}), plus the
        first period-end marker text seen per period (returned as
        {period: text}).

        Deliberately NOT content-signature based, unlike an earlier
        version of this file. Confirmed live, two distinct ways
        pesistulokset.fi's event feed can change an already-seen play's
        own bytes between polls: a parent event's own sub-array growing
        in place after it's already been scanned once (match 147206), and
        a play apparently getting retracted and reissued with different
        content for the same real point, e.g. correcting who was actually
        at bat (match 147201) - both defeat a content hash, either by
        making an already-counted play look "new" again or leaving a
        genuinely new one looking like a duplicate, depending on exactly
        what mutated. Counting *how many* run-shaped sub-events exist for
        a given (period, side) on a full rescan, and only ever announcing
        ones past however many were already announced (see
        _process_match), doesn't care whether a specific play's bytes
        changed between polls - only how many total plays exist for that
        side/period right now, which also sidesteps a third confirmed
        wrinkle (match 147207): a run for an earlier period appearing in
        the array after play has already moved on to a later one no
        longer needs special-casing, since every (period, side) is
        tracked independently regardless of when it's encountered while
        scanning.

        One thing this full rescan alone can't tell apart from a
        genuinely new play: confirmed live (match 147206) that the same
        exact play's sub-event can appear *twice within the very same
        fetch*, re-appended later in the array at a different position
        with byte-identical content. So each pass also guards against
        that specific case with a per-call (not persisted - see above for
        why that distinction matters) content check, scoped to
        (period, side) so it can never suppress two different real plays
        that legitimately look alike."""
        runs_by_period_side = {}
        period_end_by_period = {}
        seen_this_pass = set()
        for event in events:
            event_period = event.get("period")
            team_id = event.get("team") if event.get("team") is not None else event.get("hTeam")
            if team_id == home_id:
                side = "home"
            elif team_id == away_id:
                side = "away"
            else:
                side = None
            if side is not None:
                for player_ref, batter, sub_event in self._extract_runs(event):
                    dup_key = (event_period, side, json.dumps(sub_event, sort_keys=True, ensure_ascii=False))
                    if dup_key in seen_this_pass:
                        continue  # the same exact play, re-appended elsewhere in this same fetch
                    seen_this_pass.add(dup_key)
                    runs_by_period_side.setdefault((event_period, side), []).append((event, player_ref, batter))

            if event_period not in period_end_by_period:
                period_end_text = self._extract_period_end_text(event)
                if period_end_text:
                    period_end_by_period[event_period] = period_end_text
        return runs_by_period_side, period_end_by_period
