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
import probe_common  # noqa: E402

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

    def test_no_goals_means_0_0(self):
        assert goal_probe.header_state(feed(home=0, away=0))[1] == (0, 0)


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
