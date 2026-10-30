"""Tests of the NHL tracker's goal ledger and retractions (src/nhl_goal_ledger.py)."""
from unittest.mock import MagicMock, patch

import pytest

from irc_format import RESET
from tests.nhl_test_helpers import (
    goal_play, make_pbp, make_schedule_game, penalty_play, review_play, roster_spot,
    seeded, stoppage_play,
)


class TestRealGoals:
    def test_filters_by_team_and_requires_a_scorer(self, nhl_command):
        pbp = make_pbp(home_id=10, away_id=20, plays=[
            goal_play(event_owner_team_id=10, scoring_player_id=100),
            goal_play(event_owner_team_id=20, scoring_player_id=200),
            # Not a goal at all.
            {"typeDescKey": "shot-on-goal", "details": {"eventOwnerTeamId": 10}},
            # A goal-typed play with no scorer - shouldn't count as real.
            {"typeDescKey": "goal", "details": {"eventOwnerTeamId": 10}},
        ])
        home_goals = nhl_command._real_goals(pbp, "homeTeam")
        away_goals = nhl_command._real_goals(pbp, "awayTeam")
        assert len(home_goals) == 1
        assert len(away_goals) == 1

    def test_shootout_goals_are_included(self, nhl_command):
        pbp = make_pbp(home_id=10, plays=[
            goal_play(period_number=5, period_type="SO", event_owner_team_id=10, home_score=2, away_score=2),
        ])
        assert len(nhl_command._real_goals(pbp, "homeTeam")) == 1


class TestGoalOrder:
    """Several goals seen in one poll must come out in the order they were
    scored, both teams interleaved. They used to be announced one team at a
    time (home's goals, then away's) - visible when a tracker recovered a
    stretch of a game and a 2-2 line landed before the 0-1 that came first.
    Home team id is 10, away 20 in make_pbp()."""

    def _pbp(self, plays):
        roster = [roster_spot(1, "Home", "First"), roster_spot(2, "Away", "Second"),
                  roster_spot(3, "Home", "Third"), roster_spot(4, "Away", "Fourth")]
        return make_pbp(game_id=1, home_id=10, away_id=20, roster=roster, plays=plays)

    def _plays(self):
        return [
            goal_play(event_id=1, event_owner_team_id=10, scoring_player_id=1, home_score=1, away_score=0),
            goal_play(event_id=2, event_owner_team_id=20, scoring_player_id=2, home_score=1, away_score=1),
            goal_play(event_id=3, event_owner_team_id=10, scoring_player_id=3, home_score=2, away_score=1),
            goal_play(event_id=4, event_owner_team_id=20, scoring_player_id=4, home_score=2, away_score=2),
        ]

    def _announced(self, nhl_command, pbp, announced):
        bot = MagicMock()
        nhl_command._announce_new_goals(bot, "#nhl.fi", pbp, announced)
        return [call.args[1] for call in bot.send_message.call_args_list]

    def test_goals_come_out_in_the_order_they_were_scored_across_both_teams(self, nhl_command):
        messages = self._announced(nhl_command, self._pbp(self._plays()), {})

        scorers = [next(name for name in ("First", "Second", "Third", "Fourth") if name in m) for m in messages]
        assert scorers == ["First", "Second", "Third", "Fourth"]  # not First, Third, Second, Fourth
        assert "1-0" in messages[0] and "1-1" in messages[1] and "2-1" in messages[2] and "2-2" in messages[3]

    def test_only_goals_not_announced_yet_are_new_and_still_ordered(self, nhl_command):
        # the first goal (home, 1-0) is already announced: new are Second (away), Third (home), Fourth (away)
        messages = self._announced(nhl_command, self._pbp(self._plays()), {("score", 10, 1, 0): seeded(1)})

        scorers = [next(name for name in ("First", "Second", "Third", "Fourth") if name in m) for m in messages]
        assert scorers == ["Second", "Third", "Fourth"]

    def test_nothing_new_announces_nothing(self, nhl_command):
        everything = nhl_command._resolve_goals(self._pbp(self._plays()), {})[2]
        assert self._announced(nhl_command, self._pbp(self._plays()), everything) == []

    def test_a_poll_announces_a_multi_goal_backlog_in_game_order(self, nhl_command):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        }}
        game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="LIVE")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=self._pbp(self._plays())):
            nhl_command._poll_once(bot, "#nhl.fi")

        scores = [call.args[1].split("|")[0] for call in bot.send_message.call_args_list]
        assert ["1-0" in scores[0], "1-1" in scores[1], "2-1" in scores[2], "2-2" in scores[3]] == [True] * 4


