"""The pure parts of the read-only probes in tools/ (the polling loops talk to the live
feeds and are verified by running them, see tools/README.md)."""
import os
import sys

import pytest

TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import goal_probe  # noqa: E402
import liiga_sampler  # noqa: E402
import pre_push  # noqa: E402
import probe_common  # noqa: E402
import start_extras  # noqa: E402

ROSTER = [{"playerId": i, "firstName": {"default": n}, "lastName": {"default": "X"}}
          for i, n in ((1, "Alice"), (2, "Bob"), (3, "Cara"), (4, "Dan"))]


def goal(event_id, scorer, a1=None, a2=None, clock="07:12", home=1, away=0, team=20, period_type="REG"):
    return {"typeDescKey": "goal", "eventId": event_id, "timeInPeriod": clock,
            "periodDescriptor": {"number": 1, "periodType": period_type},
            "details": {"scoringPlayerId": scorer, "assist1PlayerId": a1, "assist2PlayerId": a2,
                        "homeScore": home, "awayScore": away, "eventOwnerTeamId": team}}


def feed(*plays, home=None, away=None, outcome=None):
    return {"rosterSpots": ROSTER, "plays": list(plays), "homeTeam": {"score": home},
            "awayTeam": {"score": away}, "gameOutcome": {"lastPeriodType": outcome} if outcome else {}}


@pytest.fixture
def log_lines(monkeypatch):
    lines = []
    monkeypatch.setattr(goal_probe, "log", lines.append)
    return lines


class TestScoreConflict:
    """Issue #47: a goal listed after a later goal contradicts it (0-2 next to 1-1): the probe keeps the whole list."""

    def late(self):  # the 2026-10-13 shape: a visiting goal at 18:25 (0-2) listed with the home goal at 18:33 (1-1)
        return feed(goal(1, 1, clock="06:07", home=0, away=1, team=20),
                    goal(2, 2, clock="18:25", home=0, away=2, team=20),
                    goal(3, 3, clock="18:33", home=1, away=1, team=10))

    def test_a_score_that_goes_down_is_a_conflict_and_lists_every_goal(self):
        assert goal_probe.score_conflict(self.late()) == [
            (1, "06:07", 0, 1, 20, 1), (1, "18:25", 0, 2, 20, 2), (1, "18:33", 1, 1, 10, 3)]

    def test_goals_are_ordered_by_the_clock_not_by_the_feeds_list(self):
        pbp = self.late()
        pbp["plays"].reverse()
        assert goal_probe.score_conflict(pbp) is not None

    def test_a_consistent_game_listed_out_of_order_is_not_a_conflict(self):
        """Only the clock decides the order: listed backwards, 1-1 comes before 1-0 and would look like a fall."""
        pbp = feed(goal(2, 2, clock="10:00", home=1, away=1, team=10), goal(1, 1, clock="05:00", home=1, away=0, team=10))
        assert goal_probe.score_conflict(pbp) is None

    def test_a_shootout_goal_with_a_lower_score_than_an_earlier_goal_is_ignored(self):
        shootout = dict(goal(2, 2, clock="00:00", home=0, away=0, period_type="SO"),
                        periodDescriptor={"number": 5, "periodType": "SO"})  # the 5th period, after the regular goal
        pbp = feed(goal(1, 1, clock="05:00", home=1, away=0), shootout)
        assert goal_probe.score_conflict(pbp) is None

    def test_a_consistent_list_is_not_a_conflict(self):
        pbp = feed(goal(1, 1, clock="06:07", home=0, away=1), goal(2, 2, clock="18:25", home=0, away=2),
                   goal(3, 3, clock="18:40", home=1, away=2, team=10))
        assert goal_probe.score_conflict(pbp) is None

    def test_shootout_goals_all_carry_the_tie_and_are_left_out(self):
        pbp = feed(goal(1, 1, clock="06:07", home=2, away=2),
                   goal(2, 2, clock="00:00", home=2, away=2, period_type="SO"),
                   goal(3, 3, clock="00:00", home=2, away=2, period_type="SO"))
        assert goal_probe.score_conflict(pbp) is None

    def test_goals_without_numeric_scores_and_other_plays_are_ignored(self):
        pbp = feed(goal(1, 1, home=None, away=None), {"typeDescKey": "stoppage"}, goal(2, 2, clock="08:00", home=1, away=0))
        assert goal_probe.score_conflict(pbp) is None
        assert goal_probe.score_conflict({}) is None

    def test_a_missing_clock_does_not_crash(self):
        # a goal with no clock sorts first (0 s): 1-0 then 0-1 is a score that goes down
        pbp = feed(goal(1, 1, clock=None, home=1, away=0), goal(2, 2, clock="05:00", home=0, away=1))
        assert goal_probe.score_conflict(pbp) == [(1, None, 1, 0, 20, 1), (1, "05:00", 0, 1, 20, 2)]

    def test_it_is_logged_once_again_when_the_list_changes_and_once_when_it_clears(self):
        state = {}
        first = goal_probe.check_scores(state, "A@B", self.late())
        assert len(first) == 1 and first[0].startswith("A@B: SCORES CONTRADICT in the feed (home-away): P1 06:07 0-1 team 20 id 1 | P1 18:25 0-2")
        assert goal_probe.check_scores(state, "A@B", self.late()) == []  # unchanged: nothing more
        changed = self.late()
        changed["plays"].append(goal(4, 4, clock="19:10", home=1, away=3))
        assert len(goal_probe.check_scores(state, "A@B", changed)) == 1
        cleared = feed(goal(1, 1, clock="06:07", home=0, away=1), goal(3, 3, clock="18:33", home=1, away=1, team=10))
        assert goal_probe.check_scores(state, "A@B", cleared) == ["A@B: scores consistent again"]
        assert goal_probe.check_scores(state, "A@B", cleared) == []  # and only once

    def test_each_game_has_its_own_state(self):
        state = {}
        assert len(goal_probe.check_scores(state, "A@B", self.late())) == 1
        assert len(goal_probe.check_scores(state, "C@D", self.late())) == 1


