"""The Liiga tracker's goal ledger, split out of liiga_command.py (issue #22): what has been announced for each
game, how a feed's goal events are compared with it, and the journal lines about a goal that changed or left the feed."""


class LiigaGoalLedgerMixin:
    """What the Liiga tracker has announced for each game: every goal is remembered by all the identities it
    has had (its eventId; its period, game time and scorer; its scorer and running score), so a goal that
    leaves the feed for a poll or comes back under another id is not announced twice (#16, #48, #62).
    Mixed into LiigaCommand, whose formatting helpers and state it uses."""

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