class TestAnnouncedGoalRecord:
    """Goals are remembered by (team, running score) plus event id, not by a
    count per team (issue #20). Each case below was seen in a real feed."""

    HOME, AWAY = 10, 20

    def _pbp(self, *plays):
        roster = [roster_spot(i, f"P{i}", "X") for i in range(1, 9)]
        return make_pbp(game_id=1, home_id=self.HOME, away_id=self.AWAY, plays=list(plays), roster=roster)

    def _goal(self, event_id, team, home, away, scorer=1, **kw):
        return goal_play(event_id=event_id, event_owner_team_id=team, scoring_player_id=scorer,
                         home_score=home, away_score=away, **kw)

    def _poll_sequence(self, nhl_command, feeds, all_lines=False):
        """Runs one poll per feed and returns every GOAL: line sent (every line
        sent with all_lines)."""
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": self.HOME, "away_id": self.AWAY, "announced": {}, "ended": False},
        }}
        game = make_schedule_game(gid=1, home_id=self.HOME, away_id=self.AWAY, game_state="LIVE")
        for feed in feeds:
            # _fetch_tracked_games, not _fetch_today_games: after the first poll the
            # stored games carry their slate date and the schedule is fetched by it.
            with patch.object(nhl_command, "_fetch_tracked_games", return_value={1: game}), \
                 patch.object(nhl_command, "_fetch_play_by_play", return_value=feed) as fetch:
                nhl_command._poll_once(bot, "#nhl.fi")
            assert fetch.call_count == 1  # every poll in the sequence really ran
        lines = [c.args[1] for c in bot.send_message.call_args_list]
        if all_lines:
            return lines
        return [line for line in lines if "GOAL:" in line and "NO GOAL:" not in line]

    # -- a goal that vanishes for a poll and comes back (same id) ------------

    def test_goal_that_disappears_for_a_poll_and_returns_is_announced_once(self, nhl_command):
        goal = self._goal(155, self.AWAY, 0, 1, scorer=1, assist1=2, assist2=3)
        lines = self._poll_sequence(nhl_command, [self._pbp(goal), self._pbp(), self._pbp(goal)])
        assert len(lines) == 1

    def test_the_same_with_different_assists_on_the_way_back(self, nhl_command):
        first = self._goal(155, self.AWAY, 0, 1, scorer=1, assist1=2, assist2=3)
        back = self._goal(155, self.AWAY, 0, 1, scorer=1, assist1=4)
        lines = self._poll_sequence(nhl_command, [self._pbp(first), self._pbp(), self._pbp(back)])
        assert len(lines) == 1

    # -- the same goal listed twice with two scorers, one of them renumbered (#68) -------

    def _overtime_pair(self):
        """The real sequence of 2026-10-29: the decisive overtime goal (home side, 3-4) is announced; then the
        feed lists a second overtime goal by another player 3 s earlier (3-4) and renumbers the first to 3-5;
        a poll later both are listed at 3-4; then the second is gone."""
        winner = self._goal(1142, self.HOME, 4, 3, scorer=1, period_number=4, period_type="OT", time_in_period="03:27")
        winner_renumbered = self._goal(1142, self.HOME, 5, 3, scorer=1, period_number=4, period_type="OT",
                                       time_in_period="03:27")
        other = self._goal(1141, self.HOME, 4, 3, scorer=2, period_number=4, period_type="OT", time_in_period="03:24")
        return winner, winner_renumbered, other

    def test_an_overtime_goal_listed_twice_with_two_scorers_is_announced_once(self, nhl_command, capsys):
        winner, renumbered, other = self._overtime_pair()
        feeds = [self._pbp(winner), self._pbp(other, renumbered), self._pbp(other, winner), self._pbp(winner)]
        lines = self._poll_sequence(nhl_command, feeds)
        assert len(lines) == 1 and "P1" in lines[0] and "4-3" in lines[0]  # no 3-4 by the other player, no 3-5
        assert "not announcing it yet" in capsys.readouterr().out

    def test_the_same_when_the_feed_only_ever_shows_the_renumbered_pair_once(self, nhl_command):
        winner, renumbered, other = self._overtime_pair()
        lines = self._poll_sequence(nhl_command, [self._pbp(winner), self._pbp(other, renumbered), self._pbp(winner)])
        assert len(lines) == 1

    def test_the_order_of_the_plays_in_the_feed_does_not_matter(self, nhl_command):
        winner, renumbered, other = self._overtime_pair()
        lines = self._poll_sequence(nhl_command, [self._pbp(winner), self._pbp(renumbered, other), self._pbp(winner, other)])
        assert len(lines) == 1

    def test_a_goal_inserted_before_an_announced_one_is_announced_one_poll_late_not_lost(self, nhl_command):
        """The same feed shape for a goal that really exists (the feed missed it and adds it earlier in the game, so
        the announced goal moves to the next running score): held for one poll, then announced."""
        winner, renumbered, other = self._overtime_pair()
        lines = self._poll_sequence(nhl_command, [self._pbp(winner), self._pbp(other, renumbered),
                                                  self._pbp(other, renumbered)])
        assert len(lines) == 2 and "P1" in lines[0] and "P2" in lines[1]

    def test_a_replacement_when_the_old_entry_is_gone_is_still_announced_at_once(self, nhl_command):
        # the first scorer named was wrong: the old entry is removed and the new one (other scorer) takes its place
        old = self._goal(1142, self.HOME, 4, 3, scorer=1, period_number=4, period_type="OT", time_in_period="03:27")
        new = self._goal(1141, self.HOME, 4, 3, scorer=2, period_number=4, period_type="OT", time_in_period="03:24")
        lines = self._poll_sequence(nhl_command, [self._pbp(old), self._pbp(new)])
        assert len(lines) == 2 and "P2" in lines[1]

    # -- the same goal listed under two ids ----------------------------------

    def test_two_plays_for_one_goal_in_the_same_poll_are_announced_once_the_first_listed(self, nhl_command):
        a = self._goal(278, self.AWAY, 0, 2, scorer=5)
        b = self._goal(279, self.AWAY, 0, 2, scorer=6)
        lines = self._poll_sequence(nhl_command, [self._pbp(a, b)])
        assert len(lines) == 1 and "P5" in lines[0]

    def test_a_duplicate_listed_before_the_announced_play_is_ignored(self, nhl_command):
        # the real sequence: play 279 announced, then 278 appears *before* it in the list
        later, earlier = self._goal(279, self.AWAY, 0, 2, scorer=6), self._goal(278, self.AWAY, 0, 2, scorer=5)
        lines = self._poll_sequence(nhl_command, [self._pbp(later), self._pbp(earlier, later)])
        assert len(lines) == 1 and "P6" in lines[0]  # the old behaviour announced 279 a second time

    def test_a_duplicate_listed_after_the_announced_play_is_ignored(self, nhl_command):
        first, second = self._goal(278, self.AWAY, 0, 2, scorer=5), self._goal(279, self.AWAY, 0, 2, scorer=6)
        lines = self._poll_sequence(nhl_command, [self._pbp(first), self._pbp(first, second)])
        assert len(lines) == 1

    def test_when_the_feed_drops_the_duplicate_nothing_is_announced(self, nhl_command):
        a, b = self._goal(278, self.AWAY, 0, 2, scorer=5), self._goal(279, self.AWAY, 0, 2, scorer=6)
        lines = self._poll_sequence(nhl_command, [self._pbp(b), self._pbp(a, b), self._pbp(a)])
        assert len(lines) == 1

    # -- a removed goal followed by a real one with the same running score ----

    def test_goal_after_a_disallowed_one_with_the_same_running_score_is_announced(self, nhl_command):
        disallowed = self._goal(1, self.AWAY, 0, 2, scorer=1)   # announced, then removed
        real = self._goal(2, self.AWAY, 0, 2, scorer=2)         # the team's next goal: same running score
        lines = self._poll_sequence(nhl_command, [self._pbp(disallowed), self._pbp(), self._pbp(real)])
        assert len(lines) == 2

    def test_a_removal_and_the_next_goal_in_the_same_poll_is_not_swallowed(self, nhl_command):
        disallowed = self._goal(1, self.AWAY, 0, 1, scorer=1)
        real = self._goal(2, self.AWAY, 0, 1, scorer=2)
        lines = self._poll_sequence(nhl_command, [self._pbp(disallowed), self._pbp(real)])
        assert len(lines) == 2  # the old per-team count saw 1 -> 1 and announced nothing

    # -- identity edge cases ---------------------------------------------------

    def test_a_goal_whose_running_score_was_renumbered_but_whose_id_is_known_is_not_repeated(self, nhl_command):
        before = self._goal(7, self.AWAY, 0, 3)
        renumbered = self._goal(7, self.AWAY, 0, 2)
        lines = self._poll_sequence(nhl_command, [self._pbp(before), self._pbp(renumbered)])
        assert len(lines) == 1

    def test_shootout_goals_of_one_team_are_each_announced(self, nhl_command):
        # confirmed live: every shootout goal carries the same running score and clock 00:00
        so1 = self._goal(1253, self.HOME, 2, 2, scorer=1, period_number=5, period_type="SO", time_in_period="00:00")
        so2 = self._goal(1258, self.HOME, 2, 2, scorer=2, period_number=5, period_type="SO", time_in_period="00:00")
        lines = self._poll_sequence(nhl_command, [self._pbp(so1), self._pbp(so1, so2)])
        assert len(lines) == 2

    def test_missing_running_score_falls_back_to_the_event_id(self, nhl_command):
        one = self._goal(1, self.AWAY, None, None)
        two = self._goal(2, self.AWAY, None, None, scorer=2)
        lines = self._poll_sequence(nhl_command, [self._pbp(one), self._pbp(one, two), self._pbp(one, two)])
        assert len(lines) == 2

    def test_missing_running_score_and_id_falls_back_to_period_and_clock(self, nhl_command):
        a = self._goal(None, self.AWAY, None, None, time_in_period="05:00")
        b = self._goal(None, self.AWAY, None, None, scorer=2, time_in_period="09:00")
        del a["eventId"], b["eventId"]
        lines = self._poll_sequence(nhl_command, [self._pbp(a), self._pbp(a, b), self._pbp(a, b)])
        assert len(lines) == 2

    def test_goals_of_both_teams_with_the_same_running_score_digits_are_distinct(self, nhl_command):
        home = self._goal(1, self.HOME, 1, 0)
        away = self._goal(2, self.AWAY, 1, 1)
        assert len(self._poll_sequence(nhl_command, [self._pbp(home, away)])) == 2

    # -- record handling --------------------------------------------------------

    def test_the_stored_record_is_not_mutated_in_place(self, nhl_command):
        goal = self._goal(1, self.AWAY, 0, 1)
        stored = {}
        nhl_command._resolve_goals(self._pbp(goal), stored)
        assert stored == {}

    def test_seeding_records_both_ids_of_a_doubly_listed_goal(self, nhl_command):
        a, b = self._goal(278, self.AWAY, 0, 2), self._goal(279, self.AWAY, 0, 2)
        record = nhl_command._resolve_goals(self._pbp(a, b), {}, seed=True)[2]
        assert record == {("score", self.AWAY, 0, 2): seeded(278, 279)}

    def test_a_goal_already_in_the_feed_when_tracking_started_is_never_announced(self, nhl_command):
        goal = self._goal(1, self.AWAY, 0, 1)
        record = nhl_command._resolve_goals(self._pbp(goal), {}, seed=True)[2]
        assert nhl_command._resolve_goals(self._pbp(goal), record)[0] == []