class TestObserve:
    def test_first_sight_then_a_scorer_correction_that_swaps_roles(self):
        goals = {}
        first = goal_probe.observe(goals, "A@B", feed(goal(1, 2)), 0.0)
        assert first == ["A@B P1 07:12: FIRST SEEN Bob X [no assists]"]
        later = goal_probe.observe(goals, "A@B", feed(goal(1, 1, 2, 3, clock="07:14")), 40.0)
        assert any("SCORER Bob X -> Alice X after 40s" in e and "assists: YES" in e for e in later)
        assert any("ASSISTS [none] -> [Bob X, Cara X] after 40s" in e for e in later)
        assert any("CLOCK 07:12 -> 07:14" in e for e in later)

    def test_nothing_changed_nothing_logged(self):
        goals = {}
        goal_probe.observe(goals, "A@B", feed(goal(1, 2)), 0.0)
        assert goal_probe.observe(goals, "A@B", feed(goal(1, 2)), 15.0) == []

    def test_lost_and_gained_assists_are_both_recorded(self):
        goals = {}
        goal_probe.observe(goals, "A@B", feed(goal(1, 1, 2, 3)), 0.0)
        goal_probe.observe(goals, "A@B", feed(goal(1, 1, 2)), 60.0)
        goal_probe.observe(goals, "A@B", feed(goal(1, 1, 2, 4)), 90.0)
        steps = goals[("A@B", 1)]["assist_events"]
        assert steps == [(60.0, 2, 1), (90.0, 1, 2)]

    def test_a_goal_that_leaves_the_feed_and_returns(self):
        goals = {}
        goal_probe.observe(goals, "A@B", feed(goal(1, 1)), 0.0)
        gone = goal_probe.observe(goals, "A@B", feed(), 40.0)
        back = goal_probe.observe(goals, "A@B", feed(goal(1, 1)), 82.0)
        assert "REMOVED from the feed 40s after first seen" in gone[0]
        assert "REAPPEARED in the feed after 42s away" in back[0]

    def test_a_removed_goal_is_reported_only_once(self):
        goals = {}
        goal_probe.observe(goals, "A@B", feed(goal(1, 1)), 0.0)
        assert len(goal_probe.observe(goals, "A@B", feed(), 40.0)) == 1
        assert goal_probe.observe(goals, "A@B", feed(), 55.0) == []

    def test_a_goal_listed_twice_is_flagged_once(self):
        goals = {}
        both = feed(goal(278, 1, home=2, away=0), goal(279, 2, home=2, away=0))
        first = goal_probe.observe(goals, "A@B", both, 0.0)
        assert any("DOUBLY LISTED" in e and "[278, 279]" in e for e in first)
        assert not any("DOUBLY LISTED" in e for e in goal_probe.observe(goals, "A@B", both, 10.0))

    def test_goals_of_other_games_are_not_reported_removed(self):
        goals = {}
        goal_probe.observe(goals, "A@B", feed(goal(1, 1)), 0.0)
        assert goal_probe.observe(goals, "C@D", feed(), 5.0) == []


class TestShootoutGoalsAreNotDoublyListed:
    def test_shootout_goals_share_a_running_score_and_are_not_flagged(self):
        so = [goal(5, 1, home=3, away=3, period_type="SO"), goal(6, 2, home=3, away=3, period_type="SO")]
        events = goal_probe.observe({}, "A@B", feed(goal(1, 1, home=3, away=3), *so), 0.0)
        assert not any("DOUBLY LISTED" in e for e in events)

    def test_two_ordinary_goals_with_the_same_team_and_score_still_are(self):
        events = goal_probe.observe({}, "A@B", feed(goal(1, 1, home=1, away=0), goal(2, 1, home=1, away=0)), 0.0)
        assert any("DOUBLY LISTED" in e for e in events)


