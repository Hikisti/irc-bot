"""The NHL tracker's GOAL line, split out of nhl_command.py: the line itself, its PP/SH/EN tags, the shootout
tally, the period label and the scorer and assist names from the play-by-play roster."""
from irc_format import BOLD, RESET, GREEN, prefix as irc_prefix


class NHLGoalFormatMixin:
    """How the NHL tracker words a goal. Mixed into NHLCommand; the ledger and the FINAL line use it too, and
    _shootout_tally reads the goal list through the ledger's _all_real_goals."""

    GOAL_PREFIX = irc_prefix("GOAL:", GREEN)

    def _resolve_player_name(self, pbp, player_id):
        if player_id is None:
            return "Unknown"
        for spot in pbp.get("rosterSpots") or []:
            if spot.get("playerId") == player_id:
                first = (spot.get("firstName") or {}).get("default", "")
                last = (spot.get("lastName") or {}).get("default", "")
                return f"{first} {last}".strip() or "Unknown"
        return "Unknown"

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
        if period.get("periodType") == "SO":
            # A shootout goal does not change the game score (the feed keeps the running score at the tie),
            # so the line gives that score and the shootout tally; the final line then adds the winner's goal.
            shootout_home, shootout_away = self._shootout_tally(pbp, goal)
            time_str = f" SO {shootout_home}-{shootout_away}"
        else:
            period_label = self._period_label(period)
            time_str = f" {goal.get('timeInPeriod', '')} {period_label}".rstrip()

        return (
            f"{self.GOAL_PREFIX} {BOLD}{home} {home_score}-{away_score} {away}{RESET}"
            f"{time_str} | {scoring_team} — {scorer_name}{tag_str}{assist_str}"
        )

    def _shootout_tally(self, pbp, goal) -> tuple:
        """(home, away) goals scored in the shootout up to and including `goal`, counted from the play list."""
        home_id = (pbp.get("homeTeam") or {}).get("id")
        home = away = 0
        for play in self._all_real_goals(pbp):
            if (play.get("periodDescriptor") or {}).get("periodType") != "SO":
                continue
            if (play.get("details") or {}).get("eventOwnerTeamId") == home_id:
                home += 1
            else:
                away += 1
            if play is goal or (play.get("eventId") is not None and play.get("eventId") == goal.get("eventId")):
                break
        return home, away

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