class TestRetraction:
    """A goal that was announced and then stays out of the feed is retracted
    (issue #13, stage 1). Cases are the real ones seen over three nights."""

    HOME, AWAY = 10, 20

    def _pbp(self, *plays):
        roster = [roster_spot(i, f"P{i}", "X") for i in range(1, 9)]
        return make_pbp(game_id=1, home_id=self.HOME, away_id=self.AWAY, plays=list(plays), roster=roster)

    def _quiet(self, *plays):
        """A feed with other plays in it but none of the goals under test."""
        return self._pbp({"typeDescKey": "faceoff", "eventId": 900, "periodDescriptor": {"number": 3, "periodType": "REG"},
                          "timeInPeriod": "01:00", "details": {}}, *plays)

    def _goal(self, event_id=1, team=None, home=0, away=1, scorer=1, **kw):
        kw.setdefault("time_in_period", "05:25")
        kw.setdefault("period_number", 3)
        return goal_play(event_id=event_id, event_owner_team_id=team or self.AWAY, scoring_player_id=scorer,
                         home_score=home, away_score=away, **kw)

    def _run(self, nhl_command, feeds, seeded_record=None):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": self.HOME, "away_id": self.AWAY, "announced": seeded_record or {}, "ended": False},
        }}
        game = make_schedule_game(gid=1, home_id=self.HOME, away_id=self.AWAY, game_state="LIVE")
        for feed in feeds:
            with patch.object(nhl_command, "_fetch_tracked_games", return_value={1: game}), \
                 patch.object(nhl_command, "_fetch_play_by_play", return_value=feed):
                nhl_command._poll_once(bot, "#nhl.fi")
        return [c.args[1] for c in bot.send_message.call_args_list]

    def _retractions(self, lines):
        return [line for line in lines if "NO GOAL:" in line]

    def _goals(self, lines):
        return [line for line in lines if "GOAL:" in line and "NO GOAL:" not in line]

    # -- when -----------------------------------------------------------------

    def test_a_goal_gone_for_three_polls_is_not_retracted_and_not_repeated_when_it_returns(self, nhl_command):
        goal = self._goal()
        lines = self._run(nhl_command, [self._pbp(goal), self._quiet(), self._quiet(), self._quiet(), self._pbp(goal)])
        assert self._retractions(lines) == [] and len(self._goals(lines)) == 1

    def test_the_miss_counter_starts_over_when_the_goal_returns(self, nhl_command):
        goal = self._goal()
        gap = [self._quiet()] * 3
        lines = self._run(nhl_command, [self._pbp(goal)] + gap + [self._pbp(goal)] + gap)
        assert self._retractions(lines) == []  # 3 + 3 missed polls, never 4 in a row

    def test_a_goal_gone_for_four_polls_is_retracted_once(self, nhl_command):
        goal = self._goal()
        lines = self._run(nhl_command, [self._pbp(goal)] + [self._quiet()] * 7)
        assert len(self._retractions(lines)) == 1

    def test_the_retraction_names_the_goal_and_the_current_score(self, nhl_command):
        goal = self._goal(scorer=1)
        lines = self._run(nhl_command, [self._pbp(goal)] + [self._quiet()] * 4)
        text = self._retractions(lines)[0]
        assert "NO GOAL:" in text
        assert "Carolina Hurricanes 0-0 Florida Panthers" in text
        # clock and period right after the score, as in the GOAL: line; never "the 3rd goal"
        assert f"Florida Panthers{RESET} 05:25 3rd | P1 X (Florida Panthers) was disallowed" in text
        assert "the 05:25" not in text
        assert "(" not in text.split("disallowed", 1)[1]  # no explanation without a stoppage

    def test_a_goal_without_a_clock_or_period_label_has_no_double_space(self, nhl_command):
        posted = {"team": "Florida Panthers", "scorer_name": "P1 X", "label": "", "period": 3, "clock": ""}
        text = nhl_command._format_retraction(self._quiet(), posted)
        assert f"Florida Panthers{RESET} | P1 X (Florida Panthers) was disallowed" in text

    def test_a_goal_that_returns_after_being_retracted_is_announced_again(self, nhl_command):
        goal = self._goal()
        lines = self._run(nhl_command, [self._pbp(goal)] + [self._quiet()] * 4 + [self._pbp(goal)])
        assert len(self._retractions(lines)) == 1 and len(self._goals(lines)) == 2

    def test_fetch_failures_do_not_count_as_missing(self, nhl_command):
        goal = self._goal()
        lines = self._run(nhl_command, [self._pbp(goal), self._quiet(), self._quiet(), None, None, None, None, self._quiet()])
        assert self._retractions(lines) == []  # only three real misses

    def test_goals_already_in_the_feed_when_tracking_started_are_never_retracted(self, nhl_command):
        record = {("score", self.AWAY, 0, 1): seeded(1)}
        assert self._retractions(self._run(nhl_command, [self._quiet()] * 8, seeded_record=record)) == []

    def test_the_resolver_reports_nothing_gone_for_goals_that_were_only_seeded(self, nhl_command):
        record = {("score", self.AWAY, 0, 1): seeded(1)}
        for _ in range(8):
            _, gone, record = nhl_command._resolve_goals(self._quiet(), record)
            assert gone == []

    def test_a_renumbered_goal_is_not_mistaken_for_a_missing_one(self, nhl_command):
        before, after = self._goal(7, away=3), self._goal(7, away=2)
        lines = self._run(nhl_command, [self._pbp(before)] + [self._pbp(after)] * 6)
        assert self._retractions(lines) == [] and len(self._goals(lines)) == 1

    # -- replaced by the next goal -----------------------------------------------

    def test_a_goal_replaced_at_once_is_announced_without_calling_the_old_one_disallowed(self, nhl_command):
        # real case: a first entry by player A, removed 20 s later, and 20 s after that the same goal
        # (same team, same running score) under player B: a correction, not a disallowed goal
        old, new = self._goal(1, scorer=1), self._goal(2, scorer=2)
        lines = self._run(nhl_command, [self._pbp(old), self._pbp(new)])
        assert len(lines) == 2 and "P1 X" in lines[0] and "P2 X" in lines[1]
        assert self._retractions(lines) == []

    def test_the_same_goal_re_keyed_with_a_new_id_and_clock_is_not_announced_again(self, nhl_command, capsys):
        # real case (#60): removed and re-listed in the same poll, a new id, the clock a second earlier
        old = self._goal(1, scorer=1, time_in_period="13:21")
        new = self._goal(2, scorer=1, time_in_period="13:20", assist1=7)
        lines = self._run(nhl_command, [self._pbp(old), self._pbp(new), self._pbp(new)])
        assert len(self._goals(lines)) == 1 and self._retractions(lines) == []
        assert "same scorer and period, not announced again" in capsys.readouterr().out

    def test_the_same_scorer_in_another_period_at_the_same_score_is_announced(self, nhl_command):
        old = self._goal(1, scorer=1, period_number=1)
        new = self._goal(2, scorer=1, period_number=2)
        assert len(self._goals(self._run(nhl_command, [self._pbp(old), self._pbp(new)]))) == 2

    def test_a_replacement_after_a_miss_or_two_is_still_not_a_retraction(self, nhl_command):
        old, new = self._goal(1, scorer=1), self._goal(2, scorer=2)
        lines = self._run(nhl_command, [self._pbp(old), self._quiet(), self._quiet(), self._pbp(new)])
        assert self._retractions(lines) == [] and len(self._goals(lines)) == 2

    def test_a_goal_after_one_that_was_retracted_on_its_own_is_announced_and_nothing_else_is_said(self, nhl_command):
        disallowed, next_goal = self._goal(1, scorer=1), self._goal(2, scorer=2, time_in_period="15:00")
        feeds = [self._pbp(disallowed)] + [self._quiet()] * 4 + [self._pbp(next_goal)]
        lines = self._run(nhl_command, feeds)
        assert len(self._retractions(lines)) == 1 and len(self._goals(lines)) == 2  # one retraction, from the misses

    def test_two_goals_gone_at_once_while_another_goal_is_still_there_give_two_retractions(self, nhl_command):
        a, b = self._goal(1, away=1), self._goal(2, home=1, away=1, team=self.HOME, scorer=2, time_in_period="08:00")
        keep = self._goal(3, home=1, away=2, scorer=3, time_in_period="12:00")
        lines = self._run(nhl_command, [self._pbp(a, b, keep)] + [self._quiet(keep)] * 4)
        assert len(self._retractions(lines)) == 2

    # -- a broken feed is not a run of disallowed goals ---------------------------------

    def test_an_empty_feed_does_not_retract_anything(self, nhl_command, capsys):
        a, b = self._goal(1, away=1), self._goal(2, home=1, away=1, team=self.HOME, scorer=2, time_in_period="08:00")
        lines = self._run(nhl_command, [self._pbp(a, b)] + [self._pbp()] * 8)
        assert self._retractions(lines) == []
        assert "the feed shows no goal for 2 announced one(s), not counting this poll as a miss" in capsys.readouterr().out

    def test_an_empty_feed_does_not_retract_a_single_goal_either(self, nhl_command):
        assert self._retractions(self._run(nhl_command, [self._pbp(self._goal())] + [self._pbp()] * 8)) == []

    def test_two_goals_vanishing_together_with_no_goal_left_are_not_retracted(self, nhl_command):
        a, b = self._goal(1, away=1), self._goal(2, home=1, away=1, team=self.HOME, scorer=2, time_in_period="08:00")
        lines = self._run(nhl_command, [self._pbp(a, b)] + [self._quiet()] * 8 + [self._pbp(a, b)])
        assert self._retractions(lines) == [] and len(self._goals(lines)) == 2  # nothing repeated when they return

    def test_polls_skipped_as_broken_do_not_reset_the_count(self, nhl_command):
        goal = self._goal()
        feeds = [self._pbp(goal)] + [self._quiet()] * 2 + [self._pbp()] * 3 + [self._quiet()] * 2
        assert len(self._retractions(self._run(nhl_command, feeds))) == 1  # 2 + 2 real misses

    def test_the_label_of_an_overtime_goal(self, nhl_command):
        goal = self._goal(period_number=4, period_type="OT", time_in_period="03:18")
        text = self._retractions(self._run(nhl_command, [self._pbp(goal)] + [self._quiet()] * 4))[0]
        assert f"{RESET} 03:18 OT | P1 X" in text

    # -- the score shown ------------------------------------------------------------

    def test_the_score_is_the_running_score_of_the_last_remaining_goal(self, nhl_command):
        first = self._goal(1, home=1, away=0, team=self.HOME, scorer=2, time_in_period="03:00")
        removed = self._goal(2, home=1, away=1, scorer=1)
        later = self._goal(3, home=2, away=1, team=self.HOME, scorer=3, time_in_period="09:00")
        feeds = [self._pbp(first, removed)] + [self._quiet(first)] * 4
        assert "Carolina Hurricanes 1-0 Florida Panthers" in self._retractions(self._run(nhl_command, feeds))[0]
        # a later goal is still there when the earlier one is retracted: the score shown is that goal's
        feeds = [self._pbp(first, removed, later)] + [self._pbp(first, later)] * 4
        assert "Carolina Hurricanes 2-1 Florida Panthers" in self._retractions(self._run(nhl_command, feeds))[0]

    def test_without_a_running_score_the_header_score_is_used(self, nhl_command):
        goal = self._goal()
        other = self._goal(2, home=None, away=None, scorer=2, time_in_period="08:00")
        pbp = self._pbp(other)
        pbp["homeTeam"]["score"], pbp["awayTeam"]["score"] = 4, 5
        feeds = [self._pbp(goal, other)] + [pbp] * 4
        assert "Carolina Hurricanes 4-5 Florida Panthers" in self._retractions(self._run(nhl_command, feeds))[0]

    # -- the explanation ----------------------------------------------------------------

    def _reason_text(self, nhl_command, *extra_plays):
        goal = self._goal(time_in_period="05:25", period_number=3)
        return self._retractions(self._run(nhl_command, [self._pbp(goal)] + [self._pbp(*extra_plays)] * 4))[0]

    def test_offside_challenge_even_half_a_minute_before_the_goals_clock(self, nhl_command):
        # real case: the stoppage sat at 04:54, the goal at 05:25
        text = self._reason_text(nhl_command, stoppage_play("chlg-vis-off-side", "04:54", 3))
        assert text.endswith("was disallowed (offside challenge)")

    def test_goaltender_interference_challenge_just_after_the_goals_clock(self, nhl_command):
        text = self._reason_text(nhl_command, stoppage_play("chlg-hm-goal-interference", "05:27", 3))
        assert text.endswith("(goaltender interference challenge)")

    def test_an_unknown_challenge_reason_is_just_a_challenge(self, nhl_command):
        text = self._reason_text(nhl_command, stoppage_play("chlg-hm-something-new", "05:25", 3))
        assert text.endswith("(challenge)")

    def test_a_stoppage_that_is_not_a_challenge_explains_nothing(self, nhl_command):
        assert not self._reason_text(nhl_command, stoppage_play("icing", "05:25", 3)).endswith(")")

    def test_a_challenge_too_far_from_the_goal_or_in_another_period_explains_nothing(self, nhl_command):
        assert not self._reason_text(nhl_command, stoppage_play("chlg-hm-off-side", "03:00", 3)).endswith(")")
        assert not self._reason_text(nhl_command, stoppage_play("chlg-hm-off-side", "05:00", 2)).endswith(")")
        assert not self._reason_text(nhl_command, stoppage_play("chlg-hm-off-side", "05:40", 3)).endswith(")")

    def test_a_failed_challenge_is_not_the_reason(self, nhl_command):
        # a failed challenge costs the challenging team a bench penalty and the goal stands
        text = self._reason_text(nhl_command, stoppage_play("chlg-vis-off-side", "05:25", 3),
                                 penalty_play("delaying-game-unsuccessful-challenge", "05:25", 3))
        assert not text.endswith(")")

    def test_the_nearest_challenge_wins(self, nhl_command):
        text = self._reason_text(nhl_command, stoppage_play("chlg-hm-goal-interference", "04:40", 3),
                                 stoppage_play("chlg-hm-off-side", "05:20", 3))
        assert text.endswith("(offside challenge)")

    def test_a_challenge_recorded_only_in_the_secondary_reason_is_the_reason(self, nhl_command):
        # real case (2026-10-15): the stoppage 3 s before the goal's clock, the generic referee reason in "reason"
        # and the challenge code only in "secondaryReason"
        text = self._reason_text(nhl_command, stoppage_play("referee-or-linesman", "05:22", 3, secondary="chlg-vis-off-side"))
        assert text.endswith("was disallowed (offside challenge)")

    def test_the_secondary_field_gives_the_kind_of_challenge_too(self, nhl_command):
        assert self._reason_text(nhl_command, stoppage_play("referee-or-linesman", "05:25", 3,
                                                            secondary="chlg-hm-goal-interference")).endswith("(goaltender interference challenge)")
        assert self._reason_text(nhl_command, stoppage_play("referee-or-linesman", "05:25", 3,
                                                            secondary="chlg-hm-something-new")).endswith("(challenge)")

    def test_a_challenge_code_in_the_main_field_wins_over_another_secondary_reason(self, nhl_command):
        text = self._reason_text(nhl_command, stoppage_play("chlg-hm-off-side", "05:25", 3, secondary="tv-timeout"))
        assert text.endswith("(offside challenge)")

    def test_with_a_challenge_code_in_both_fields_the_main_one_decides(self, nhl_command):
        text = self._reason_text(nhl_command, stoppage_play("chlg-hm-off-side", "05:25", 3, secondary="chlg-hm-goal-interference"))
        assert text.endswith("(offside challenge)")

    def test_a_failed_challenge_in_the_secondary_field_is_not_the_reason(self, nhl_command):
        text = self._reason_text(nhl_command, stoppage_play("referee-or-linesman", "05:25", 3, secondary="chlg-vis-off-side"),
                                 penalty_play("delaying-game-unsuccessful-challenge", "05:25", 3))
        assert not text.endswith(")")

    def test_the_nearest_of_a_secondary_field_challenge_and_a_video_review_wins(self, nhl_command):
        assert self._reason_text(nhl_command, stoppage_play("referee-or-linesman", "05:24", 3, secondary="chlg-vis-off-side"),
                                 review_play("05:00", 3)).endswith("(offside challenge)")
        assert self._reason_text(nhl_command, stoppage_play("referee-or-linesman", "05:00", 3, secondary="chlg-vis-off-side"),
                                 review_play("05:24", 3)).endswith("(video review)")

    @pytest.mark.parametrize("secondary", ["tv-timeout", "", None, "puck-in-benches"])
    def test_other_secondary_reasons_explain_nothing(self, nhl_command, secondary):
        assert not self._reason_text(nhl_command, stoppage_play("referee-or-linesman", "05:25", 3, secondary=secondary)).endswith(")")

    def test_a_video_review_just_after_the_goals_clock_is_the_reason(self, nhl_command):
        # real case (2026-10-14): the goal at 09:27, a referee stoppage with the secondary reason video-review at
        # 09:29, other stoppages around it, and no challenge entry
        text = self._reason_text(nhl_command, stoppage_play("goalie-stopped-after-sog", "04:10", 3), review_play("05:27", 3),
                                 stoppage_play("icing", "06:20", 3))
        assert text.endswith("was disallowed (video review)")

    def test_a_video_review_before_the_goals_clock_counts_too(self, nhl_command):
        assert self._reason_text(nhl_command, review_play("05:00", 3)).endswith("(video review)")

    def test_a_video_review_too_far_from_the_goal_or_in_another_period_explains_nothing(self, nhl_command):
        assert not self._reason_text(nhl_command, review_play("03:00", 3)).endswith(")")
        assert not self._reason_text(nhl_command, review_play("05:00", 2)).endswith(")")
        assert not self._reason_text(nhl_command, review_play("05:40", 3)).endswith(")")

    def test_a_plain_referee_stoppage_explains_nothing(self, nhl_command):
        # the 2026-10-13 case: the same stoppage reason, but no video-review secondary reason
        assert not self._reason_text(nhl_command, stoppage_play("referee-or-linesman", "05:27", 3)).endswith(")")

    def test_the_nearest_of_a_challenge_and_a_video_review_wins(self, nhl_command):
        assert self._reason_text(nhl_command, stoppage_play("chlg-hm-off-side", "05:00", 3),
                                 review_play("05:24", 3)).endswith("(video review)")
        assert self._reason_text(nhl_command, review_play("05:00", 3),
                                 stoppage_play("chlg-hm-off-side", "05:24", 3)).endswith("(offside challenge)")

    def test_a_failed_challenges_penalty_does_not_hide_a_video_review(self, nhl_command):
        before = self._reason_text(nhl_command, stoppage_play("chlg-vis-off-side", "05:10", 3),
                                   penalty_play("delaying-game-unsuccessful-challenge", "05:10", 3), review_play("05:26", 3))
        after = self._reason_text(nhl_command, review_play("05:26", 3),
                                  penalty_play("delaying-game-unsuccessful-challenge", "05:27", 3))
        assert before.endswith("(video review)") and after.endswith("(video review)")

    def test_the_failed_challenge_alone_still_explains_nothing(self, nhl_command):
        assert not self._reason_text(nhl_command, stoppage_play("chlg-vis-off-side", "05:25", 3),
                                     penalty_play("delaying-game-unsuccessful-challenge", "05:25", 3)).endswith(")")

    def test_a_goal_with_an_unreadable_clock_gets_no_explanation(self, nhl_command):
        goal = self._goal(time_in_period="")
        text = self._retractions(self._run(nhl_command, [self._pbp(goal)] + [self._pbp(
            stoppage_play("chlg-hm-off-side", "05:25", 3))] * 4))[0]
        assert not text.endswith(")")

    # -- what is kept for the corrections that build on this -------------------------------

    def test_the_posted_scorer_assists_and_label_are_stored_with_the_goal(self, nhl_command):
        goal = self._goal(scorer=1, assist1=2, assist2=3)
        self._run(nhl_command, [self._pbp(goal)])
        record = nhl_command._channels["#nhl.fi"]["games"][1]["announced"][("score", self.AWAY, 0, 1)]
        assert record["posted"] == {"team": "Florida Panthers", "scorer": 1, "scorer_name": "P1 X",
                                    "assists": [2, 3], "period": 3, "clock": "05:25", "label": "05:25 3rd"}
        assert record["ids"] == [1] and record["missing"] == 0 and record["retracted"] is False