class TestHeaderCheck:
    def _pbp(self, header, running, outcome=None, period_type="REG"):
        plays = [goal(1, 1, home=running[0], away=running[1], period_type=period_type)]
        return feed(*plays, home=header[0], away=header[1], outcome=outcome)

    def test_live_games_need_no_fast_polling(self, log_lines):
        assert goal_probe.check_header({}, "A@B", "LIVE", self._pbp((1, 0), (1, 0)), 0.0) is False
        assert log_lines == []

    def test_agreement_is_logged_and_needs_no_more_polling(self, log_lines):
        assert goal_probe.check_header({}, "A@B", "FINAL", self._pbp((1, 0), (1, 0)), 0.0) is False
        assert "AGREE" in log_lines[0]

    def test_a_stale_header_is_flagged_and_its_catch_up_timed(self, log_lines):
        ends = {}
        assert goal_probe.check_header(ends, "A@B", "FINAL", self._pbp((0, 0), (1, 0)), 100.0) is True
        assert "MISMATCH" in log_lines[0]
        assert goal_probe.check_header(ends, "A@B", "FINAL", self._pbp((1, 0), (1, 0)), 135.0) is False
        assert "header caught up 35s" in log_lines[-1]

    def test_an_overtime_game_needs_the_overtime_outcome_in_the_header(self, log_lines):
        stale = self._pbp((1, 0), (1, 0), outcome="REG", period_type="OT")
        assert goal_probe.check_header({}, "A@B", "FINAL", stale, 0.0) is True
        fresh = self._pbp((1, 0), (1, 0), outcome="OT", period_type="OT")
        assert goal_probe.check_header({}, "C@D", "FINAL", fresh, 0.0) is False

    def test_shootout_goals_do_not_count_towards_the_running_score(self):
        so = goal(5, 1, home=0, away=0, period_type="SO")
        header, running, outcome, period_type = goal_probe.header_state(
            feed(goal(1, 1, home=1, away=1), so, home=2, away=1, outcome="SO"))
        assert (header, running, outcome, period_type) == ((2, 1), (1, 1), "SO", "SO")

    def _shootout(self, header, outcome="SO"):
        plays = [goal(1, 1, home=3, away=3), goal(5, 1, home=3, away=3, period_type="SO"),
                 goal(6, 2, home=3, away=3, period_type="SO")]
        return feed(*plays, home=header[0], away=header[1], outcome=outcome)

    def test_a_shootout_header_one_goal_ahead_of_the_running_score_is_the_right_one(self, log_lines):
        assert goal_probe.check_header({}, "A@B", "FINAL", self._shootout((4, 3)), 0.0) is False
        assert "AGREE" in log_lines[0] and "MISMATCH" not in log_lines[0]

    def test_a_shootout_that_agreed_at_the_first_check_logs_no_catch_up(self, log_lines):
        ends = {}
        goal_probe.check_header(ends, "A@B", "FINAL", self._shootout((4, 3)), 0.0)
        goal_probe.check_header(ends, "A@B", "FINAL", self._shootout((4, 3)), 20.0)
        assert not any("caught up" in line for line in log_lines)

    def test_the_summary_says_agreed_for_a_shootout_that_agreed_at_the_first_check(self, log_lines):
        ends = {}
        goal_probe.check_header(ends, "A@B", "FINAL", self._shootout((4, 3)), 0.0)
        goal_probe.summarize({}, ends)
        assert "A@B: header at first check in an end state: agreed" in log_lines[-1]

    def test_a_shootout_header_equal_to_the_running_score_is_the_stale_one(self, log_lines):
        assert goal_probe.check_header({}, "A@B", "OVER", self._shootout((3, 3)), 0.0) is True
        assert "MISMATCH" in log_lines[0]

    def test_a_shootout_header_two_goals_ahead_or_without_the_shootout_outcome_is_a_mismatch(self, log_lines):
        assert goal_probe.check_header({}, "A@B", "FINAL", self._shootout((5, 3)), 0.0) is True
        assert goal_probe.check_header({}, "C@D", "FINAL", self._shootout((4, 3), outcome="REG"), 0.0) is True

    def test_a_shootout_header_without_numbers_is_a_mismatch_not_a_crash(self, log_lines):
        assert goal_probe.check_header({}, "A@B", "FINAL", self._shootout((None, None)), 0.0) is True

    def test_no_goals_means_0_0(self):
        assert goal_probe.header_state(feed(home=0, away=0))[1] == (0, 0)


def stoppage(event_id, clock, reason="offside", secondary=None, period=1):
    return {"typeDescKey": "stoppage", "eventId": event_id, "timeInPeriod": clock,
            "periodDescriptor": {"number": period}, "details": {"reason": reason, "secondaryReason": secondary}}


