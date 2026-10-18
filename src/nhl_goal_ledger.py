"""The NHL tracker's goal ledger, split out of nhl_command.py (issue #10): what has been announced for each
game, how a feed's goal list is compared with it, and the retraction of a goal that left the feed."""
from irc_format import BOLD, RESET, RED, prefix as irc_prefix


class NHLGoalLedgerMixin:
    """What the NHL tracker has announced for each game, and what it does about a feed that changes.

    A goal is remembered as the team plus its running score plus its event ids (not as a count per team, issue
    #20), so a goal that flickers out of the feed for a poll or is renumbered is not announced again, and a goal
    that stays out of the feed for RETRACT_AFTER_MISSING_POLLS polls is retracted with a `NO GOAL:` line that
    names the reason the feed records near it (a challenge or a video review). Mixed into NHLCommand, whose
    play-by-play fetches, formatting helpers and state it uses.
    """

    NO_GOAL_PREFIX = irc_prefix("NO GOAL:", RED)

    # An announced goal is retracted once it has been missing from the feed
    # for this many polls in a row (about 90-120 s at the 30 s poll rate):
    # long enough to ride out a feed flicker (seen: 20 s and 82 s), short
    # enough to still be news. Goals have been seen to vanish for good
    # 101-281 s after appearing.
    RETRACT_AFTER_MISSING_POLLS = 4

    # How far before/after a removed goal's clock a challenge stoppage may
    # sit and still be taken as its explanation (seen: from 31 s before the
    # goal to 2 s after it).
    CHALLENGE_WINDOW_BEFORE_SECONDS = 60
    CHALLENGE_WINDOW_AFTER_SECONDS = 10
    VIDEO_REVIEW = "video-review"  # the secondary reason of a referee stoppage for a review
    CHALLENGE_REASONS = (
        ("off-side", "offside challenge"),
        ("goal-interference", "goaltender interference challenge"),
    )

    # ---- announcements --------------------------------------------------

    def _real_goals(self, pbp, side):
        """Only plays with typeDescKey "goal" *and* a resolvable
        scoringPlayerId count as a real goal - matches the same "only
        count what actually has a scorer" defensive principle already
        used for LiigaCommand's video-review-disallowed-goal filtering,
        applied here pre-emptively rather than in response to a confirmed
        incident (this API hasn't been observed doing anything
        equivalent, but the same shape of problem - a play-type entry
        with no attributable scorer - is plausible here too)."""
        team_id = (pbp.get(side) or {}).get("id")
        return [
            p for p in (pbp.get("plays") or [])
            if p.get("typeDescKey") == "goal"
            and (p.get("details") or {}).get("scoringPlayerId")
            and (p.get("details") or {}).get("eventOwnerTeamId") == team_id
        ]

    def _all_real_goals(self, pbp) -> list:
        """Both teams' real goals in play order (the play-by-play is
        chronological)."""
        both = self._real_goals(pbp, "homeTeam") + self._real_goals(pbp, "awayTeam")
        position = {id(play): index for index, play in enumerate(pbp.get("plays") or [])}
        return sorted(both, key=lambda goal: position[id(goal)])

    def _goal_key(self, goal):
        """What makes a goal 'the same goal' across polls: the scoring team
        and the running score right after it, i.e. the team's n-th goal.
        Confirmed live that one goal can be listed twice under different
        event ids (same period, clock and running score, the players' roles
        in a different order), so the event id alone is not an identity.
        A shootout goal is keyed by its event id instead: every shootout
        goal carries the same running score and a clock of 00:00. With no
        usable running score the event id is the fallback too."""
        details = goal.get("details") or {}
        home, away = details.get("homeScore"), details.get("awayScore")
        period_type = (goal.get("periodDescriptor") or {}).get("periodType")
        if period_type != "SO" and isinstance(home, int) and isinstance(away, int):
            return ("score", details.get("eventOwnerTeamId"), home, away)
        if goal.get("eventId") is not None:
            return ("id", goal.get("eventId"))
        return ("time", details.get("eventOwnerTeamId"),
                (goal.get("periodDescriptor") or {}).get("number"), goal.get("timeInPeriod"))

    def _new_record(self, ids, posted) -> dict:
        return {"ids": list(ids), "posted": posted, "missing": 0, "retracted": False}

    def _resolve_goals(self, pbp, announced, seed=False) -> tuple:
        """(goals to announce now, posted goals that are gone, the updated
        record) for one game's feed. `announced` maps a goal's key to a
        record: the event ids announced under it, what was posted for it
        (None for goals that were already in the feed when tracking
        started - nobody saw those announced, so they are never retracted),
        how many polls in a row it has been missing, and whether it was
        retracted. Compared with counting goals per team (which this
        replaced), this survives what the feed was seen doing:
          - a goal vanishing for a poll and coming back (same id): known, not
            announced again, and not retracted unless it stays away;
          - the same goal listed under two ids at once: announced once;
          - a goal removed and a different goal later reaching the same
            running score: the old ids are gone, so the new goal is announced.
            If the old goal had already been missing for long enough it was
            retracted on its own (a disallowed goal, then the team's next
            goal). If the new goal arrives sooner, the feed has most likely
            replaced a first entry with the corrected one (the first scorer
            named was wrong: seen as a removal and a new goal 20 s apart, the
            same running score, the first scorer then credited with an assist),
            which is not a disallowed goal, so nothing is retracted and the
            old line is left as it was;
          - a goal whose running score was renumbered, but whose id was
            already announced: known, not announced again. When the score it
            moved to belonged to another announced goal, that goal is the one
            that is gone (a disallowed goal renumbers the team's later goals
            into its place) and is retracted in its turn;
          - an announced goal that stays missing for
            RETRACT_AFTER_MISSING_POLLS polls: reported gone.
        New goals come back in play order across both teams."""
        record = {key: dict(rec, ids=list(rec["ids"])) for key, rec in announced.items()}
        by_key = {}
        for goal in self._all_real_goals(pbp):
            by_key.setdefault(self._goal_key(goal), []).append(goal)

        new, gone = [], []
        for key, plays in by_key.items():
            ids = [i for i in (play.get("eventId") for play in plays) if i is not None]
            rec = record.get(key)
            if rec is not None and rec["retracted"]:
                rec = None  # a retracted goal that returns, or a later goal at this score, is new
            if rec is not None and (not ids or not rec["ids"] or any(i in rec["ids"] for i in ids)):
                # Journal lines so the server log shows when a repeat was ignored.
                if rec["missing"]:
                    print(f"NHL: game {pbp.get('id')}: goal {self._key_text(key)} is back after "
                          f"{rec['missing']} missed poll(s), not announced again")
                extra = [i for i in ids if i not in rec["ids"]]
                if extra and rec["ids"]:
                    print(f"NHL: game {pbp.get('id')}: goal {self._key_text(key)} listed again as "
                          f"event {extra}, not announced again")
                rec["ids"] += [i for i in ids if i not in rec["ids"]]
                rec["missing"] = 0
                continue
            moved = next((k for k, r in record.items()
                          if k != key and not r["retracted"] and any(i in r["ids"] for i in ids)), None)
            if moved is not None:
                print(f"NHL: game {pbp.get('id')}: goal {self._key_text(moved)} is now "
                      f"{self._key_text(key)}, not announced again")
                displaced = record.get(key)
                record[key] = record.pop(moved)  # renumbered: same goal under a new key
                record[key]["ids"] += [i for i in ids if i not in record[key]["ids"]]
                record[key]["missing"] = 0
                if displaced is not None and not displaced["retracted"]:
                    # The goal that used to have this score is the one that is gone (a
                    # disallowed goal renumbers the team's later goals into its place):
                    # keep it under a key no play can have, so it counts as missing and
                    # is retracted after the usual wait.
                    record[("displaced", *displaced["ids"])] = displaced
                continue
            posted = rec["posted"] if rec is not None else None
            if posted is not None and ids and self._same_goal_rekeyed(plays[0], posted):
                print(f"NHL: game {pbp.get('id')}: goal {self._key_text(key)} is now event {ids}, "
                      f"same scorer and period, not announced again")
                rec["ids"] += [i for i in ids if i not in rec["ids"]]
                rec["missing"] = 0
                continue
            new.append(plays[0])
            record[key] = self._new_record(ids, None if seed else self._posted_info(pbp, plays[0]))

        # A feed with no plays at all, or with no goals while two or more announced
        # ones are missing, looks like a broken response, not a run of disallowed
        # goals (those leave one at a time): do not count it against the goals.
        missing = [r for k, r in record.items() if k not in by_key and r["posted"] is not None and not r["retracted"]]
        broken = not (pbp.get("plays") or []) or (not by_key and len(missing) >= 2)
        if broken and missing:
            print(f"NHL: game {pbp.get('id')}: the feed shows no goal for {len(missing)} announced "
                  f"one(s), not counting this poll as a miss")
        for key, rec in record.items():
            if key in by_key or rec["posted"] is None or rec["retracted"] or broken:
                continue
            rec["missing"] += 1
            if rec["missing"] >= self.RETRACT_AFTER_MISSING_POLLS:
                rec["retracted"] = True
                gone.append(rec["posted"])

        position = {id(play): index for index, play in enumerate(pbp.get("plays") or [])}
        return sorted(new, key=lambda goal: position[id(goal)]), gone, record

    @staticmethod
    def _same_goal_rekeyed(goal, posted) -> bool:
        """The feed re-listed a posted goal under a new event id (confirmed live:
        the clock a second earlier, the assists filled in): same scorer, same
        period. A different scorer is a correction and is announced."""
        scorer = (goal.get("details") or {}).get("scoringPlayerId")
        period = (goal.get("periodDescriptor") or {}).get("number")
        return scorer is not None and scorer == posted["scorer"] and period == posted["period"]

    def _key_text(self, key) -> str:
        if key[0] == "score":
            return f"(team {key[1]}, {key[2]}-{key[3]})"
        return "(" + ", ".join(str(part) for part in key) + ")"

    def _posted_info(self, pbp, goal) -> dict:
        """What the announcement said about a goal, kept so a later poll can
        retract it (and, later, correct it)."""
        details = goal.get("details") or {}
        home = pbp.get("homeTeam") or {}
        team = self._team_name(home if details.get("eventOwnerTeamId") == home.get("id") else pbp.get("awayTeam") or {})
        clock = goal.get("timeInPeriod") or ""
        period = goal.get("periodDescriptor") or {}
        return {
            "team": team,
            "scorer": details.get("scoringPlayerId"),
            "scorer_name": self._resolve_player_name(pbp, details.get("scoringPlayerId")),
            "assists": [details.get(f"assist{n}PlayerId") for n in (1, 2) if details.get(f"assist{n}PlayerId")],
            "period": period.get("number"),
            "clock": clock,
            "label": f"{clock} {self._period_label(period)}".strip(),
        }

    def _announce_new_goals(self, irc_bot, channel, pbp, announced) -> dict:
        """Retracts the goals that are gone, announces the goals not
        announced yet, and returns the updated record to store."""
        new, gone, record = self._resolve_goals(pbp, announced)
        for posted in gone:
            print(f"NHL: retracting goal {posted['label']} by {posted['scorer_name']} in {channel}")
            self._safe_send(irc_bot, channel, self._format_retraction(pbp, posted))
        for goal in new:
            self._safe_send(irc_bot, channel, self._format_goal(pbp, goal))
        return record

    def _format_retraction(self, pbp, posted) -> str:
        home = self._team_name(pbp.get("homeTeam") or {})
        away = self._team_name(pbp.get("awayTeam") or {})
        home_score, away_score = self._current_score(pbp)
        reason = self._challenge_reason(pbp, posted.get("period"), self._clock_seconds(posted.get("clock")))
        why = f" ({reason})" if reason else ""
        # Same shape as the GOAL: line (score, then clock and period). An earlier
        # "the 11:47 3rd goal by ..." was read as "the 3rd goal" - the 3rd is the period.
        when = f" {posted['label']}" if posted.get("label") else ""
        return (
            f"{self.NO_GOAL_PREFIX} {BOLD}{home} {home_score}-{away_score} {away}{RESET}{when}"
            f" | {posted['scorer_name']} ({posted['team']}) was disallowed{why}"
        )

    def _current_score(self, pbp) -> tuple:
        """The score as the remaining goals show it: the running score of the
        last real goal, 0-0 with none; the header score if that goal has no
        running score."""
        goals = self._all_real_goals(pbp)
        if not goals:
            return 0, 0
        details = goals[-1].get("details") or {}
        home, away = details.get("homeScore"), details.get("awayScore")
        if isinstance(home, int) and isinstance(away, int):
            return home, away
        return (pbp.get("homeTeam") or {}).get("score", "?"), (pbp.get("awayTeam") or {}).get("score", "?")

    def _clock_seconds(self, clock):
        try:
            minutes, seconds = str(clock).split(":")
            return int(minutes) * 60 + int(seconds)
        except (ValueError, TypeError):
            return None

    def _challenge_reason(self, pbp, period, seconds):
        """Why a goal was taken off, from the stoppage the feed records near it - a coach's challenge (the code
        "chlg-..." in its reason or in its secondary reason), or a video review (a referee stoppage whose
        secondary reason is "video-review"; 2026-10-14: the feed held that and no challenge entry for a goal
        that was taken off) - (its clock is where play was stopped,
        which can be up to half a minute before the goal), or None. A challenge that failed is followed by a
        bench penalty and the goal stands, so such a stoppage is not an explanation. The nearest stoppage
        wins."""
        if seconds is None:
            return None
        plays = pbp.get("plays") or []
        best = None
        for index, play in enumerate(plays):
            if play.get("typeDescKey") != "stoppage":
                continue
            details = play.get("details") or {}
            # The challenge code is usually in "reason", but on 2026-10-15 a stoppage had the generic
            # "referee-or-linesman" there and "chlg-vis-off-side" only in "secondaryReason" (#59).
            code = next((r for r in (details.get("reason"), details.get("secondaryReason"))
                         if (r or "").startswith("chlg")), None)
            challenge = code is not None
            if not challenge and details.get("secondaryReason") != self.VIDEO_REVIEW:
                continue
            if (play.get("periodDescriptor") or {}).get("number") != period:
                continue
            at = self._clock_seconds(play.get("timeInPeriod"))
            if at is None or not (seconds - self.CHALLENGE_WINDOW_BEFORE_SECONDS <= at <= seconds + self.CHALLENGE_WINDOW_AFTER_SECONDS):
                continue
            if challenge and any(p.get("typeDescKey") == "penalty"
                                 and (p.get("details") or {}).get("descKey") == "delaying-game-unsuccessful-challenge"
                                 for p in plays[index + 1:index + 5]):
                continue
            if best is None or abs(at - seconds) < best[0]:
                text = (next((t for fragment, t in self.CHALLENGE_REASONS if fragment in code), "challenge")
                        if challenge else "video review")
                best = (abs(at - seconds), text)
        return best[1] if best else None