class TestJournalLinesForIgnoredRepeats:
    """A line in the bot's log whenever a repeated goal is ignored, so the server journal shows the fix acting."""

    def _pbp(self, *plays):
        return make_pbp(game_id=77, home_id=10, away_id=20, plays=list(plays))

    def _goal(self, event_id, home=0, away=1, **kw):
        return goal_play(event_id=event_id, event_owner_team_id=20, scoring_player_id=1, home_score=home, away_score=away, **kw)

    def test_a_goal_that_comes_back_after_missed_polls_is_logged(self, nhl_command, capsys):
        first = nhl_command._resolve_goals(self._pbp(self._goal(1)), {})[2]
        quiet = self._pbp({"typeDescKey": "faceoff", "eventId": 900, "periodDescriptor": {"number": 1, "periodType": "REG"},
                           "timeInPeriod": "01:00", "details": {}})
        for _ in range(2):
            _, _, first = nhl_command._resolve_goals(quiet, first)
        capsys.readouterr()
        nhl_command._resolve_goals(self._pbp(self._goal(1)), first)
        assert "NHL: game 77: goal (team 20, 0-1) is back after 2 missed poll(s), not announced again" in capsys.readouterr().out

    def test_a_second_play_for_an_announced_goal_is_logged(self, nhl_command, capsys):
        record = nhl_command._resolve_goals(self._pbp(self._goal(279)), {})[2]
        capsys.readouterr()
        nhl_command._resolve_goals(self._pbp(self._goal(278), self._goal(279)), record)
        assert "NHL: game 77: goal (team 20, 0-1) listed again as event [278], not announced again" in capsys.readouterr().out

    def test_a_renumbered_goal_is_logged(self, nhl_command, capsys):
        record = nhl_command._resolve_goals(self._pbp(self._goal(7, away=3)), {})[2]
        capsys.readouterr()
        nhl_command._resolve_goals(self._pbp(self._goal(7, away=2)), record)
        assert "is now (team 20, 0-2), not announced again" in capsys.readouterr().out

    def test_ordinary_polls_and_seeding_log_nothing(self, nhl_command, capsys):
        goal = self._goal(1)
        record = nhl_command._resolve_goals(self._pbp(goal), {}, seed=True)[2]
        nhl_command._resolve_goals(self._pbp(goal), record)
        nhl_command._resolve_goals(self._pbp(goal, self._goal(2, away=2)), record)
        assert capsys.readouterr().out == ""

    def test_seeding_a_feed_with_a_doubly_listed_goal_logs_nothing(self, nhl_command, capsys):
        nhl_command._resolve_goals(self._pbp(self._goal(278), self._goal(279)), {}, seed=True)
        assert capsys.readouterr().out == ""

    def test_the_key_text_of_a_shootout_goal(self, nhl_command):
        assert nhl_command._key_text(("id", 1253)) == "(id, 1253)"