class TestReviewStoppages:
    """Issue #64: how long after a goal leaves the feed its challenge or video-review entry appears."""

    def reviews(self):
        return {"seen": set(), "games": set(), "delays": []}

    def test_which_stoppages_count(self):
        pbp = feed(stoppage(1, "06:38", "chlg-hm-off-side"),
                   stoppage(2, "06:38", "referee-or-linesman", "chlg-vis-off-side"),
                   stoppage(3, "10:00", "referee-or-linesman", "video-review"),
                   stoppage(4, "06:38", "offside"), stoppage(5, "12:00", "icing"))
        assert goal_probe.review_stoppages(pbp) == {
            1: (1, "06:38", "chlg-hm-off-side"), 2: (1, "06:38", "chlg-vis-off-side"), 3: (1, "10:00", "video-review")}

    def test_the_delay_after_the_goal_left_the_feed(self):
        goals, reviews = {}, self.reviews()
        goal_probe.observe(goals, "A@B", feed(goal(1, 1, clock="07:01")), 100.0)
        goal_probe.observe_reviews(reviews, goals, "A@B", feed(goal(1, 1, clock="07:01")), 100.0)
        goal_probe.observe(goals, "A@B", feed(), 180.0)
        assert goal_probe.observe_reviews(reviews, goals, "A@B", feed(), 180.0) == []
        late = feed(stoppage(9, "06:38", "referee-or-linesman", "chlg-vis-off-side"))
        events = goal_probe.observe_reviews(reviews, goals, "A@B", late, 400.0)
        assert events == ["A@B P1 06:38: REVIEW STOPPAGE chlg-vis-off-side, 220s after the goal at 07:01 "
                          "(Alice X [no assists]) left the feed"]
        assert reviews["delays"] == [220.0]
        assert goal_probe.observe_reviews(reviews, goals, "A@B", late, 420.0) == []  # once only

    def test_one_before_the_goal_has_left_is_logged_untimed(self):
        goals, reviews = {}, self.reviews()
        goal_probe.observe_reviews(reviews, goals, "A@B", feed(goal(1, 1, clock="07:01")), 0.0)
        pbp = feed(goal(1, 1, clock="07:01"), stoppage(9, "06:38", "chlg-hm-goal-interference"))
        goal_probe.observe(goals, "A@B", pbp, 20.0)
        assert goal_probe.observe_reviews(reviews, goals, "A@B", pbp, 20.0) == [
            "A@B P1 06:38: REVIEW STOPPAGE chlg-hm-goal-interference, no removed goal near it yet"]
        assert reviews["delays"] == []

    @pytest.mark.parametrize("clock, period", [("05:00", 1), ("07:12", 1), ("06:38", 2)])
    def test_a_removed_goal_outside_the_bots_look_back_is_not_matched(self, clock, period):
        goals, reviews = {}, self.reviews()
        goal_probe.observe(goals, "A@B", feed(goal(1, 1, clock="07:01")), 0.0)
        goal_probe.observe_reviews(reviews, goals, "A@B", feed(), 0.0)
        goal_probe.observe(goals, "A@B", feed(), 20.0)
        events = goal_probe.observe_reviews(reviews, goals, "A@B", feed(stoppage(9, clock, "chlg-vis-off-side",
                                                                                  period=period)), 40.0)
        assert events[0].endswith("no removed goal near it yet")

    def test_stoppages_already_there_at_a_games_first_look_are_not_timed(self):
        goals, reviews = {}, self.reviews()
        events = goal_probe.observe_reviews(reviews, goals, "A@B", feed(stoppage(9, "06:38", "chlg-vis-off-side")), 0.0)
        assert events == ["A@B P1 06:38: REVIEW STOPPAGE chlg-vis-off-side (already in the feed at the first look)"]

    def test_the_summary_lists_the_delays(self, log_lines):
        goal_probe.summarize({}, {}, {"seen": {("A@B", 1), ("A@B", 2)}, "games": {"A@B"}, "delays": [220.4, 35.0]})
        assert "  review stoppages: 2; seconds after a removed goal near it left the feed: [35, 220]" in log_lines

    def test_the_run_loop_logs_them(self, log_lines):
        command = FakeNhl([{1: score_game()}], {1: feed(stoppage(9, "06:38", "chlg-vis-off-side"))})
        clock = FakeClock()
        goal_probe.run(command, "2026-10-08", 30.0, 10, sleep=clock.sleep, clock=clock.now)
        assert log_lines.count("B@A P1 06:38: REVIEW STOPPAGE chlg-vis-off-side (already in the feed at the first look)") == 1


def score_game(abbrev_home="A", abbrev_away="B", state="LIVE"):
    return {"gameState": state, "homeTeam": {"abbrev": abbrev_home}, "awayTeam": {"abbrev": abbrev_away}}


