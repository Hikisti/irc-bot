"""Tests of the NHL tracker's GOAL line: its wording, tags, shootout tally and names (src/nhl_goal_format.py)."""
import pytest

from irc_format import BOLD, RESET
from tests.nhl_test_helpers import goal_play, make_pbp, roster_spot


class TestResolvePlayerName:
    def test_resolves_from_embedded_roster(self, nhl_command):
        pbp = make_pbp(roster=[roster_spot(100, "Aleksander", "Barkov")])
        assert nhl_command._resolve_player_name(pbp, 100) == "Aleksander Barkov"

    def test_unresolvable_id_falls_back_to_unknown(self, nhl_command):
        pbp = make_pbp(roster=[])
        assert nhl_command._resolve_player_name(pbp, 999) == "Unknown"

    def test_none_id_falls_back_to_unknown(self, nhl_command):
        assert nhl_command._resolve_player_name(make_pbp(), None) == "Unknown"


class TestFormatGoal:
    def test_regular_goal_with_assists(self, nhl_command):
        pbp = make_pbp(
            home_id=10, away_id=20, home_score=1, away_score=0,
            roster=[roster_spot(100, "Bradly", "Nadeau"), roster_spot(101, "Mike", "Reilly")],
        )
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, assist1=101, home_score=1, away_score=0)

        message = nhl_command._format_goal(pbp, goal)

        assert message == (
            f"{nhl_command.GOAL_PREFIX} {BOLD}Carolina Hurricanes 1-0 Florida Panthers{RESET}"
            " 04:31 1st | Carolina Hurricanes — Bradly Nadeau (assists: Mike Reilly)"
        )

    def test_away_goal_credits_away_team(self, nhl_command):
        pbp = make_pbp(home_id=10, away_id=20, roster=[roster_spot(200, "Sam", "Reinhart")])
        goal = goal_play(event_owner_team_id=20, scoring_player_id=200)

        message = nhl_command._format_goal(pbp, goal)
        assert "Florida Panthers — Sam Reinhart" in message

    def test_unresolvable_scorer_shows_unknown(self, nhl_command):
        pbp = make_pbp(home_id=10, roster=[])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=999)
        assert "— Unknown" in nhl_command._format_goal(pbp, goal)

    def test_no_assists_omits_the_assists_clause(self, nhl_command):
        pbp = make_pbp(home_id=10, roster=[roster_spot(100)])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100)
        assert "assists" not in nhl_command._format_goal(pbp, goal)

    def test_shootout_goal_shows_the_game_score_and_the_shootout_tally(self, nhl_command):
        """Issue #38: the game score stays at the tie, the tally says why the final reads 3-2."""
        goal = goal_play(event_id=7, event_owner_team_id=10, scoring_player_id=100, period_number=5, period_type="SO",
                         time_in_period="00:00", home_score=2, away_score=2)
        pbp = make_pbp(home_id=10, away_id=20, plays=[goal], roster=[roster_spot(100)])
        line = nhl_command._format_goal(pbp, goal)
        assert f"{BOLD}Carolina Hurricanes 2-2 Florida Panthers{RESET} SO 1-0 | Carolina Hurricanes — " in line
        assert "00:00" not in line

    def test_overtime_goal_shows_ot_label(self, nhl_command):
        pbp = make_pbp(home_id=10, roster=[roster_spot(100)])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, period_number=4, period_type="OT")
        assert " OT |" in nhl_command._format_goal(pbp, goal)


