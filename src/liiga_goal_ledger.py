"""The Liiga tracker's goal ledger, split out of liiga_command.py (issue #22): what has been announced for each
game, how a feed's goal events are compared with it, the journal lines about a goal that changed or left the feed,
and the retraction of a goal that stays out of it."""
from irc_format import BOLD, RESET, RED, prefix as irc_prefix


class LiigaGoalLedgerMixin:
    """What the Liiga tracker has announced for each game: every goal is remembered by all the identities it
    has had (its eventId; its period, game time and scorer; its scorer and running score), so a goal that
    leaves the feed for a poll or comes back under another id is not announced twice (#16, #48, #62).
    A goal that was announced and then stays out of the feed for RETRACT_AFTER_MISSING_POLLS polls in a row is
    retracted with a `NO GOAL:` line (#22). Mixed into LiigaCommand, whose formatting helpers and state it uses."""

    NO_GOAL_PREFIX = irc_prefix("NO GOAL:", RED)

    # An announced goal is retracted once the feed has not shown it for this many polls in a row (about two
    # minutes at the 30 s poll rate). The feed was seen to drop a goal for one 10 s sample and bring it back
    # (2026-10-08, 2026-10-23), and a goal taken away never came back (2026-10-20, 2026-10-23).
    RETRACT_AFTER_MISSING_POLLS = 4

    # Tags of an event in goalEvents that is not a goal although it has a scorer: "RL0" is a penalty shot that did
    # not score (a scored one is "RL"); it carries the unchanged running score and the team's own goals count
    # leaves it out. Seen in 5 games this season; announced as a goal on 2026-10-15 (#58).
    NO_GOAL_TAGS = frozenset({"RL0"})

    SIDES = ("homeTeam", "awayTeam")

    def _real_goal_events(self, game, side):
        """goalEvents can include an entry for a goal disallowed by video
        review - confirmed live (Ässät-SaiPa, 2026-09-25): an entry
        tagged goalTypes ["YV", "VT0"] ("VT" = videotarkistus/video
        review) with scorerPlayerId 0 and scorerPlayer null, which never
        bumped the team's own authoritative "goals" count (stayed 0
        there while goalEvents kept the entry) - unlike every real goal,
        which always has a real scorer. Without filtering it out, it was
        announced as "GOAL: ... | TeamName — Unknown (YV/VT0)", which
        reads as a real goal by an unknown scorer rather than "no goal
        happened here at all". Filtering here (not in the callers) keeps
        the seeded baseline count and the announced slice built from the
        same list, so a disallowed goal is never seeded, counted, or
        announced.

        A missed penalty shot (tag RL0) is in the list too, with its shooter as the scorer and the
        score unchanged: it is not a goal either (issue #58).
        """
        events = (game.get(side) or {}).get("goalEvents") or []
        return [e for e in events if e.get("scorerPlayer") and not self.NO_GOAL_TAGS.intersection(e.get("goalTypes") or [])]

    def _event_keys(self, side, event) -> frozenset:
        """Every identity a goal event can be recognised by: its eventId and its period, game time and
        scorer - never its position in the list or the number of goals. A goal is the same goal if any of
        them matches an announced one: the shootout goal of 2026-10-14 was announced twice, a minute apart,
        because its identity changed between two polls while the feed's goal count stayed the same (#48).
        (A different goal differs in all of them - except one scorer scoring twice at the same period and
        game time, which only a very long shootout could produce.)"""
        keys = set()
        if event.get("eventId") is not None:
            keys.add((side, "id", event.get("eventId")))
        scorer = (event.get("scorerPlayer") or {}).get("playerId") or event.get("scorerPlayerId")
        keys.add((side, "at", event.get("period"), event.get("gameTime"), scorer))
        # Confirmed live (#62): one goal came back after a few minutes with a new eventId and its game
        # time 70 s later, so neither identity above matched. The scorer's running score is a third.
        keys.add((side, "score", event.get("period"), scorer, event.get("homeTeamScore"), event.get("awayTeamScore")))
        return frozenset(keys)

    def _reconcile(self, game, side, records):
        """(records, fresh): `records` is the list of announced goals, each the set of identities it has had so
        far (it only grows); `fresh` are the feed's events that match none of them, which the caller announces.
        A matching event adds its identities to the goal it matches."""
        records = [set(r) for r in records]
        fresh = []
        for event in self._real_goal_events(game, side):
            keys = self._event_keys(side, event)
            match = next((r for r in records if r & keys), None)
            if match is None:
                records.append(set(keys))
                fresh.append(event)
            else:
                match |= keys
        return records, fresh

    def _announce_new_goals(self, irc_bot, channel, game, prev, side):
        """Announces the goal events not announced (or seeded at the start)
        before. Announcing by identity and not by counting events matters
        because the feed was seen, twice in one afternoon (2026-10-08), to
        leave a goal out of one 10 s sample and bring it back in the next:
        a count that was replaced on every poll announced the goal again."""
        _, fresh = self._reconcile(game, side, prev["announced"][side])
        for event in fresh:
            self._safe_send(irc_bot, channel, self._format_goal(game, side, event))

    @staticmethod
    def _describe_key(key) -> str:
        return f"eventId {key[2]}" if key[1] == "id" else f"period {key[2]} time {key[3]} scorer {key[4]}"

    def _log_missing_goals(self, game_id, game, prev, now):
        """A journal line when the number of announced goals the feed does not
        show changes: a one-poll dip of a stale copy, or a goal taken away. And one when a goal's eventId
        changed or appeared while it was still recognised as the same goal (#48)."""
        for side in self.SIDES:
            for before, after in zip(prev["announced"][side], now["announced"][side]):
                old_ids = {k for k in before if k[1] == "id"}
                new_ids = {k for k in after if k[1] == "id"} - old_ids
                if new_ids:
                    print(f"Liiga: game {game_id}: {self._team_name(game, side)}: a goal's eventId "
                          f"{'changed' if old_ids else 'appeared'}, still the same goal: "
                          f"{', '.join(sorted(self._describe_key(k) for k in old_ids)) or 'none'} -> "
                          f"{', '.join(sorted(self._describe_key(k) for k in new_ids))} "
                          f"({', '.join(sorted(self._describe_key(k) for k in after if k[1] == 'at'))})")
            if prev["missing"][side] != now["missing"][side]:
                print(f"Liiga: game {game_id}: {self._team_name(game, side)}: announced goals missing from the feed "
                      f"{prev['missing'][side]} -> {now['missing'][side]}")

    def _posted_info(self, game, side, event) -> dict:
        """What the GOAL: line said about a goal, kept so a later poll can retract it."""
        scorer = event.get("scorerPlayer") or {}
        period_label = self.PERIOD_LABELS.get(event.get("period"), "")
        clock = self._format_clock(game, event)
        return {
            "team": self._team_name(game, side),
            "scorer_name": f"{scorer.get('firstName', '')} {scorer.get('lastName', '')}".strip() or "Unknown",
            "label": f"{clock} {period_label}".strip(),
        }

    def _ledger(self, game, side, prev, records, count_misses) -> list:
        """One entry per announced goal, in the order of `records`: how many polls in a row the feed has not
        shown it, and what its GOAL: line said (None for a goal already in the feed when tracking started -
        nobody saw that one announced, so it is never retracted)."""
        old = ((prev or {}).get("ledger") or {}).get(side) or []
        events = self._real_goal_events(game, side)
        entries = []
        for index, record in enumerate(records):
            shown = next((e for e in events if record & self._event_keys(side, e)), None)
            if index < len(old):
                entry = dict(old[index])
                if shown is not None:
                    entry["missing"] = 0
                elif count_misses:
                    entry["missing"] += 1
            else:
                entry = {"missing": 0, "posted": None if prev is None or shown is None
                         else self._posted_info(game, side, shown)}
            entries.append(entry)
        return entries

    def _feed_looks_broken(self, game, prev) -> bool:
        """A feed with no goal events at all while two or more announced goals are missing looks like a broken
        response, not two goals taken away at once: such a poll is not counted against the goals."""
        if prev is None or any(self._real_goal_events(game, side) for side in self.SIDES):
            return False
        waiting = sum(1 for side in self.SIDES for entry in (prev.get("ledger") or {}).get(side) or []
                      if entry["posted"] is not None)
        return waiting >= 2

    def _format_retraction(self, game, posted) -> str:
        home = self._team_name(game, "homeTeam")
        away = self._team_name(game, "awayTeam")
        when = f" {posted['label']}" if posted.get("label") else ""
        return (
            f"{self.NO_GOAL_PREFIX} {BOLD}{home} {self._team_goals(game, 'homeTeam')}-"
            f"{self._team_goals(game, 'awayTeam')} {away}{RESET}{when}"
            f" | {posted['scorer_name']} ({posted['team']}) was disallowed"
        )

    def _retract_gone_goals(self, irc_bot, channel, game, state):
        """Posts a `NO GOAL:` line for every announced goal the feed has not shown for
        RETRACT_AFTER_MISSING_POLLS polls, and forgets it (mutates `state`), so a goal that comes back later is
        announced again as new."""
        for side in self.SIDES:
            kept_records, kept_entries, gone = [], [], 0
            for record, entry in zip(state["announced"][side], state["ledger"][side]):
                if entry["posted"] is not None and entry["missing"] >= self.RETRACT_AFTER_MISSING_POLLS:
                    print(f"Liiga: retracting goal {entry['posted']['label']} by {entry['posted']['scorer_name']} "
                          f"in {channel}")
                    self._safe_send(irc_bot, channel, self._format_retraction(game, entry["posted"]))
                    gone += 1
                    continue
                kept_records.append(record)
                kept_entries.append(entry)
            state["announced"][side], state["ledger"][side] = kept_records, kept_entries
            state["missing"][side] -= gone