class FakeNhl:
    """Stands in for NHLCommand: `scores` is a list of results (None = the request failed), one per
    cycle, the last one repeating; `pbp` maps a game id to its feed (None = failed)."""

    def __init__(self, scores, pbp):
        self.scores, self.pbp, self.calls = list(scores), pbp, 0

    def _fetch_scores(self, date):
        result = self.scores[min(self.calls, len(self.scores) - 1)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result

    def _fetch_play_by_play(self, gid):
        return self.pbp.get(gid)


class FakeClock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class TestRun:
    def run(self, command, poll=10, deadline=100000.0):
        clock = FakeClock()
        goals, ends, reviews, reason = goal_probe.run(command, "2026-10-08", deadline, poll, sleep=clock.sleep,
                                                      clock=clock.now)
        return clock, reason

    def test_it_stops_after_ten_silent_cycles_in_a_row_and_says_so(self, log_lines):
        clock, reason = self.run(FakeNhl([None], {}))
        assert reason == "API silent"
        assert len(clock.sleeps) == goal_probe.MAX_SILENT_CYCLES - 1  # no sleep after the cycle that gave up
        assert log_lines[-1] == "stopping: the API has not answered in 10 cycles in a row"

    def test_one_answer_starts_the_count_over(self, log_lines):
        answers = [None] * 9 + [{}] + [None] * 9
        clock, reason = self.run(FakeNhl(answers, {}), poll=10, deadline=190.0)
        assert reason == "deadline" and not any("stopping" in line for line in log_lines)

    def test_live_games_whose_play_by_play_never_arrives_count_as_silent(self, log_lines):
        _, reason = self.run(FakeNhl([{1: score_game()}], {1: None}))
        assert reason == "API silent"

    def test_one_play_by_play_among_failures_counts_as_an_answer(self, log_lines):
        scores = {1: score_game(), 2: score_game("C", "D")}
        _, reason = self.run(FakeNhl([scores], {1: None, 2: feed(goal(1, 1))}), deadline=500.0)
        assert reason == "deadline"

    def test_an_exception_counts_as_silent_and_is_logged(self, log_lines):
        _, reason = self.run(FakeNhl([RuntimeError("boom")], {}))
        assert reason == "API silent"
        assert "error: RuntimeError: boom" in log_lines

    def test_it_stops_when_every_game_has_been_over_for_a_while(self, log_lines):
        final = {1: score_game(state="FINAL")}
        clock, reason = self.run(FakeNhl([final], {1: feed(goal(1, 1), home=1, away=0)}), poll=100)
        assert reason == "all games settled"
        assert clock.t > goal_probe.WATCH_AFTER_END

    def test_goals_are_logged_while_polling(self, log_lines):
        self.run(FakeNhl([{1: score_game()}], {1: feed(goal(1, 1))}), deadline=15.0)
        assert any("FIRST SEEN" in line for line in log_lines)

    @pytest.mark.parametrize("poll, ending", [(10, 5), (20, 10), (30, 15), (8, 5)])
    def test_the_interval_between_cycles_is_the_given_one_and_half_of_it_while_a_game_is_ending(self, log_lines, poll, ending):
        over = {1: score_game(state="OVER")}
        live = {1: score_game()}
        pbp_over = feed(goal(1, 1), home=0, away=0)  # header behind the plays: the game still needs fast polling
        clock, _ = self.run(FakeNhl([live, over], {1: pbp_over}), poll=poll, deadline=poll * 2 + ending + 0.5)
        assert clock.sleeps[0] == poll and clock.sleeps[1] == ending


class TestParseArgs:
    def test_the_defaults(self):
        assert goal_probe.parse_args(["goal_probe.py", "2026-10-08"]) == ("2026-10-08", 14, goal_probe.LIVE_POLL)

    def test_hours_and_interval(self):
        assert goal_probe.parse_args(["goal_probe.py", "2026-10-08", "6", "20"]) == ("2026-10-08", 6.0, 20.0)

    @pytest.mark.parametrize("argv", [
        ["goal_probe.py"],                                  # no date
        ["goal_probe.py", "d", "1", "20", "extra"],         # too many
        ["goal_probe.py", "d", "many"],                     # hours not a number
        ["goal_probe.py", "d", "6", "fast"],                # interval not a number
        ["goal_probe.py", "d", "6", "2"],                   # faster than the fastest ending poll
        ["goal_probe.py", "d", "0"],                        # no time at all
    ])
    def test_a_usage_error_is_none(self, argv):
        assert goal_probe.parse_args(argv) is None


class TestLiigaJudge:
    BASE = {"gameTime": 1241, "home": 0, "away": 0, "events_home": 0, "events_away": 0, "ended": False, "period": 2}

    def test_first_poll_has_nothing_to_compare(self):
        assert liiga_sampler.judge(None, self.BASE) == []

    def test_forward_movement_is_not_flagged(self):
        assert liiga_sampler.judge(self.BASE, dict(self.BASE, gameTime=1252, home=1)) == []

    def test_clock_and_score_going_back(self):
        prev = dict(self.BASE, gameTime=1252, home=1)
        assert liiga_sampler.judge(prev, self.BASE) == ["CLOCK-BACK 11s", "SCORE-BACK 1-0->0-0"]

    def test_ended_flipping_back_and_the_period_going_back(self):
        assert liiga_sampler.judge(dict(self.BASE, ended=True), self.BASE) == ["ENDED-FLAP"]
        assert liiga_sampler.judge(self.BASE, dict(self.BASE, period=1)) == ["PERIOD-BACK 2->1"]

    def test_the_goal_event_list_shrinking_is_flagged_on_its_own(self):
        prev = dict(self.BASE, events_home=1, home=1)
        assert liiga_sampler.judge(prev, dict(self.BASE, home=1)) == ["EVENTS-BACK 1-0->0-0"]
        assert liiga_sampler.judge(self.BASE, dict(self.BASE, events_away=1)) == []

    def test_a_missing_clock_is_not_a_backwards_step(self):
        assert liiga_sampler.judge(self.BASE, dict(self.BASE, gameTime=None)) == []

    def _game(self, home=(), away=()):
        def event(event_id, period, game_time, scorer, name=True):
            e = {"eventId": event_id, "period": period, "gameTime": game_time}
            if name:
                e["scorerPlayer"] = {"playerId": scorer, "firstName": "A", "lastName": "B"}
            return e
        return {"homeTeam": {"goalEvents": [event(*g) for g in home]}, "awayTeam": {"goalEvents": [event(*g) for g in away]}}

    def test_goal_identities_are_numbers_only_per_side_in_feed_order(self):
        game = self._game(home=[(11, 1, 125, 501), (12, 2, 40, 502)], away=[(219, 5, 3900, 777)])
        assert liiga_sampler.goal_identities(game) == (((11, 1, 125, 501), (12, 2, 40, 502)), ((219, 5, 3900, 777),))

    def test_an_event_without_a_scorer_is_a_disallowed_goal_and_is_left_out(self):
        game = {"homeTeam": {"goalEvents": [{"eventId": 3, "period": 1, "gameTime": 446}]}, "awayTeam": {}}
        assert liiga_sampler.goal_identities(game) == ((), ())

    def test_a_rekeyed_goal_gives_different_identities(self):
        before, after = self._game(away=[(218, 5, 3900, 777)]), self._game(away=[(219, 5, 3900, 777)])
        assert liiga_sampler.goal_identities(before) != liiga_sampler.goal_identities(after)

    def test_a_missing_part_is_shown_as_a_dash(self):
        game = self._game(away=[(None, 5, 3900, 777)])
        assert liiga_sampler.describe_goals(liiga_sampler.goal_identities(game)) == "home - | away -/5/3900/777"

    def test_the_description_lists_both_sides(self):
        game = self._game(home=[(11, 1, 125, 501), (12, 2, 40, 502)], away=[(219, 5, 3900, 777)])
        assert liiga_sampler.describe_goals(liiga_sampler.goal_identities(game)) == \
            "home 11/1/125/501, 12/2/40/502 | away 219/5/3900/777"

    def test_a_game_with_no_goals_is_described_without_error(self):
        assert liiga_sampler.describe_goals(liiga_sampler.goal_identities({})) == "home - | away -"

    def test_cache_node_id_from_a_via_header(self):
        via = "1.1 b7248001409a22dcf06ac3c9df2f5fac.cloudfront.net (CloudFront), 1.1 7995acb4.cloudfront.net (CloudFront)"
        assert liiga_sampler.edge_of(via) == "b72480"
        assert liiga_sampler.edge_of("") == "?"

    def test_state_of_a_game(self):
        game = {"gameTime": 10, "homeTeam": {"goals": 2, "goalEvents": [{}, {}]}, "awayTeam": {}, "ended": 1,
                "currentPeriod": 3}
        assert liiga_sampler.state_of(game) == {"gameTime": 10, "home": 2, "away": 0, "events_home": 2,
                                                "events_away": 0, "ended": True, "period": 3}


class TestLogger:
    def test_lines_are_stamped_and_appended(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(probe_common, "LOG_DIR", str(tmp_path / "logs"))
        log = probe_common.make_logger("x")
        log("hello")
        log("again")
        text = (tmp_path / "logs" / "x.log").read_text().splitlines()
        assert len(text) == 2 and text[0].endswith("hello") and "Z (" in text[0] and " local) " in text[0]
        assert "hello" in capsys.readouterr().out


class TestStartExtras:
    """The daily timer's decision: which probe to start, from when, with which date and hours."""
    UTC = start_extras.UTC

    def at(self, day, hour, minute=0):
        import datetime
        return datetime.datetime(2026, 11, day, hour, minute, tzinfo=self.UTC)

    def test_a_big_nhl_slate_gets_the_probe_with_its_eastern_date(self):
        # 11 games from 00:00 UTC on the 20th: the slate of the 19th in US Eastern time.
        starts = [self.at(20, 0)] * 10 + [self.at(20, 3)]
        (run,) = start_extras.plan(self.at(19, 10), None, starts)
        assert run.script == "goal_probe.py"
        assert run.args == ("2026-11-19", "8", "20")  # 23:30 to 07:00 (last start + 4 h), rounded up
        assert run.start_at == self.at(19, 23, 30)

    def test_a_big_liiga_day_gets_the_sampler_with_its_helsinki_date(self):
        starts = [self.at(20, 16, 30)] * 6
        (run,) = start_extras.plan(self.at(20, 10), starts, None)
        assert (run.script, run.args, run.start_at) == ("liiga_sampler.py", ("2026-11-20", "4"), self.at(20, 16))

    def test_the_start_is_in_utc_whatever_zone_the_schedule_used(self):
        helsinki = start_extras.LiigaCommand.HELSINKI_TZ  # the trackers' parser returns Helsinki-local times
        (run,) = start_extras.plan(self.at(19, 10), None, [self.at(20, 0).astimezone(helsinki)] * 10)
        assert run.start_at.utcoffset().total_seconds() == 0 and f"{run.start_at:%H:%M}" == "23:30"

    def test_small_days_get_nothing(self):
        assert start_extras.plan(self.at(19, 10), [self.at(19, 16, 30)] * 4, [self.at(20, 0)] * 9) == []

    def test_both_leagues_in_the_order_they_start(self):
        runs = start_extras.plan(self.at(20, 10), [self.at(20, 16, 30)] * 7, [self.at(21, 0)] * 12)
        assert [r.script for r in runs] == ["liiga_sampler.py", "goal_probe.py"]

    def test_a_run_whose_start_has_passed_starts_now_for_the_hours_left(self):
        (run,) = start_extras.plan(self.at(20, 18), [self.at(20, 16, 30)] * 5, None)
        assert run.start_at == self.at(20, 18) and run.args[1] == "2"  # until 20:00

    def test_a_day_already_over_gets_nothing(self):
        assert start_extras.plan(self.at(20, 23), [self.at(20, 16, 30)] * 5, None) == []

    def test_a_schedule_that_could_not_be_read_gets_nothing(self):
        assert start_extras.plan(self.at(20, 10), None, None) == []

    def test_todays_starts_reads_the_trackers_own_schedule(self):
        class Command:
            START_TIME_KEY = "start"
            def _fetch_today_games(self):
                return {1: {"start": "2026-11-20T16:30:00Z"}, 2: {"start": None}, 3: {}}
            _parse_start_dt = start_extras.LiigaCommand._parse_start_dt
            HELSINKI_TZ = start_extras.LiigaCommand.HELSINKI_TZ
        assert [dt.astimezone(self.UTC) for dt in start_extras.todays_starts(Command())] == [self.at(20, 16, 30)]
        Command._fetch_today_games = lambda self: None
        assert start_extras.todays_starts(Command()) is None

    @pytest.mark.parametrize("command", [start_extras.LiigaCommand, start_extras.NHLCommand])
    def test_the_real_trackers_still_have_what_it_reads(self, command):
        # The test above uses a stand-in, so a tracker renaming one of these would pass it and crash the
        # server's timer instead, leaving that night without probe data.
        assert callable(command._fetch_today_games)
        assert callable(command._parse_start_dt)
        assert isinstance(command.START_TIME_KEY, str)
        assert command.HELSINKI_TZ is not None
        assert start_extras.NHLCommand.EASTERN_TZ is not None

    def test_main_waits_until_each_start_and_skips_a_probe_already_running(self, monkeypatch, capsys):
        runs = [start_extras.Run("liiga_sampler.py", ("2026-11-20", "4"), self.at(20, 16)),
                start_extras.Run("goal_probe.py", ("2026-11-20", "8", "20"), self.at(20, 23, 30))]
        monkeypatch.setattr(start_extras, "plan", lambda now, liiga, nhl: runs)
        monkeypatch.setattr(start_extras, "todays_starts", lambda command: [])
        monkeypatch.setattr(start_extras, "already_running", lambda script: script == "goal_probe.py")
        now = [self.at(20, 10)]
        slept, launched = [], []

        def sleep(seconds):
            assert len(slept) < 5, "slept again and again"
            slept.append(seconds)
            now[0] += __import__("datetime").timedelta(seconds=seconds)

        class Process:
            def wait(self):
                pass

        def launch(argv, **kwargs):
            launched.append(argv[1:])
            return Process()

        start_extras.main(clock=lambda: now[0], sleep=sleep, launch=launch)
        assert slept == [6 * 3600, 7.5 * 3600]
        assert [os.path.basename(a[0]) for a in launched] == ["liiga_sampler.py"]
        assert launched[0][1:] == ["2026-11-20", "4"]
        assert "goal_probe.py is already running" in capsys.readouterr().out

    def test_main_with_nothing_to_do_says_so(self, monkeypatch, capsys):
        monkeypatch.setattr(start_extras, "todays_starts", lambda command: None)
        assert start_extras.main(clock=lambda: self.at(20, 10)) == []
        assert "nothing to start today (2026-11-20)" in capsys.readouterr().out

    def test_a_dry_run_prints_the_plan_and_starts_nothing(self, monkeypatch, capsys):
        runs = [start_extras.Run("goal_probe.py", ("2026-11-19", "8", "20"), self.at(19, 23, 30))]
        monkeypatch.setattr(start_extras, "plan", lambda now, liiga, nhl: runs)
        monkeypatch.setattr(start_extras, "todays_starts", lambda command: [])

        def launch(*args, **kwargs):
            raise AssertionError("a dry run started a probe")

        assert start_extras.main(clock=lambda: self.at(19, 10), launch=launch, dry_run=True) == []
        assert "goal_probe.py 2026-11-19 8 20 from 23:30 UTC" in capsys.readouterr().out

    def test_already_running_asks_pgrep_for_the_script(self, monkeypatch):
        calls = []
        monkeypatch.setattr(start_extras.subprocess, "run",
                            lambda argv, **kw: calls.append(argv) or type("R", (), {"returncode": 1})())
        assert start_extras.already_running("goal_probe.py") is False
        assert calls == [["pgrep", "-f", "tools/goal_probe.py"]]


class TestPrePush:
    UTC = pre_push.UTC

    def at(self, hour, minute=0):
        return pre_push.datetime.datetime(2026, 11, 21, hour, minute, tzinfo=self.UTC)

    def test_the_bot_runs_the_commit_a_no_restart_deploy_names(self):
        log = ("Deploy\tDeploy over SSH\techo \"restart: changed since ...\"\n"
               "Deploy\tDeploy over SSH\t2026-11-21T21:52:16.6793966Z no restart needed: nothing the bot runs "
               "changed since it started from 6b946bd28f90b9969f8d5e5882ed7bb9df708d0e\n")
        assert pre_push.started_from(log, "3aed7d4") == "6b946bd28f90b9969f8d5e5882ed7bb9df708d0e"

    def test_after_a_restart_the_bot_runs_the_deployed_commit(self):
        log = "Deploy\tDeploy over SSH\t2026-11-19T10:00:01.1Z restart: changed since the running bot started: src/x.py\n"
        assert pre_push.started_from(log, "abc1234") == "abc1234"

    def test_the_scripts_own_source_lines_are_not_a_decision(self):
        log = ('Deploy\tDeploy over SSH\techo "restart: the commit the running bot was started from is not known"\n'
               "Deploy\tDeploy over SSH\tsudo -n systemctl restart \"***\"\n")
        assert pre_push.started_from(log, "abc1234") is None

    @pytest.mark.parametrize("start, expected", [
        ((20, 30), "live"),   # started 1 h 43 min ago, within the NHL's 4 h
        ((22, 13), "live"),   # starting this minute
        ((23, 0), "soon"),    # within SOON_HOURS
        ((23, 30), "later"),
        ((18, 0), "over"),    # 4 h 13 min ago, past the NHL's 4 h
    ])
    def test_game_status(self, start, expected):
        assert pre_push.game_status(self.at(22, 13), self.at(*start), 4) == expected

    def test_added_lines_carry_their_file_and_new_line_number(self):
        diff = ("diff --git a/CLAUDE.md b/CLAUDE.md\n--- a/CLAUDE.md\n+++ b/CLAUDE.md\n"
                "@@ -10,2 +10,3 @@\n-old\n+new one\n+new two\n"
                "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        assert pre_push.added_lines(diff) == [("CLAUDE.md", 10, "new one"), ("CLAUDE.md", 11, "new two")]

    def test_a_pure_move_has_no_differences(self):
        diff = ("--- a/a.py\n+++ b/a.py\n@@ -1,2 +0,0 @@\n-def f():\n-    return 1\n"
                "--- /dev/null\n+++ b/b.py\n@@ -0,0 +1,3 @@\n+def f():\n+\n+    return 1\n")
        assert pre_push.move_differences(diff) == ([], [])

    def test_an_edited_line_in_a_move_shows_on_both_sides(self):
        diff = "--- a/a.py\n+++ b/b.py\n@@ -1 +1 @@\n-    return 1\n+    return 2\n"
        assert pre_push.move_differences(diff) == (["    return 1"], ["    return 2"])

    def test_server_values_split_the_login_from_the_host_and_skip_placeholders(self):
        conf = ('# comment\nSERVER="someone@203.0.113.7"\nSERVICE="botunit"\nBOT_DIR="<the checkout>"\n')
        assert pre_push.server_values(conf) == {"login": "someone", "host": "203.0.113.7", "service": "botunit"}

    def test_server_hits_name_what_matched_but_not_the_value(self):
        address = ".".join(("10", "1", "2", "3"))  # built here so this file holds no address the scan flags
        lines = [("a.md", 3, "restart botunit by hand"), ("b.md", 9, f"seen from {address}"),
                 ("c.py", 1, "version 3.14.0 is fine")]
        assert pre_push.server_hits(lines, {"service": "botunit"}) == [
            ("a.md", 3, "service"), ("b.md", 9, "an IP address")]

    def test_documentation_addresses_are_not_flagged(self):
        lines = [("t.py", n, f"host {ip}") for n, ip in enumerate(("192.0.2.1", "198.51.100.4", "203.0.113.7"))]
        assert pre_push.server_hits(lines, {}) == []

    def test_issue_numbers_are_only_reported_in_claude_md(self):
        lines = [("CLAUDE.md", 5, "posted by the second poll (#70), so"), ("CLAUDE.md", 6, "colour &#39; or C#9"),
                 ("README.md", 2, "see #70")]
        assert pre_push.issue_refs(lines) == [(5, "posted by the second poll (#70), so")]