class TestShootoutTally:
    """Issue #38: the shootout tally is counted from the play list, in play order, up to the goal being announced."""

    def _so_goal(self, event_id, team, scorer=100):
        return goal_play(event_id=event_id, event_owner_team_id=team, scoring_player_id=scorer, period_number=5,
                         period_type="SO", time_in_period="00:00", home_score=2, away_score=2)

    def _pbp(self, *plays):
        regulation = goal_play(event_id=1, event_owner_team_id=10, home_score=1, away_score=0, scoring_player_id=100)
        return make_pbp(home_id=10, away_id=20, plays=[regulation, *plays], roster=[roster_spot(100)])

    def test_each_shootout_goal_shows_the_tally_so_far(self, nhl_command):
        goals = [self._so_goal(10, 10), self._so_goal(11, 20), self._so_goal(12, 20), self._so_goal(13, 10)]
        pbp = self._pbp(*goals)
        tallies = [nhl_command._format_goal(pbp, g).split(" | ")[0].split(" SO ")[1] for g in goals]
        assert tallies == ["1-0", "1-1", "1-2", "2-2"]

    def test_the_tally_is_home_then_away_whichever_team_scores(self, nhl_command):
        away_goal = self._so_goal(10, 20)
        assert " SO 0-1 | " in nhl_command._format_goal(self._pbp(away_goal), away_goal)

    def test_regulation_and_overtime_goals_are_not_counted_in_the_tally(self, nhl_command):
        overtime = goal_play(event_id=2, event_owner_team_id=20, period_number=4, period_type="OT", time_in_period="01:30",
                             home_score=1, away_score=1, scoring_player_id=100)
        goal = self._so_goal(10, 10)
        pbp = self._pbp(overtime, goal)
        assert " SO 1-0 | " in nhl_command._format_goal(pbp, goal)

    def test_the_tally_follows_play_order_not_event_ids(self, nhl_command):
        later = self._so_goal(5, 20)      # a lower id, but played second
        first = self._so_goal(99, 10)
        pbp = self._pbp(first, later)
        assert " SO 1-1 | " in nhl_command._format_goal(pbp, later)
        assert " SO 1-0 | " in nhl_command._format_goal(pbp, first)

    def test_a_goal_that_is_no_longer_in_the_play_list_gets_the_whole_tally(self, nhl_command):
        gone = self._so_goal(50, 10)
        assert " SO 1-0 | " in nhl_command._format_goal(self._pbp(self._so_goal(10, 10)), gone)

    def test_a_shootout_goal_without_an_event_id_is_found_by_identity(self, nhl_command):
        first, second = self._so_goal(None, 10), self._so_goal(None, 20)
        pbp = self._pbp(first, second)
        assert " SO 1-1 | " in nhl_command._format_goal(pbp, second)

    def test_other_goal_lines_are_unchanged(self, nhl_command):
        goal = goal_play(event_id=3, event_owner_team_id=10, period_number=4, period_type="OT", time_in_period="03:23",
                         home_score=2, away_score=1, scoring_player_id=100)
        line = nhl_command._format_goal(self._pbp(goal), goal)
        assert f"{RESET} 03:23 OT | " in line and " SO " not in line


class TestGoalTags:
    """PP / SH / EN, decoded from the goal play's situationCode (away goalie,
    away skaters, home skaters, home goalie) - validated against NHL's own
    per-goal labels on 250 real goals with 0 mismatches. Home team id is 10,
    away 20 in make_pbp()."""

    def _tags(self, nhl_command, code, scored_by_home, period_type="REG"):
        pbp = make_pbp(home_id=10, away_id=20)
        goal = goal_play(event_owner_team_id=10 if scored_by_home else 20,
                         period_type=period_type, situation_code=code)
        return nhl_command._goal_tags(pbp, goal)

    @pytest.mark.parametrize("code, by_home, period_type, expected", [
        ("1551", True, "REG", []),            # five on five
        ("1451", False, "REG", ["SH"]),       # real EDM-VAN goal: away scored 4-on-5
        ("1541", False, "REG", ["PP"]),       # real EDM-VAN goal: away scored 5-on-4
        ("1541", True, "REG", ["SH"]),        # same code, scored by the team with 4 skaters
        ("1460", True, "REG", ["PP"]),        # real: home goalie pulled, 6-on-4 - NHL calls it a power play
        ("1331", False, "OT", []),            # three on three overtime
        ("1441", True, "REG", []),            # four on four
        ("0651", True, "REG", ["EN"]),        # away pulled the goalie; home scores into the empty net
        ("0641", True, "REG", ["SH", "EN"]),  # ...while shorthanded
        ("0651", False, "REG", []),           # the pulled-goalie team itself scoring 6-on-5: even strength
        ("1541", True, "SO", []),             # shootout goals get no tag whatever the code says
    ])
    def test_decoding(self, nhl_command, code, by_home, period_type, expected):
        assert self._tags(nhl_command, code, by_home, period_type) == expected

    def test_numeric_code_that_lost_its_leading_zero_is_restored(self, nhl_command):
        assert self._tags(nhl_command, 651, True) == ["EN"]  # int 651 is really "0651"

    @pytest.mark.parametrize("code", [None, "", "abc", "155", "15511", "0000", True, 1.5])
    def test_missing_or_malformed_code_means_no_tag(self, nhl_command, code):
        assert self._tags(nhl_command, code, True) == []

    def test_a_goal_without_any_code_is_untagged(self, nhl_command):
        pbp = make_pbp(home_id=10)
        assert nhl_command._goal_tags(pbp, goal_play(event_owner_team_id=10)) == []

    def test_tag_appears_between_scorer_and_assists(self, nhl_command):
        pbp = make_pbp(home_id=10, away_id=20, roster=[roster_spot(100, "Evan", "Bouchard"), roster_spot(101, "Connor", "McDavid")])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, assist1=101, situation_code="1460")
        assert nhl_command._format_goal(pbp, goal).endswith("— Evan Bouchard (PP) (assists: Connor McDavid)")

    def test_combined_tags_are_slash_joined_like_liiga(self, nhl_command):
        pbp = make_pbp(home_id=10, away_id=20, roster=[roster_spot(100, "Evan", "Bouchard")])
        goal = goal_play(event_owner_team_id=10, scoring_player_id=100, situation_code="0641")
        assert nhl_command._format_goal(pbp, goal).endswith("— Evan Bouchard (SH/EN)")