class TestRenumberedGoals:
    """Issue #25: a disallowed goal renumbers the same team's later goals into
    its place. The later goal is not announced again, and it is the goal that
    is gone that gets retracted."""

    HOME, AWAY = TestRetraction.HOME, TestRetraction.AWAY
    _pbp, _quiet, _goal, _run = TestRetraction._pbp, TestRetraction._quiet, TestRetraction._goal, TestRetraction._run
    _retractions, _goals = TestRetraction._retractions, TestRetraction._goals

    def _scenario(self):
        first = self._goal(1, team=self.HOME, home=1, away=0, scorer=1, time_in_period="04:00")
        second = self._goal(2, team=self.HOME, home=2, away=0, scorer=2, time_in_period="06:00")
        renumbered = self._goal(2, team=self.HOME, home=1, away=0, scorer=2, time_in_period="06:00")
        return first, second, renumbered

    def test_the_later_goal_is_not_announced_again(self, nhl_command):
        first, second, renumbered = self._scenario()
        lines = self._run(nhl_command, [self._pbp(first), self._pbp(first, second)] + [self._pbp(renumbered)] * 3)
        assert len(self._goals(lines)) == 2

    def test_the_disallowed_goal_is_retracted_and_the_valid_one_is_not(self, nhl_command):
        first, second, renumbered = self._scenario()
        lines = self._run(nhl_command, [self._pbp(first), self._pbp(first, second)] + [self._pbp(renumbered)] * 8)
        retractions = self._retractions(lines)
        assert len(retractions) == 1
        assert "04:00" in retractions[0] and "P1" in retractions[0] and "P2" not in retractions[0]

    def test_a_displaced_goal_nobody_saw_posted_is_never_retracted(self, nhl_command):
        first, second, renumbered = self._scenario()
        record = {("score", self.HOME, 1, 0): seeded(1)}
        lines = self._run(nhl_command, [self._pbp(first, second)] + [self._pbp(renumbered)] * 8, seeded_record=record)
        assert len(self._goals(lines)) == 1 and self._retractions(lines) == []
