import datetime
import re
from zoneinfo import ZoneInfo

import requests

from irc_format import BOLD, RESET, GREEN, ORANGE, RED, prefix as irc_prefix
from live_tracker_command import LiveTrackerCommand
from nhl_scoreboard import NHLScoreboardMixin


class NHLCommand(NHLScoreboardMixin, LiveTrackerCommand):
    """
    Live-tracks today's NHL games in a channel, announcing goals and final
    scores as they happen. Uses api-web.nhle.com, the NHL's own public web
    API (the same one that powers the NHL's official site) - undocumented,
    but free and keyless, the same situation as liiga.fi/pesistulokset.fi.

    Usage:
      !nhl start  -> start polling today's games in this channel
      !nhl stop   -> stop polling in this channel
      !nhl next   -> show the next upcoming NHL gameday's games and times
      !nhl now    -> today's board: live games (score, period, clock), then
                     finished and upcoming ones (see NHLScoreboardMixin)
      !nhl results -> finished games of the latest slate that has any

    Two things this API needs that liiga.fi doesn't:
      - "Today" is bucketed by US Eastern time, not Helsinki time - a game
        starting at 01:00 UTC is still "yesterday" to this API if that's
        still evening Eastern. EASTERN_TZ is used specifically for the
        fetch's own date parameter; HELSINKI_TZ (inherited) is still used
        for the early-start guard and every displayed time, since that's
        what a Helsinki viewer actually wants to see.
      - Unlike liiga.fi (whose one schedule call already embeds every
        today's game's full goalEvents list), api-web.nhle.com's schedule
        endpoint only has team names/start times/scores - the goal-by-goal
        detail (scorer, assists, period) needs a separate per-game
        /gamecenter/{id}/play-by-play call. That endpoint conveniently
        already embeds the full roster too, so no extra roster fetch is
        needed (unlike PesisCommand, which needs a separate roster call).
    """

    ALIASES = ("!nhl",)
    CHANNELS = ("#nhl.fi", "#veikkaus")

    DISPLAY_NAME = "NHL"
    COMMAND_NAME = "!nhl"
    CACHE_SLUG = "NHL"
    TRACKED_NOUN = "games"
    TRACKED_NOUN_COUNTED = "game(s)"
    PERIOD_NOUN = "gameday"
    STATE_KEY = "games"
    START_TIME_KEY = "startTimeUTC"
    ENDED_STATE_KEY = "ended"
    SUBCOMMANDS = ("start", "stop", "next", "now", "results")

    EASTERN_TZ = ZoneInfo("America/New_York")
    BASE_URL = "https://api-web.nhle.com/v1"
    # Attendance isn't in any of api-web.nhle.com's JSON (checked landing,
    # boxscore, play-by-play, right-rail, game-story) - only in NHL's HTML
    # Game Summary report, as "Attendance 19,250&nbsp;at&nbsp;Lenovo Center".
    REPORTS_URL = "https://www.nhl.com/scores/htmlreports"
    ATTENDANCE_RE = re.compile(r"Attendance(?:\s|&nbsp;|<[^>]*>){0,40}?(\d[\d,]*)", re.I)

    GOAL_PREFIX = irc_prefix("GOAL:", GREEN)
    FINAL_PREFIX = irc_prefix("FINAL:", ORANGE)
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
    CHALLENGE_REASONS = (
        ("off-side", "offside challenge"),
        ("goal-interference", "goaltender interference challenge"),
    )

    def execute(self, args=None, irc_bot=None, channel=None, **kwargs):
        arg = (args or "").strip().lower()
        if arg in ("now", "results"):
            return self._scoreboard(irc_bot, channel, arg)
        return super().execute(args, irc_bot=irc_bot, channel=channel, **kwargs)

    # ---- "next"/"run" lookup hooks (see LiveTrackerCommand._run_next / _run) --

    def _fetch_next_period(self, context):
        return self._fetch_next_gameday()

    def _format_period_summary(self, items):
        # The day may be partly played: show scores and (final) so a game
        # that is over or underway doesn't read as still to come.
        return self._format_games_summary(items, with_scores=True)

    def _fetch_today_items(self, context):
        return self._fetch_today_games()

    def _build_initial_state(self, items) -> dict:
        state = {gid: self._seed_snapshot(g) for gid, g in items.items()}
        self._seed_announced(state)
        return state

    def _seed_announced(self, state):
        """Fills in each game's record of goals already in its feed (mutated
        in place) by fetching its own play-by-play - seeded from every real
        goal already in the game's history so `!nhl start` on a game
        already in progress doesn't replay it as fresh GOAL: lines. One
        game's play-by-play fetch doesn't depend on any other's, so all
        of today's games are fetched concurrently rather than one at a
        time - same reasoning/pattern as PesisCommand's
        _seed_match_extras()."""
        for gid, pbp in self._fetch_concurrently(list(state.keys()), self._fetch_play_by_play).items():
            if pbp is not None:
                state[gid]["announced"] = self._resolve_goals(pbp, {}, seed=True)[2]

    # ---- polling ---------------------------------------------------------

    def _poll_once(self, irc_bot, channel, *context_args) -> bool:
        """Fetch current game states and announce diffs since the last poll.

        Returns True once every tracked game has ended (nothing left to watch).
        """
        prev_state = self._get_state(channel)
        if prev_state is None:
            return True

        games = self._fetch_tracked_games(prev_state)
        if games is None:
            return False

        all_ended = bool(games)
        new_state = {}

        # Every game that can still change needs its own play-by-play
        # fetch (unlike Liiga, whose single schedule call already has
        # everyone's goal events) - fetched concurrently, same reasoning
        # as _seed_goal_counts() above. Games not yet started or already
        # finished (and already announced) are skipped: nothing new can
        # be in their feed.
        to_fetch = [gid for gid, g in games.items() if self._needs_play_by_play(g, prev_state.get(gid))]
        pbp_by_gid = self._fetch_concurrently(to_fetch, self._fetch_play_by_play)

        for gid, game in games.items():
            try:
                prev = prev_state.get(gid)
                pbp = pbp_by_gid.get(gid)
                if prev is None:
                    # A game we weren't tracking yet (e.g. added after start).
                    # Seed a baseline silently instead of replaying old goals.
                    new_state[gid] = self._seed_snapshot(game)
                    if pbp is not None:
                        new_state[gid]["announced"] = self._resolve_goals(pbp, {}, seed=True)[2]
                    if not new_state[gid]["ended"]:
                        all_ended = False
                    continue

                announced = prev["announced"]
                if pbp is not None:
                    announced = self._announce_new_goals(irc_bot, channel, pbp, announced)

                ended = self._is_ended(game)
                if ended and not prev["ended"] and pbp is not None:
                    self._announce_end(irc_bot, channel, pbp)

                # Play-by-play fetch failed this cycle: `announced` is still
                # the previous record, carried forward unchanged, so the next
                # successful poll doesn't replay already-announced goals.
                new_state[gid] = self._seed_snapshot(game)
                new_state[gid]["announced"] = announced
                if not ended:
                    all_ended = False
            except Exception as e:
                print(f"NHL: failed to process game {gid} in {channel}: {e}")
                new_state[gid] = prev_state.get(gid) or self._seed_snapshot(game)
                all_ended = False

        self._set_state(channel, new_state)
        return all_ended

    def _needs_play_by_play(self, game, prev) -> bool:
        """False for a game whose play-by-play can't hold anything new:
        not started yet, or finished with the end already recorded in
        the previous snapshot (so its FINAL: has been announced). A game
        that just ended this cycle (prev not ended) is still fetched -
        that final poll carries its last goal and the FINAL: line."""
        if game.get("gameState") in ("FUT", "PRE"):
            return False
        return not (prev is not None and prev["ended"] and self._is_ended(game))

    # ---- start-time summary -------------------------------------------

    def _format_tracking_summary(self, items):
        return self._format_games_summary(items, with_scores=True)

    def _format_games_summary(self, games, with_scores=False) -> str:
        """Groups games by scheduled start time, e.g.
        '21:00 CAR-FLA, PHI-BOS | 22:00 EDM-WPG'. Uses each team's short
        abbreviation (not the full city+mascot name) here specifically -
        an NHL slate can run 8+ games a night, and full names
        ("Carolina Hurricanes-Florida Panthers, ...") would make this
        summary line unreadably long; full names are still used in the
        GOAL:/FINAL: messages below, where only one game's worth appears
        per line. With with_scores, a game already underway shows its
        score ('TOR 1-0 MTL') and a finished one adds '(final)'."""
        def label(game):
            home, away = self._team_abbrev(game, "homeTeam"), self._team_abbrev(game, "awayTeam")
            score = self._score_pair(game) if with_scores else None
            if score is None:
                return f"{home}-{away}"
            text = f"{home} {score[0]}-{score[1]} {away}"
            return f"{text} (final)" if self._is_ended(game) else text

        return self._format_start_time_summary(games, "startTimeUTC", label)

    def _score_pair(self, game):
        """(home, away) score of a game that has started, else None.
        FUT/PRE games have no score yet; anything else only counts if
        both scores are real numbers, so an unexpected shape shows no
        score rather than a wrong one."""
        if game.get("gameState") in ("FUT", "PRE"):
            return None
        home = (game.get("homeTeam") or {}).get("score")
        away = (game.get("awayTeam") or {}).get("score")
        if isinstance(home, int) and isinstance(away, int):
            return home, away
        return None

    # ---- team name helpers ------------------------------------------------

    def _team_abbrev(self, game, side) -> str:
        return (game.get(side) or {}).get("abbrev") or "?"

    def _team_name(self, team) -> str:
        place = (team.get("placeName") or {}).get("default") or ""
        common = (team.get("commonName") or {}).get("default") or ""
        return f"{place} {common}".strip() or "Unknown"

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

    def _resolve_player_name(self, pbp, player_id):
        if player_id is None:
            return "Unknown"
        for spot in pbp.get("rosterSpots") or []:
            if spot.get("playerId") == player_id:
                first = (spot.get("firstName") or {}).get("default", "")
                last = (spot.get("lastName") or {}).get("default", "")
                return f"{first} {last}".strip() or "Unknown"
        return "Unknown"

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
            already announced: known, not announced again;
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
            if rec is None and moved is not None:
                print(f"NHL: game {pbp.get('id')}: goal {self._key_text(moved)} is now "
                      f"{self._key_text(key)}, not announced again")
                record[key] = record.pop(moved)  # renumbered: same goal under a new key
                record[key]["ids"] += [i for i in ids if i not in record[key]["ids"]]
                record[key]["missing"] = 0
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
        return (
            f"{self.NO_GOAL_PREFIX} {BOLD}{home} {home_score}-{away_score} {away}{RESET}"
            f" | the {posted['label']} goal by {posted['scorer_name']} ({posted['team']}) was disallowed{why}"
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
        """Why a goal was taken off, from the coach's-challenge stoppage the
        feed records near it (its clock is where play was stopped, which can
        be up to half a minute before the goal), or None. A challenge that
        failed is followed by a bench penalty and the goal stands, so such a
        stoppage is not an explanation."""
        if seconds is None:
            return None
        plays = pbp.get("plays") or []
        best = None
        for index, play in enumerate(plays):
            reason = (play.get("details") or {}).get("reason") or ""
            if play.get("typeDescKey") != "stoppage" or not reason.startswith("chlg"):
                continue
            if (play.get("periodDescriptor") or {}).get("number") != period:
                continue
            at = self._clock_seconds(play.get("timeInPeriod"))
            if at is None or not (seconds - self.CHALLENGE_WINDOW_BEFORE_SECONDS <= at <= seconds + self.CHALLENGE_WINDOW_AFTER_SECONDS):
                continue
            if any(p.get("typeDescKey") == "penalty"
                   and (p.get("details") or {}).get("descKey") == "delaying-game-unsuccessful-challenge"
                   for p in plays[index + 1:index + 5]):
                continue
            if best is None or abs(at - seconds) < best[0]:
                best = (abs(at - seconds), reason)
        if best is None:
            return None
        return next((text for fragment, text in self.CHALLENGE_REASONS if fragment in best[1]), "challenge")

    def _format_goal(self, pbp, goal) -> str:
        home = self._team_name(pbp.get("homeTeam") or {})
        away = self._team_name(pbp.get("awayTeam") or {})
        details = goal.get("details") or {}
        home_score = details.get("homeScore", "?")
        away_score = details.get("awayScore", "?")

        scoring_team_id = details.get("eventOwnerTeamId")
        scoring_team = home if scoring_team_id == (pbp.get("homeTeam") or {}).get("id") else away

        scorer_name = self._resolve_player_name(pbp, details.get("scoringPlayerId"))
        assist_names = ", ".join(
            self._resolve_player_name(pbp, details.get(f"assist{n}PlayerId"))
            for n in (1, 2) if details.get(f"assist{n}PlayerId")
        )
        assist_str = f" (assists: {assist_names})" if assist_names else ""

        tags = self._goal_tags(pbp, goal)
        tag_str = f" ({'/'.join(tags)})" if tags else ""

        period = goal.get("periodDescriptor") or {}
        period_label = self._period_label(period)
        time_str = f" {goal.get('timeInPeriod', '')} {period_label}".rstrip()

        return (
            f"{self.GOAL_PREFIX} {BOLD}{home} {home_score}-{away_score} {away}{RESET}"
            f"{time_str} | {scoring_team} — {scorer_name}{tag_str}{assist_str}"
        )

    def _goal_tags(self, pbp, goal) -> list:
        """["PP"], ["SH"], ["EN"] or a combination (e.g. ["SH", "EN"]),
        empty for an ordinary even-strength goal. Decoded from the goal
        play's situationCode - four digits: away goalie, away skaters,
        home skaters, home goalie (1 = goalie in net, 0 = pulled) - so it
        costs no extra request. Checked against NHL's own per-goal
        strength/goalModifier labels (landing endpoint) on 250 goals
        across 45 games: 0 mismatches (46 power play, 3 shorthanded, 8
        empty net, the rest even strength).

        A pulled goalie counts as an extra attacker, not a skater
        advantage: a team scoring 6-on-5 with its own goalie out is even
        strength (NHL labels it that way), while 6-on-4 is a power play.
        Shootout goals get no tag (they're played at no strength), and a
        missing or malformed code just means no tag. Penalty shots aren't
        distinguishable in this feed (none appeared in the sample)."""
        if (goal.get("periodDescriptor") or {}).get("periodType") == "SO":
            return []
        raw = goal.get("situationCode")
        if isinstance(raw, int) and not isinstance(raw, bool):
            code = str(raw).zfill(4)  # a numeric code would have lost its leading zero ("0651" -> 651)
        else:
            code = raw if isinstance(raw, str) else ""
        if len(code) != 4 or not code.isdigit() or code == "0000":
            return []
        away_goalie, away_skaters, home_skaters, home_goalie = (int(d) for d in code)
        scored_by_home = (goal.get("details") or {}).get("eventOwnerTeamId") == (pbp.get("homeTeam") or {}).get("id")
        own_skaters, own_goalie, opp_skaters, opp_goalie = (
            (home_skaters, home_goalie, away_skaters, away_goalie)
            if scored_by_home else (away_skaters, away_goalie, home_skaters, home_goalie)
        )
        own_effective = own_skaters - (1 if own_goalie == 0 else 0)
        opp_effective = opp_skaters - (1 if opp_goalie == 0 else 0)

        tags = []
        if own_effective > opp_effective:
            tags.append("PP")
        elif own_effective < opp_effective:
            tags.append("SH")
        if opp_goalie == 0:
            tags.append("EN")
        return tags

    def _period_label(self, period_descriptor) -> str:
        period_type = period_descriptor.get("periodType")
        if period_type == "SO":
            return "SO"
        if period_type == "OT":
            return "OT"
        number = period_descriptor.get("number")
        return {1: "1st", 2: "2nd", 3: "3rd"}.get(number, f"period {number}" if number else "")

    def _is_ended(self, game) -> bool:
        return game.get("gameState") in ("FINAL", "OFF")

    def _announce_end(self, irc_bot, channel, pbp):
        home = self._team_name(pbp.get("homeTeam") or {})
        away = self._team_name(pbp.get("awayTeam") or {})
        home_score = (pbp.get("homeTeam") or {}).get("score", "?")
        away_score = (pbp.get("awayTeam") or {}).get("score", "?")

        last_period_type = (pbp.get("gameOutcome") or {}).get("lastPeriodType") or ""
        suffix = f" ({last_period_type})" if last_period_type in ("OT", "SO") else ""

        # Same "Yleisöä" wording as !liiga's FINAL line; left out whenever
        # the figure isn't available (see _fetch_attendance).
        attendance = self._fetch_attendance(pbp)
        attendance_str = f" | Yleisöä: {attendance}" if attendance else ""

        self._safe_send(
            irc_bot, channel,
            f"{self.FINAL_PREFIX} {home} {home_score}-{away_score} {away}{suffix}{attendance_str}",
        )

    def _fetch_attendance(self, pbp):
        """The game's attendance as an int, or None if it can't be had -
        the report may not exist yet right when a game ends (404), NHL may
        change the page, or the request may fail; none of those may stop
        the FINAL: line, which just goes out without it. One request per
        finished game."""
        season, game_id = pbp.get("season"), pbp.get("id")
        if not season or not game_id:
            return None
        url = f"{self.REPORTS_URL}/{season}/GS{str(game_id)[4:]}.HTM"
        try:
            resp = self.session.get(url, timeout=self.REQUEST_TIMEOUT_SECONDS, headers={"Accept": "text/html"})
            resp.raise_for_status()
            match = self.ATTENDANCE_RE.search(resp.text)
        except requests.exceptions.RequestException as e:
            print(f"NHL game report unavailable for game {game_id} (no attendance): {e}")
            return None
        if not match:
            return None
        digits = match.group(1).replace(",", "")
        return int(digits) if digits.isdigit() and int(digits) > 0 else None

    # ---- data fetching --------------------------------------------------

    def _seed_snapshot(self, game) -> dict:
        return {
            "home_id": (game.get("homeTeam") or {}).get("id"),
            "away_id": (game.get("awayTeam") or {}).get("id"),
            "announced": {},  # goal key -> event ids announced, see _resolve_goals()
            "ended": self._is_ended(game),
            # The schedule day (Eastern) this game belongs to - what the poll
            # keeps following, see _fetch_tracked_games().
            "slate": game.get("slateDate") or self._today_eastern_str(),
        }

    def _fetch_play_by_play(self, game_id):
        """Returns the raw play-by-play payload for one game (score,
        gameState, goal-by-goal detail, and the game's own roster all in
        one call), or None on failure."""
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/gamecenter/{game_id}/play-by-play",
                timeout=self.REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            resp.raise_for_status()
            return resp.json()
        except (ValueError, AttributeError, TypeError) as e:
            print(f"NHL API returned unexpected play-by-play data for game {game_id}: {e}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"NHL API play-by-play request failed for game {game_id}: {e}")
            return None

    def _fetch_schedule(self, date_str):
        """Returns the raw /v1/schedule/{date} payload (a 7-day window
        starting at date_str), or None on failure."""
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/schedule/{date_str}",
                timeout=self.REQUEST_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            resp.raise_for_status()
            return resp.json()
        except (ValueError, AttributeError, TypeError) as e:
            print(f"NHL API returned unexpected schedule data for date={date_str}: {e}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"NHL API schedule request failed for date={date_str}: {e}")
            return None

    def _today_eastern_str(self) -> str:
        # Schedule dates are bucketed by US Eastern time, not Helsinki -
        # confirmed live: a game starting 01:00 UTC (already the next UTC
        # calendar day) is still grouped under the previous day's Eastern
        # date. Fetching by a Helsinki-computed date would silently miss
        # or double-count games for a large chunk of the Helsinki day.
        return datetime.datetime.now(self.EASTERN_TZ).strftime("%Y-%m-%d")

    def _slates_by_date(self, data) -> dict:
        """{date: {game_id: game}} for every day in a schedule window. Each
        game gets a "slateDate" of its own so a snapshot can remember
        which day's slate it belongs to."""
        slates = {}
        for week in data.get("gameWeek") or []:
            date = week.get("date")
            slates[date] = {
                g["id"]: {**g, "slateDate": date}
                for g in (week.get("games") or []) if g.get("id") is not None
            }
        return slates

    def _fetch_today_games(self):
        """Returns {game_id: game_dict} for "today" (US Eastern), or None
        on failure. Also includes any game from the previous Eastern day
        that is still in progress: late starts (10 pm Eastern and after)
        run past Eastern midnight, and someone starting tracking then
        would otherwise see none of the game actually being played."""
        now = datetime.datetime.now(self.EASTERN_TZ)
        today_str = now.strftime("%Y-%m-%d")
        yesterday_str = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        data = self._fetch_schedule(yesterday_str)  # a 7-day window, so this covers today too
        if data is None:
            return None
        slates = self._slates_by_date(data)
        games = dict(slates.get(today_str, {}))
        for gid, game in slates.get(yesterday_str, {}).items():
            if game.get("gameState") in ("LIVE", "CRIT"):
                games[gid] = game
        return games

    def _fetch_tracked_games(self, prev_state):
        """Returns {game_id: game_dict} for the slate day(s) the tracked
        games belong to, or None on failure - NOT simply "today".

        Confirmed live (2026-09-29, #nhl.fi): the schedule is bucketed by
        Eastern date, and Eastern midnight lands in the middle of the
        late games (a 10 pm Eastern start is still being played at
        midnight). Polling "today's" slate at that moment silently
        replaced the tracked games with tomorrow's, so every goal and
        FINAL after midnight was lost and the tracker quietly moved on to
        the next day. Following the tracked games' own slate keeps them
        until they finish, and then the tracker ends, as it should."""
        dates = sorted({snap["slate"] for snap in prev_state.values() if snap.get("slate")})
        if not dates:
            return self._fetch_today_games()
        data = self._fetch_schedule(dates[0])  # a 7-day window from the earliest tracked day
        if data is None:
            return None
        slates = self._slates_by_date(data)
        games = {}
        for date in dates:
            games.update(slates.get(date, {}))
        return games

    def _helsinki_date_label(self, games, eastern_date_str) -> str:
        """The API's own gameWeek "date" is an Eastern calendar date - not
        safe to hand straight to _format_date_label() (which compares it
        against Helsinki's own "today"), since an Eastern evening game
        lands on the *following* Helsinki calendar date. Confirmed live:
        a slate the API buckets under Eastern "2026-09-29" landed
        entirely on Helsinki date 2026-09-30, so "!nhl next" was
        announcing "tomorrow" for a slate that was actually two days out
        (a real Tuesday-labeled-as-Wednesday mislabel reported live).
        Uses the *earliest* game's own Helsinki-converted date instead -
        falls back to the raw Eastern date if none of the games have a
        parseable start time (defensive, not observed live)."""
        starts = [
            dt for dt in (self._parse_start_dt(g.get("startTimeUTC")) for g in games.values())
            if dt is not None
        ]
        if not starts:
            return eastern_date_str
        return min(starts).strftime("%Y-%m-%d")

    def _fetch_next_gameday(self):
        """Returns (date_str, games_dict) for the closest date (today or
        later) that has games *not all already finished*, or (None, None)
        if that can't be determined (API unreachable, or genuinely
        nothing found within the search bound).

        Unlike liiga.fi (whose schedule call only ever returns one day at
        a time, needing a day-by-day loop to look ahead), this API's
        /schedule/{date} already returns a full 7-day window per call -
        so scanning forward a few *weeks* of calls covers the same ground
        Liiga's day-by-day loop covers in ~3 weeks of single-day calls.
        """
        date_str = self._today_eastern_str()
        max_weeks = max(1, -(-self.NEXT_SEARCH_MAX_DAYS // 7))  # ceil division

        for _ in range(max_weeks):
            data = self._fetch_schedule(date_str)
            if data is None:
                return None, None

            for week in data.get("gameWeek") or []:
                games = {g["id"]: g for g in (week.get("games") or []) if g.get("id") is not None}
                if games and not all(self._is_ended(g) for g in games.values()):
                    return self._helsinki_date_label(games, week["date"]), games

            next_start = data.get("nextStartDate")
            if not next_start or next_start <= date_str:
                break
            date_str = next_start

        return None, None
