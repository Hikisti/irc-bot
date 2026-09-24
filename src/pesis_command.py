import datetime
import os
import traceback
from concurrent.futures import ThreadPoolExecutor

from irc_format import GREEN, ORANGE, PURPLE, prefix as irc_prefix
from live_tracker_command import LiveTrackerCommand
from pesis_event_parsing import PesisEventParsingMixin
from pesis_player_names import PesisPlayerNamesMixin
from pesis_data_fetching import PesisDataFetchingMixin


class PesisCommand(LiveTrackerCommand, PesisEventParsingMixin, PesisPlayerNamesMixin, PesisDataFetchingMixin):
    """
    Live-tracks today's matches of a Finnish pesäpallo league/division in a
    channel, announcing runs and final results as they happen. Uses
    pesistulokset.fi's unofficial JSON API (the same one that powers
    pesistulokset.fi itself, authenticated with the "public" API key baked
    into that site's own frontend - not a secret credential).

    This is the shared engine, not a command by itself - every concrete
    command (SuperpesisCommand, YkkospesisCommand, ...) is a thin subclass
    that only overrides SERIES_LEVEL_NAME/SERIES_NAME (which league this
    instance tracks), DISPLAY_NAME (how it's named in chat messages and
    log lines) and CACHE_SLUG (its own series-id disk cache file, so two
    subclasses running concurrently - e.g. !superpesis and !ykkospesis in
    the same channel - never share or clobber each other's state; every
    other piece of state - _channels, _player_cache, _series_cache - is
    already a fresh instance attribute per subclass instance too). None of
    the rest of this file is specific to any one league: same API, same
    endpoints, same event/roster shapes, same scoring vocabulary,
    confirmed live against real matches from both leagues currently
    configured.

    This class itself holds the start/stop/next lifecycle glue and the
    core match-processing loop (_run, _poll_once, _process_match); the
    three cohesive chunks of logic it's built from live in their own
    mixin files, brought in via multiple inheritance above:
    PesisEventParsingMixin (pesis_event_parsing.py, turning the raw event
    feed into RUN:/JAKSO:/FINAL: text), PesisPlayerNamesMixin
    (pesis_player_names.py, scorer/batter name resolution), and
    PesisDataFetchingMixin (pesis_data_fetching.py, every actual
    pesistulokset.fi HTTP call). See live_tracker_command.py for the
    start/stop/next/poll-loop shell shared with LiigaCommand.

    Usage (COMMAND_NAME substituted per subclass, e.g. "!superpesis"):
      <COMMAND_NAME> start  -> start polling today's matches in this channel
      <COMMAND_NAME> stop   -> stop polling in this channel
      <COMMAND_NAME> next   -> show the next upcoming matchday and its times

    Every subclass configured so far is restricted to the #pesis.fi channel
    (see CommandHandler's "channels" config), unlike most other commands.

    Notes on the implementation, since this was built by reverse-engineering
    an undocumented API rather than reading real docs:
      - "Miesten <league>" (men's) is looked up dynamically by name each
        time tracking starts (level=SERIES_LEVEL_NAME, series="Miehet"),
        rather than a hardcoded season-series id, since that id changes
        every season. The women's division shares the same level name,
        distinguished only by series="Miehet" vs "Naiset". Confirmed live
        that a level/series pair can match *more than one* seasonSeries
        (e.g. Ykköspesis: the real "Miesten Ykköspesis" league alongside
        several unrelated same-category tournaments like "Talven
        harjoitusotteluita") - disambiguated by preferring an entry with
        "shortcut": true (confirmed live this reliably marks the actual
        league, for both leagues configured so far), falling back to the
        first match if none has it set. See _resolve_series_id().
      - Score and "finished" status always come from /public/matches-list's
        authoritative liveResult object, never computed from the event
        feed - the event feed is only used for *which* player scored, and
        a per-poll running score is snapped back to the authoritative
        total after each cycle so it can't silently drift.
      - Only runs are surfaced (via the confirmed patterns in
        _is_run_sub_event(): "eteni"/"eteni harhaheitolla" (wild throw)
        reaching "kotipesään", "löi kunnarin!" (home run), "sai
        vapaataipaleen..." (a bases-loaded walk forcing a run home -
        confirmed live in an Ykköspesis match; pesistulokset.fi's own page
        labels the batter column literally "Vapaataival" for these, the
        same convention as the unrelated "Harhaheitto" wild-throw case),
        and "juoksu" for a scoring-contest tie-break); the richer
        play-by-play (individual hits, defensive positioning, outs, etc.)
        is intentionally not parsed. Checked against several real,
        finished matches across both leagues configured so far: this
        still isn't guaranteed to be the complete pesäpallo scoring
        vocabulary (each addition so far came from a real report of a
        missed run), so a genuinely new pattern could still be missed as
        an individual "RUN:" announcement. This is a disclosed gap in the
        play-by-play, not a rounding error to "fix" preemptively; the
        final/authoritative score is never affected by it either way (see
        _process_match).
      - Scorer identification: a player reference in the event feed is
        either a global {"id": N} (resolved via /public/player/{id}) or a
        per-match jersey {"number": N} - confirmed live that some matches
        only give the latter, and that treating a jersey number as a
        global id resolves to a real but completely unrelated player.
        Jersey numbers are resolved against that match's own roster
        (fetched once via /public/match?id=, see _fetch_match_roster())
        instead. See _resolve_scorer_name().
      - Performance: /public/series-list is large (confirmed live: ~1MB
        even filtered to the current season via "current-season=true",
        ~5.6MB unfiltered across 82+ historical seasons) and was the
        single biggest cost in !superpesis start. Its result is cached
        (SERIES_CACHE_TTL_SECONDS) since the resolved id only changes
        around a season boundary. Per-match event/roster seeding also
        runs concurrently across matches rather than one at a time (see
        _seed_match_extras()), since they're independent lookups.
      - Event feed reliability: /online/{id}/events has been confirmed
        live to change shape between polls in ways a naive "diff what's
        new" approach can't handle, across three separate real-match
        incidents:
          1) (match 147206) the array can contain a whole segment of
             already-seen plays re-appended later at different positions,
             AND a single outer event's own "events" sub-array can grow
             in place after that outer event already sits at a fixed
             array position (a batter's turn starts as just the hit, then
             runner advances/scores get appended to that same event over
             several polls).
          2) (match 147201) the same real point can apparently get
             retracted and reissued mid-game with entirely different
             content (e.g. correcting who was actually at bat).
          3) (match 147207) a run for an *earlier* period can appear in
             the array well after that period's own JAKSO: end was
             already announced (e.g. a correction to a jakso 1 play
             surfacing partway through jakso 2), even though the
             fully-settled, post-game array is always cleanly
             period-ordered.
        Two earlier designs tried here - slicing by event-count position,
        then a full rescan relying on a content-hash "seen" set - each
        fixed one or two of these but not all three (a content hash, for
        instance, is defeated equally by (1)'s in-place mutation and
        (2)'s deliberate re-issue, in opposite directions: one needs the
        hash to stay stable, the other guarantees it won't). The design
        that actually holds up against all three at once, used by
        _process_match() and _group_runs_and_period_ends(): full rescan
        every poll (unavoidable - it's the only way to see (1)'s growth),
        grouped into an independent list per (period, side), with only a
        plain *count* of how many of that list have already been
        announced as state - not which specific ones, not their content.
        A play's bytes changing between polls is irrelevant to a count;
        (3) needs no special-casing since every (period, side) is its own
        independent bucket regardless of when it's encountered while
        scanning. The count per (period, side) is also capped at
        pesistulokset.fi's own authoritative per-period total (always
        eventually correct, confirmed against real final results) as a
        last-resort safety net against ever displaying a score that
        total doesn't confirm - both seeded from the match's existing
        history when tracking starts (_seed_match_extras()) so starting
        mid-match doesn't replay it.
    """

    BASE_URL = "https://api.pesistulokset.fi/api/v1"
    # Public frontend API key, extracted from pesistulokset.fi's own JS
    # bundle - the same one every visitor's browser uses, not a secret.
    API_KEY = "wRX0tTke3DZ8RLKAMntjZ81LwgNQuSN9"

    # The four attributes below are what make a concrete subclass -
    # PesisCommand itself is never instantiated directly. See the class
    # docstring for what each one is for.
    SERIES_LEVEL_NAME = None
    SERIES_NAME = None
    DISPLAY_NAME = None
    CACHE_SLUG = None
    # e.g. "!superpesis" - only used for the usage string and log-message
    # readability, never for actual command dispatch (that's
    # CommandHandler's job, driven by its own aliases config).
    COMMAND_NAME = None

    TRACKED_NOUN = "matches"
    PERIOD_NOUN = "matchday"
    STATE_KEY = "matches"
    REQUIRES_CONTEXT = True

    # /public/series-list is ~1MB even filtered to the current season alone
    # (5.6MB unfiltered, across 82+ historical seasons) - confirmed live
    # this was the single biggest cost in "<command> start". The resolved
    # id only ever changes around a season boundary, so caching it for a
    # few hours cuts that cost to (near) zero on every start after the
    # first, at negligible staleness risk. Persisted to disk (not just
    # kept in memory) so a bot restart doesn't lose it either - otherwise
    # every restart pays the full cost again on the very next start. The
    # file itself is keyed by CACHE_SLUG (set in __init__, once
    # CACHE_SLUG is known) so two subclasses never share or clobber each
    # other's cached series id.
    SERIES_CACHE_TTL_SECONDS = 6 * 3600

    RUN_PREFIX = irc_prefix("RUN:", GREEN)
    FINAL_PREFIX = irc_prefix("FINAL:", ORANGE)
    PERIOD_END_PREFIX = irc_prefix("JAKSO:", PURPLE)

    # The event feed's "period" field is 0-indexed - confirmed live via a
    # "Ensimmäinen jakso päättyi" (first period ended) event carrying
    # period=0, not period=1 as originally (wrongly) assumed. Falls back
    # to a generic "jakso N" for any value beyond what's been observed.
    PERIOD_LABELS = {
        0: "1. jakso",
        1: "2. jakso",
        2: "supervuoro",
        3: "kotiutuslyöntikilpailu",
    }

    def __init__(self):
        super().__init__()
        self._player_cache = {}  # player id -> display name
        self._series_cache = None  # (series_id, resolved_at_epoch_seconds) or None; see _resolve_series_id()
        # Per-subclass (CACHE_SLUG) file, so !superpesis and !ykkospesis
        # never share or clobber each other's cached series id.
        self.SERIES_CACHE_FILE = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f".{self.CACHE_SLUG}_series_cache.json",
        )

    # ---- "next" lookup hooks (see LiveTrackerCommand._run_next) --------

    def _resolve_context(self):
        return self._resolve_series_id()

    def _format_not_found_message(self) -> str:
        return f"No upcoming {self.DISPLAY_NAME} matches found in the next {self.NEXT_SEARCH_MAX_DAYS} days."

    def _fetch_next_period(self, context):
        status, date_str, matches = self._fetch_next_matchday(context)
        if status == "error":
            return None, None
        if status == "not_found":
            return None, {}
        return date_str, matches

    def _format_period_summary(self, items):
        return self._format_matches_summary(items)

    # ---- background thread entry point --------------------------------

    def _run(self, irc_bot, channel, stop_event):
        try:
            series_id = self._resolve_series_id()
        except Exception as e:
            print(f"{self.DISPLAY_NAME} series lookup error: {e}")
            series_id = None

        if series_id is None:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, f"Error: could not reach the {self.DISPLAY_NAME} API.")
            return

        try:
            matches = self._fetch_today_matches(series_id)
        except Exception as e:
            print(f"{self.DISPLAY_NAME} initial fetch error: {e}")
            matches = None

        if matches is None:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, f"Error: could not reach the {self.DISPLAY_NAME} API.")
            return

        if not matches:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, f"No {self.DISPLAY_NAME} matches scheduled today.")
            return

        state = {mid: self._seed_snapshot(m) for mid, m in matches.items()}
        self._seed_match_extras(state)

        with self._lock:
            entry = self._channels.get(channel)
            if entry is None or entry["stop_event"] is not stop_event:
                return  # stopped (or superseded) before the lookup finished
            entry["matches"] = state

        summary = self._format_matches_summary(matches.values())
        self._safe_send(
            irc_bot, channel, f"Tracking {len(matches)} {self.DISPLAY_NAME} match(es) today: {summary}"
        )

        self._poll_loop(irc_bot, channel, stop_event, series_id)

    def _seed_match_extras(self, state):
        """Fills in each match's event-count baseline, roster, and
        already-announced run/period-end counts in `state` (mutated in
        place). The "announced"/"ended_periods" counts are what actually
        gate re-announcing a play (see _process_match) - seeded from
        every run/period-end already in the match's history so
        `!superpesis start` on a match already in progress doesn't replay
        its whole history as fresh RUN:/JAKSO: lines. One match's
        events+roster fetch doesn't depend on any other's, so all matches
        are seeded concurrently rather than one at a time - with 2+
        matches tracked (the common case) this roughly halves the time
        !superpesis start takes to report back."""
        if not state:
            return

        def seed_one(mid):
            events = self._fetch_match_events(mid)
            roster = self._fetch_match_roster(mid)
            return mid, events, roster

        with ThreadPoolExecutor(max_workers=len(state)) as executor:
            for mid, events, roster in executor.map(seed_one, state.keys()):
                if events is not None:
                    state[mid]["event_count"] = len(events)
                    runs_by_period_side, period_end_by_period = self._group_runs_and_period_ends(
                        events, state[mid]["home_id"], state[mid]["away_id"],
                    )
                    state[mid]["announced"] = {key: len(items) for key, items in runs_by_period_side.items()}
                    state[mid]["ended_periods"] = set(period_end_by_period.keys())
                state[mid]["roster"] = roster

    def _poll_once(self, irc_bot, channel, series_id) -> bool:
        matches = self._fetch_today_matches(series_id)
        if matches is None:
            return False

        with self._lock:
            entry = self._channels.get(channel)
            if entry is None:
                return True
            prev_state = entry["matches"]

        all_ended = bool(prev_state)
        new_state = {}

        for mid, prev in prev_state.items():
            match = matches.get(mid)
            if match is None:
                # Not in today's list anymore (e.g. date rolled over past
                # midnight mid-match) - leave its last known state alone
                # rather than guessing it's finished.
                new_state[mid] = prev
                if not prev.get("finished"):
                    all_ended = False
                continue

            try:
                new_state[mid] = self._process_match(irc_bot, channel, match, prev)
            except Exception as e:
                # _process_match is the biggest, most-changed method in
                # this file and every real bug found so far broke inside
                # it - the traceback is what actually pinpoints the line,
                # str(e) alone often isn't enough (e.g. a bare KeyError).
                print(f"{self.DISPLAY_NAME}: failed to process match {mid} in {channel}: {e}")
                traceback.print_exc()
                new_state[mid] = prev

            if not new_state[mid].get("finished"):
                all_ended = False

        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None:
                entry["matches"] = new_state

        return all_ended

    def _process_match(self, irc_bot, channel, match, prev):
        if prev.get("finished"):
            # Confirmed live: the event feed can keep appending events
            # (apparent corrections/re-syncs to a period's tally) well
            # after "Ottelu päättyi" already fired and FINAL: was sent -
            # once a match is done, stop touching it entirely rather than
            # risk more RUN:/JAKSO: lines that contradict the final result.
            return prev

        live = match.get("liveResult") or {}
        home_id, away_id = prev["home_id"], prev["away_id"]
        home_name, away_name = prev["home_name"], prev["away_name"]
        roster = prev.get("roster") or {}

        events = self._fetch_match_events(prev["match_id"])
        # {(period, side): count of runs already announced for that
        # period+side} - the only state driving what still needs
        # announcing. See _group_runs_and_period_ends() for why this is
        # count-based rather than content-signature based.
        announced = dict(prev.get("announced") or {})
        ended_periods = set(prev.get("ended_periods") or ())

        if events is not None:
            runs_by_period_side, period_end_by_period = self._group_runs_and_period_ends(
                events, home_id, away_id,
            )

            # Sorted so a poll that finds new runs in more than one
            # period/side announces them in a sensible (period, then
            # side) order rather than arbitrary dict order.
            for period, side in sorted(runs_by_period_side, key=lambda k: (k[0] if k[0] is not None else -1, k[1])):
                items = runs_by_period_side[(period, side)]
                # Pesäpallo scores each jakso independently, not as a
                # match-long running total (the API's own result object
                # bears this out: it has separate per-period run arrays,
                # not one cumulative count) - so this is scoped to a
                # single period, not the whole match. Never let the
                # announced count for a side exceed pesistulokset.fi's
                # own authoritative period total: confirmed live (match
                # 147201) that a play can apparently get retracted and
                # reissued mid-game with different content (e.g.
                # correcting who was actually at bat), which would
                # otherwise risk a double-announced or over-the-real-total
                # score. The authoritative total is always eventually
                # correct (confirmed against real final results), so
                # capping against it is a safe invariant regardless of
                # what's actually happening in the raw feed.
                authoritative = self._period_runs(live, side, period)
                already = announced.get((period, side), 0)
                limit = len(items) if authoritative is None else min(len(items), authoritative)
                scoring_team_id = home_id if side == "home" else away_id
                for idx in range(already, limit):
                    event, player_ref, batter = items[idx]
                    announced[(period, side)] = idx + 1
                    scorer_name = self._resolve_scorer_name(player_ref, scoring_team_id, batter, roster)
                    # The batter ("lyöjä") who put the ball in play is a
                    # separate person from the runner who scored
                    # ("etenijä") - resolved the same way (roster first,
                    # since it's just as likely to be a jersey number).
                    batter_name = (
                        self._resolve_scorer_name(None, scoring_team_id, batter, roster)
                        if batter is not None else None
                    )
                    self._safe_send(irc_bot, channel, self._format_run(
                        event, home_name, away_name, announced.get((period, "home"), 0),
                        announced.get((period, "away"), 0), scoring_team_id, home_id,
                        scorer_name, batter_name,
                    ))
                if authoritative is not None and len(items) > limit:
                    # Diagnostic, not an error: means more run-shaped
                    # sub-events have been detected for this side/period
                    # than pesistulokset.fi's own authoritative total
                    # currently confirms - either that total simply hasn't
                    # caught up yet (this will resolve itself once it
                    # does, since `already` is never advanced past
                    # `limit`) or one of the detected ones is a retraction
                    # artifact that will never be confirmed. Either way,
                    # logged so a recurrence leaves hard evidence.
                    print(
                        f"{self.DISPLAY_NAME}: match {prev['match_id']} period {period} ({side}) has "
                        f"{len(items)} detected run(s) but authoritative total is only "
                        f"{authoritative} - holding back {len(items) - limit}"
                    )

            for period, text in period_end_by_period.items():
                if period in ended_periods:
                    continue
                ended_periods.add(period)
                # The period's own final tally, using whatever was
                # actually announced for it above (capped at
                # authoritative the same way, so this can't show a number
                # pesistulokset.fi's own page wouldn't).
                self._safe_send(irc_bot, channel, self._format_period_end(
                    text, home_name, away_name,
                    announced.get((period, "home"), 0), announced.get((period, "away"), 0),
                ))

        # Informational only (e.g. for anyone inspecting state) - nothing
        # above is gated by this; monotonic so a transiently shorter API
        # response can't shrink it.
        event_count = max(prev["event_count"], len(events)) if events is not None else prev["event_count"]

        # "period" is purely informational/for display continuity here -
        # doesn't gate anything above, unlike the position/period-reset
        # tracking this replaced. "lastPeriod" (confirmed live) is the
        # period currently, or most recently, being played.
        current_period = live.get("lastPeriod")
        if current_period is None:
            current_period = prev.get("period")
        period_home_runs = announced.get((current_period, "home"), 0)
        period_away_runs = announced.get((current_period, "away"), 0)

        finished = bool(live.get("finished"))
        if finished and not prev.get("finished"):
            self._safe_send(
                irc_bot, channel,
                f"{self.FINAL_PREFIX} {self._format_final(live, home_name, away_name)}",
            )

        return {
            "match_id": prev["match_id"],
            "home_id": home_id,
            "away_id": away_id,
            "home_name": home_name,
            "away_name": away_name,
            "period": current_period,
            "period_home_runs": period_home_runs,
            "period_away_runs": period_away_runs,
            "event_count": event_count,
            "finished": finished,
            "roster": roster,  # rosters don't change mid-match, carry forward unchanged
            "announced": announced,
            "ended_periods": ended_periods,
        }

    # ---- match summary (start message) ---------------------------------

    def _match_start_label(self, match):
        date_str = match.get("date")
        if not date_str:
            return None
        try:
            dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        return dt.astimezone(self.HELSINKI_TZ).strftime("%H:%M")

    def _format_matches_summary(self, matches) -> str:
        groups = {}
        order = []
        for match in matches:
            label = self._match_start_label(match) or "??:??"
            home = (match.get("home") or {}).get("name") or "?"
            away = (match.get("away") or {}).get("name") or "?"
            name = f"{home}-{away}"
            if label not in groups:
                groups[label] = []
                order.append(label)
            groups[label].append(name)

        order.sort(key=lambda label: (label == "??:??", label))
        return " | ".join(f"{label} {', '.join(groups[label])}" for label in order)

    def _seed_snapshot(self, match):
        live = match.get("liveResult") or {}
        # "lastPeriod" is the period currently (or most recently) being
        # played, confirmed live - e.g. lastPeriod=1 ("2. jakso") with
        # lastPeriodFinished=False for a match mid-second-period. Falls
        # back to 0 (1st period) for a match with no liveResult data yet.
        current_period = live.get("lastPeriod")
        if current_period is None:
            current_period = 0
        return {
            "match_id": match.get("id"),
            "home_id": (match.get("home") or {}).get("id"),
            "away_id": (match.get("away") or {}).get("id"),
            "home_name": (match.get("home") or {}).get("name") or "Unknown",
            "away_name": (match.get("away") or {}).get("name") or "Unknown",
            "period": current_period,
            "period_home_runs": self._period_runs(live, "home", current_period) or 0,
            "period_away_runs": self._period_runs(live, "away", current_period) or 0,
            "event_count": 0,  # seeded from a real fetch below, see _run()
            "finished": bool(live.get("finished")),
            "roster": {},  # seeded from a real fetch below, see _run()
            # Both seeded from a real fetch below too (_seed_match_extras)
            # with every run/period-end already in the match's history up
            # to now, so starting mid-match doesn't replay old plays as
            # fresh RUN:/JAKSO: announcements - _process_match rescans the
            # full events array every poll and relies entirely on these
            # counts for "already announced" (see
            # _group_runs_and_period_ends()), not array position or
            # content hashing.
            "announced": {},  # {(period, side): count}
            "ended_periods": set(),
        }


class SuperpesisCommand(PesisCommand):
    """Live-tracks Miesten Superpesis (pesäpallo, men's top division) - see
    PesisCommand for the shared implementation."""
    SERIES_LEVEL_NAME = "Superpesis"
    SERIES_NAME = "Miehet"
    DISPLAY_NAME = "Superpesis"
    CACHE_SLUG = "superpesis"
    COMMAND_NAME = "!superpesis"


class YkkospesisCommand(PesisCommand):
    """Live-tracks Miesten Ykköspesis (pesäpallo, men's second division) -
    see PesisCommand for the shared implementation. Confirmed live against
    real matches (147197, 2026-09-03) that the API, event/roster shapes,
    and scoring vocabulary are identical to Superpesis's."""
    SERIES_LEVEL_NAME = "Ykköspesis"
    SERIES_NAME = "Miehet"
    DISPLAY_NAME = "Ykköspesis"
    CACHE_SLUG = "ykkospesis"
    COMMAND_NAME = "!ykkospesis"
