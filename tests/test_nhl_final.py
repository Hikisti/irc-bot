"""Tests of the NHL tracker's end of a game: the FINAL line, the level-score guard, the attendance lookups and the
results list (src/nhl_final.py)."""
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from tests.conftest import make_json_response as make_response
from tests.nhl_test_helpers import goal_play, html_response, make_pbp, make_schedule_game, roster_spot


class TestAnnounceEnd:
    def test_regulation_final_has_no_suffix(self, nhl_command):
        bot = MagicMock()
        pbp = make_pbp(home_score=5, away_score=4, last_period_type=None)
        nhl_command._announce_end(bot, "#nhl.fi", pbp)
        message = bot.send_message.call_args[0][1]
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 5-4 Florida Panthers"

    def test_overtime_final_gets_ot_suffix(self, nhl_command):
        bot = MagicMock()
        pbp = make_pbp(home_score=3, away_score=2, last_period_type="OT")
        nhl_command._announce_end(bot, "#nhl.fi", pbp)
        assert bot.send_message.call_args[0][1].endswith("(OT)")

    def test_shootout_final_gets_so_suffix(self, nhl_command):
        bot = MagicMock()
        pbp = make_pbp(home_score=3, away_score=2, last_period_type="SO")
        nhl_command._announce_end(bot, "#nhl.fi", pbp)
        assert bot.send_message.call_args[0][1].endswith("(SO)")


class TestLevelScoreAtFinal:
    """Issue #12, second live case (2026-10-13): a shootout game turned FINAL while the header and the score
    endpoint still said 2-2 with no outcome, and the line went out as "2-2" with no (SO). An NHL game never
    ends level, so such a score is stale: the plays decide it, or the line waits."""

    def _so(self, event_id, team):
        return goal_play(event_id=event_id, event_owner_team_id=team, scoring_player_id=100, period_number=5,
                         period_type="SO", time_in_period="00:00", home_score=2, away_score=2)

    def _regulation(self, *scores):
        return [goal_play(event_id=n, event_owner_team_id=10 if h > (scores[n - 2][0] if n > 1 else 0) else 20,
                          scoring_player_id=100, home_score=h, away_score=a)
                for n, (h, a) in enumerate(scores, 1)]

    def _pbp(self, plays, home=2, away=2, last_period_type=None):
        return make_pbp(home_id=10, away_id=20, plays=plays, roster=[roster_spot(100)],
                        home_score=home, away_score=away, last_period_type=last_period_type)

    def _final(self, nhl_command, pbp, hold_allowed=False):
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_attendance", return_value=None):
            record = nhl_command._announce_end(bot, "#nhl.fi", pbp, hold_allowed=hold_allowed)
        return record, [c[0][1] for c in bot.send_message.call_args_list]

    def test_the_real_case_a_shootout_won_by_the_away_side(self, nhl_command, capsys):
        # 0-1, 0-2, 1-2, 2-2 in regulation, then away, home, away in the shootout (the 2026-10-13 game)
        plays = self._regulation((0, 1), (0, 2), (1, 2), (2, 2)) + [self._so(10, 20), self._so(11, 10), self._so(12, 20)]
        record, sent = self._final(nhl_command, self._pbp(plays))
        assert sent == [f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 2-3 Florida Panthers (SO)"]
        assert record["text"] == "CAR 2-3 FLA (SO)"
        assert "level at 2-2 at FINAL, the plays say 2-3 SO; using the plays'" in capsys.readouterr().out

    def test_a_shootout_won_by_the_home_side(self, nhl_command):
        plays = self._regulation((1, 0), (1, 1)) + [self._so(10, 10), self._so(11, 20), self._so(12, 10)]
        plays[-3:] = [self._so(10, 10), self._so(11, 20), self._so(12, 10)]
        for play in plays[-3:]:
            play["details"]["homeScore"] = play["details"]["awayScore"] = 1
        _, sent = self._final(nhl_command, self._pbp(plays, home=1, away=1))
        assert sent[0].endswith("Carolina Hurricanes 2-1 Florida Panthers (SO)")

    def test_a_stale_overtime_game_reads_the_overtime_goals_running_score(self, nhl_command):
        plays = self._regulation((1, 0), (1, 1)) + [
            goal_play(event_id=9, event_owner_team_id=20, period_number=4, period_type="OT", time_in_period="02:11",
                      scoring_player_id=100, home_score=1, away_score=2)]
        _, sent = self._final(nhl_command, self._pbp(plays, home=1, away=1))
        assert sent[0].endswith("Carolina Hurricanes 1-2 Florida Panthers (OT)")

    def test_a_level_result_the_plays_cannot_decide_is_held_back_when_allowed(self, nhl_command, capsys):
        plays = self._regulation((1, 0), (1, 1)) + [self._so(10, 10), self._so(11, 20)]  # shootout level so far
        record, sent = self._final(nhl_command, self._pbp(plays, home=1, away=1), hold_allowed=True)
        assert record is None and sent == []
        assert "FINAL held back, level at 1-1 and the plays do not decide it" in capsys.readouterr().out

    def test_without_goals_in_the_feed_a_level_result_is_held_back_too(self, nhl_command):
        record, sent = self._final(nhl_command, make_pbp(home_score=2, away_score=2), hold_allowed=True)
        assert record is None and sent == []

    def test_when_waiting_is_over_the_line_goes_out_as_it_is(self, nhl_command):
        record, sent = self._final(nhl_command, make_pbp(home_score=2, away_score=2), hold_allowed=False)
        assert sent == [f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 2-2 Florida Panthers"]
        assert record["text"] == "CAR 2-2 FLA"

    def test_a_decided_header_is_never_second_guessed(self, nhl_command, capsys):
        plays = self._regulation((1, 0), (2, 0), (2, 1))
        _, sent = self._final(nhl_command, self._pbp(plays, home=2, away=1))
        assert sent[0].endswith("Carolina Hurricanes 2-1 Florida Panthers")
        assert "the plays say" not in capsys.readouterr().out

    @pytest.mark.parametrize("plays, expected", [
        ([], None),
        ([goal_play(event_id=1, home_score=1, away_score=0)], (1, 0, "REG")),
        ([goal_play(event_id=1, home_score=1, away_score=1)], None),
        ([goal_play(event_id=1, home_score=None, away_score=None)], None),
        ([goal_play(event_id=1, period_number=4, period_type="OT", time_in_period="01:00", home_score=0, away_score=1)], (0, 1, "OT")),
    ])
    def test_result_from_plays(self, nhl_command, plays, expected):
        assert nhl_command._result_from_plays(self._pbp(plays)) == expected

    def test_a_shootout_with_no_regulation_goals_starts_from_nil_nil(self, nhl_command):
        plays = [self._so(10, 10), self._so(11, 20), self._so(12, 10)]
        for play in plays:
            play["details"]["homeScore"] = play["details"]["awayScore"] = 0
        assert nhl_command._result_from_plays(self._pbp(plays, home=0, away=0)) == (1, 0, "SO")

    def test_a_shootout_goal_without_a_running_score_before_it_is_not_decided(self, nhl_command):
        plays = [goal_play(event_id=1, home_score=None, away_score=None), self._so(10, 10)]
        assert nhl_command._result_from_plays(self._pbp(plays)) is None


class TestOvertimeMarkFromPlays:
    """Issue #12, third live case (2026-10-22): a 3-2 overtime win went out without "(OT)" in one channel
    (the header and the score endpoint named no overtime yet) and with it 13 s later in the other. A decided
    score is not stale, so the level-score guard did not apply; the last goal's own period carries the mark."""

    HOME, AWAY = 10, 20

    def _goal(self, event_id, team, home, away, period=1, period_type="REG", time_in_period="05:00"):
        return goal_play(event_id=event_id, event_owner_team_id=team, scoring_player_id=100, period_number=period,
                         period_type=period_type, time_in_period=time_in_period, home_score=home, away_score=away)

    def _overtime_win(self):  # 1-0, 1-1, 2-1, 2-2 in regulation, 3-2 for the home side in overtime
        return [self._goal(1, self.HOME, 1, 0), self._goal(2, self.AWAY, 1, 1, 2), self._goal(3, self.HOME, 2, 1, 2),
                self._goal(4, self.AWAY, 2, 2, 3), self._goal(5, self.HOME, 3, 2, 4, "OT", "03:47")]

    def _final(self, nhl_command, pbp, scores=None, slate=None):
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_scores", return_value=scores or {}), \
                patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", pbp, slate)
        return bot.send_message.call_args[0][1]

    def _pbp(self, plays, home, away, last_period_type=None):
        return make_pbp(home_id=self.HOME, away_id=self.AWAY, plays=plays, roster=[roster_spot(100)],
                        home_score=home, away_score=away, last_period_type=last_period_type)

    def test_an_overtime_win_with_no_mark_in_the_header_gets_one_from_the_plays(self, nhl_command, capsys):
        message = self._final(nhl_command, self._pbp(self._overtime_win(), 3, 2))
        assert message.endswith("Carolina Hurricanes 3-2 Florida Panthers (OT)")
        assert "FINAL without an overtime mark, the plays say 3-2 OT; using the plays' mark" in capsys.readouterr().out

    def test_the_same_when_the_score_endpoint_names_no_period_type_either(self, nhl_command):
        official = {"id": 2026010026, "gameDate": "2026-10-22", "gameState": "OVER",
                    "homeTeam": {"abbrev": "CAR", "score": 3}, "awayTeam": {"abbrev": "FLA", "score": 2}}
        message = self._final(nhl_command, self._pbp(self._overtime_win(), 3, 2), {2026010026: official}, "2026-10-22")
        assert message.endswith("3-2 Florida Panthers (OT)")

    def test_a_shootout_win_with_no_mark_gets_it_from_the_plays(self, nhl_command):
        so = [self._goal(10 + n, team, 2, 2, 5, "SO", "00:00") for n, team in enumerate((self.AWAY, self.HOME, self.HOME))]
        plays = [self._goal(1, self.HOME, 1, 0), self._goal(2, self.AWAY, 1, 1, 2), self._goal(3, self.HOME, 2, 1, 2),
                 self._goal(4, self.AWAY, 2, 2, 3)] + so
        message = self._final(nhl_command, self._pbp(plays, 3, 2))
        assert message.endswith("Carolina Hurricanes 3-2 Florida Panthers (SO)")

    def test_a_regulation_win_gets_no_mark(self, nhl_command, capsys):
        plays = [self._goal(1, self.HOME, 1, 0), self._goal(2, self.HOME, 2, 0, 2)]
        message = self._final(nhl_command, self._pbp(plays, 2, 0))
        assert message.endswith("Carolina Hurricanes 2-0 Florida Panthers")
        assert "overtime mark" not in capsys.readouterr().out

    def test_a_mark_the_header_already_has_is_left_alone(self, nhl_command, capsys):
        message = self._final(nhl_command, self._pbp(self._overtime_win(), 3, 2, "OT"))
        assert message.endswith("3-2 Florida Panthers (OT)") and "overtime mark" not in capsys.readouterr().out

    def test_plays_that_disagree_with_the_score_do_not_add_a_mark(self, nhl_command):
        # the header says 4-2; the plays end 3-2: not the same game state, so neither is trusted over the other
        message = self._final(nhl_command, self._pbp(self._overtime_win(), 4, 2))
        assert message.endswith("4-2 Florida Panthers")


class TestFinalHeldAcrossPolls:
    """The tracker side of the hold: no FINAL: line while the result is level, the game not ended, and a line
    after FINAL_HOLD_POLLS polls at the latest."""

    def _poll(self, nhl_command, bot, pbp):
        game = make_schedule_game(gid=1, game_state="FINAL", home_score=2, away_score=2)
        with patch.object(nhl_command, "_fetch_tracked_games", return_value={1: game}), \
                patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
                patch.object(nhl_command, "_fetch_scores", return_value={}), \
                patch.object(nhl_command, "_fetch_attendance", return_value=18000):
            return nhl_command._poll_once(bot, "#nhl.fi")

    def _finals(self, bot):
        return [c[0][1] for c in bot.send_message.call_args_list if "FINAL:" in c[0][1]]

    def _tracker(self, nhl_command):
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        }}

    def test_a_level_result_is_held_then_posted_once_the_plays_decide_it(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        level = make_pbp(game_id=1, home_score=2, away_score=2, game_state="FINAL")
        assert self._poll(nhl_command, bot, level) is False  # the tracker keeps running
        assert self._poll(nhl_command, bot, level) is False
        assert self._finals(bot) == []
        decided = make_pbp(game_id=1, home_score=2, away_score=3, game_state="FINAL", last_period_type="SO")
        assert self._poll(nhl_command, bot, decided) is True
        assert self._finals(bot) == [f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 2-3 Florida Panthers (SO) | Yleisöä: 18000"]
        self._poll(nhl_command, bot, decided)
        assert len(self._finals(bot)) == 1

    def test_after_the_hold_limit_the_level_line_goes_out(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        level = make_pbp(game_id=1, home_score=2, away_score=2, game_state="FINAL")
        for _ in range(nhl_command.FINAL_HOLD_POLLS):
            assert self._poll(nhl_command, bot, level) is False
        assert self._finals(bot) == []
        assert self._poll(nhl_command, bot, level) is True
        assert self._finals(bot) == [f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 2-2 Florida Panthers | Yleisöä: 18000"]


class TestFailedFinalFetch:
    """Issue #26: a game that turns FINAL in a poll whose play-by-play request
    fails is not marked as ended, so the next poll still sends its FINAL: line."""

    def _poll(self, nhl_command, bot, pbp):
        game = make_schedule_game(gid=1, game_state="FINAL", home_score=3, away_score=2)
        with patch.object(nhl_command, "_fetch_tracked_games", return_value={1: game}), \
                patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
                patch.object(nhl_command, "_fetch_attendance", return_value=18000):
            return nhl_command._poll_once(bot, "#nhl.fi")

    def _finals(self, bot):
        return [c[0][1] for c in bot.send_message.call_args_list if "FINAL:" in c[0][1]]

    def test_the_final_line_is_sent_by_the_next_poll_and_only_once(self, nhl_command):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        }}
        pbp = make_pbp(game_id=1, home_score=3, away_score=2, game_state="FINAL")
        assert self._poll(nhl_command, bot, None) is False  # the tracker must not stop on the failed poll
        assert self._finals(bot) == []
        assert self._poll(nhl_command, bot, pbp) is True
        assert len(self._finals(bot)) == 1
        self._poll(nhl_command, bot, pbp)
        assert len(self._finals(bot)) == 1


class TestFetchAttendance:
    REAL_MARKUP = '<td align="center" style="font-size: 10px;font-weight:bold">Attendance 19,250&nbsp;at&nbsp;Lenovo Center</td>'

    def _pbp(self):
        return make_pbp(game_id=2026020004, season=20262027)

    def test_parses_the_real_report_markup(self, nhl_command):
        with patch.object(nhl_command.session, "get", return_value=html_response(self.REAL_MARKUP)):
            assert nhl_command._fetch_attendance(self._pbp()) == 19250

    def test_builds_the_report_url_from_season_and_game_id(self, nhl_command):
        with patch.object(nhl_command.session, "get", return_value=html_response(self.REAL_MARKUP)) as mock_get:
            nhl_command._fetch_attendance(self._pbp())
        assert mock_get.call_args.args[0] == "https://www.nhl.com/scores/htmlreports/20262027/GS020004.HTM"
        assert mock_get.call_args.kwargs["headers"] == {"Accept": "text/html"}  # the session default is JSON

    @pytest.mark.parametrize("html, expected", [
        ("Attendance 17,850 at TD Garden", 17850),
        ("<b>Attendance</b> <b>10,226</b>", 10226),
        ("attendance 9,999", 9999),
    ])
    def test_tolerates_markup_and_spacing_variants(self, nhl_command, html, expected):
        with patch.object(nhl_command.session, "get", return_value=html_response(html)):
            assert nhl_command._fetch_attendance(self._pbp()) == expected

    def test_report_not_published_yet_returns_none(self, nhl_command, capsys):
        with patch.object(nhl_command.session, "get", return_value=html_response("", status_code=404)):
            assert nhl_command._fetch_attendance(self._pbp()) is None
        assert "no attendance" in capsys.readouterr().out

    def test_request_failure_returns_none(self, nhl_command):
        with patch.object(nhl_command.session, "get", side_effect=requests.exceptions.ConnectionError):
            assert nhl_command._fetch_attendance(self._pbp()) is None

    @pytest.mark.parametrize("html", ["<html>no figure here</html>", "Attendance TBD", "Attendance 0"])
    def test_page_without_a_usable_figure_returns_none(self, nhl_command, html):
        with patch.object(nhl_command.session, "get", return_value=html_response(html)):
            assert nhl_command._fetch_attendance(self._pbp()) is None

    def test_missing_season_or_id_makes_no_request(self, nhl_command):
        nhl_command.session.get.reset_mock()
        assert nhl_command._fetch_attendance(make_pbp(game_id=2026020004)) is None  # no season
        assert nhl_command._fetch_attendance({"season": 20262027}) is None  # no id
        nhl_command.session.get.assert_not_called()


class TestFinalWithAttendance:
    def test_final_line_includes_attendance_like_liiga(self, nhl_command):
        bot = MagicMock()
        pbp = make_pbp(home_score=3, away_score=2, last_period_type="OT")
        with patch.object(nhl_command, "_fetch_attendance", return_value=18347):
            nhl_command._announce_end(bot, "#nhl.fi", pbp)
        assert bot.send_message.call_args[0][1] == (
            f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers (OT) | Yleisöä: 18347"
        )

    def test_final_line_is_unchanged_when_attendance_is_unavailable(self, nhl_command):
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", make_pbp(home_score=5, away_score=4))
        message = bot.send_message.call_args[0][1]
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 5-4 Florida Panthers"

    def test_a_game_ending_during_a_poll_announces_it_with_attendance(self, nhl_command):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False},
        }}
        game = make_schedule_game(gid=1, home_id=10, away_id=20, game_state="FINAL")
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, home_score=5, away_score=2, game_state="FINAL")

        with patch.object(nhl_command, "_fetch_today_games", return_value={1: game}), \
             patch.object(nhl_command, "_fetch_play_by_play", return_value=pbp), \
             patch.object(nhl_command, "_fetch_attendance", return_value=19088):
            nhl_command._poll_once(bot, "#nhl.fi")

        assert bot.send_message.call_args[0][1].endswith("| Yleisöä: 19088")


class TestFinalScoreFromScoreEndpoint:
    """Issue #12: the play-by-play header can lag the plays at the end of a
    game (posted 2-2 and no OT for a 3-2 overtime win); the score endpoint
    already had the right result, so the FINAL: line reads it."""

    GID = 2026010026
    SLATE = "2026-10-02"

    def _official(self, home, away, last_period="OT", state="OVER", gid=GID):
        game = {"id": gid, "gameDate": self.SLATE, "gameState": state,
                "homeTeam": {"abbrev": "CAR", "score": home}, "awayTeam": {"abbrev": "FLA", "score": away}}
        if last_period:
            game["gameOutcome"] = {"lastPeriodType": last_period}
        return game

    def _final(self, nhl_command, pbp, scores, slate=SLATE):
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_scores", return_value=scores), \
                patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", pbp, slate)
        return bot.send_message.call_args[0][1]

    def test_stale_header_is_replaced_by_the_official_score_and_ot_mark(self, nhl_command):
        stale = make_pbp(home_score=2, away_score=2, last_period_type=None)  # the real shape of the incident
        message = self._final(nhl_command, stale, {self.GID: self._official(3, 2)})
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers (OT)"

    def test_shootout_winner_comes_from_the_official_score(self, nhl_command):
        stale = make_pbp(home_score=2, away_score=2, last_period_type=None)
        message = self._final(nhl_command, stale, {self.GID: self._official(3, 2, last_period="SO")})
        assert message.endswith("Carolina Hurricanes 3-2 Florida Panthers (SO)")

    def test_a_regulation_game_has_no_suffix(self, nhl_command):
        message = self._final(nhl_command, make_pbp(home_score=5, away_score=4),
                              {self.GID: self._official(5, 4, last_period="REG")})
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 5-4 Florida Panthers"

    @pytest.mark.parametrize("scores", [
        None,                                            # request failed
        {},                                              # game not listed
        {GID + 1: {"id": GID + 1}},                      # another game only
        {GID: {"id": GID, "gameState": "OVER"}},         # no numeric score
    ])
    def test_falls_back_to_the_header_when_the_official_result_is_unavailable(self, nhl_command, scores):
        pbp = make_pbp(home_score=3, away_score=2, last_period_type="OT")
        message = self._final(nhl_command, pbp, scores)
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers (OT)"

    def test_an_endpoint_game_without_game_outcome_keeps_the_headers_ot_mark(self, nhl_command, capsys):
        """Issue #27: no gameOutcome is not a statement that it was regulation."""
        pbp = make_pbp(game_id=self.GID, home_score=3, away_score=2, last_period_type="OT")
        message = self._final(nhl_command, pbp, {self.GID: self._official(3, 2, last_period=None)})
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers (OT)"
        assert capsys.readouterr().out == ""

    def test_the_endpoint_score_is_still_used_when_it_has_no_game_outcome(self, nhl_command):
        stale = make_pbp(game_id=self.GID, home_score=2, away_score=2, last_period_type=None)
        message = self._final(nhl_command, stale, {self.GID: self._official(3, 2, last_period=None)})
        assert message == f"{nhl_command.FINAL_PREFIX} Carolina Hurricanes 3-2 Florida Panthers"

    def test_a_header_that_disagrees_is_logged_with_both_values(self, nhl_command, capsys):
        stale = make_pbp(game_id=self.GID, home_score=2, away_score=2, last_period_type=None)
        self._final(nhl_command, stale, {self.GID: self._official(3, 2)})
        assert (f"NHL: game {self.GID}: the play-by-play header says 2-2 REG at FINAL, "
                "the score endpoint says 3-2 OT; using the endpoint's") in capsys.readouterr().out

    def test_a_header_that_agrees_logs_nothing(self, nhl_command, capsys):
        for header_type, official_type in ((None, "REG"), ("OT", "OT")):
            pbp = make_pbp(game_id=self.GID, home_score=3, away_score=2, last_period_type=header_type)
            self._final(nhl_command, pbp, {self.GID: self._official(3, 2, last_period=official_type)})
        assert capsys.readouterr().out == ""

    def test_a_missing_official_result_is_logged(self, nhl_command, capsys):
        self._final(nhl_command, make_pbp(game_id=self.GID, home_score=3, away_score=2), {})
        assert (f"NHL: game {self.GID}: no result from the score endpoint for the FINAL line, "
                "using the play-by-play header") in capsys.readouterr().out

    def test_without_a_slate_nothing_is_logged(self, nhl_command, capsys):
        nhl_command._announce_end(MagicMock(), "#nhl.fi", make_pbp(home_score=3, away_score=2))
        assert "score endpoint" not in capsys.readouterr().out

    def test_without_a_slate_no_request_is_made(self, nhl_command):
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_scores") as fetch, \
                patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", make_pbp(home_score=3, away_score=2))
        fetch.assert_not_called()
        assert "3-2" in bot.send_message.call_args[0][1]

    def test_a_copy_cached_a_while_ago_is_never_used(self, nhl_command):
        """!nhl now may have cached the pre-goal board some seconds ago."""
        old = {"games": [{**self._official(2, 2, last_period=None, state="LIVE")}]}
        new = {"games": [self._official(3, 2)]}
        nhl_command.session.get.side_effect = [make_response(old), make_response(new)]
        assert nhl_command._fetch_scores(self.SLATE)[self.GID]["homeTeam"]["score"] == 2
        stamp, games = nhl_command._score_cache[self.SLATE]
        nhl_command._score_cache[self.SLATE] = (stamp - nhl_command.FINAL_SCORE_MAX_AGE_SECONDS - 1, games)
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", make_pbp(home_score=2, away_score=2), self.SLATE)
        assert "3-2" in bot.send_message.call_args[0][1]

    def test_games_ending_in_the_same_poll_share_one_score_request(self, nhl_command):
        """Issue #28: each FINAL used to throw the cached copy away and fetch /score again."""
        nhl_command.session.get.side_effect = [make_response({"games": [self._official(3, 2), self._official(2, 1, gid=self.GID + 1)]})]
        bot = MagicMock()
        with patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._announce_end(bot, "#nhl.fi", make_pbp(game_id=self.GID, home_score=2, away_score=2), self.SLATE)
            nhl_command._announce_end(bot, "#nhl.fi", make_pbp(game_id=self.GID + 1, home_score=1, away_score=1), self.SLATE)
        assert nhl_command.session.get.call_count == 1
        assert "3-2" in bot.send_message.call_args_list[0][0][1] and "2-1" in bot.send_message.call_args_list[1][0][1]

    def test_the_poll_looks_the_result_up_on_the_games_own_slate(self, nhl_command):
        bot = MagicMock()
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            1: {"home_id": 10, "away_id": 20, "announced": {}, "ended": False, "slate": self.SLATE},
        }}
        game = make_schedule_game(gid=1, game_state="FINAL", home_score=2, away_score=2)
        with patch.object(nhl_command, "_fetch_tracked_games", return_value={1: game}), \
                patch.object(nhl_command, "_fetch_play_by_play", return_value=make_pbp(game_id=1, home_score=2, away_score=2)), \
                patch.object(nhl_command, "_fetch_scores", return_value={1: self._official(3, 2, gid=1)}) as fetch, \
                patch.object(nhl_command, "_fetch_attendance", return_value=None):
            nhl_command._poll_once(bot, "#nhl.fi")
        fetch.assert_called_once_with(self.SLATE, max_age=nhl_command.FINAL_SCORE_MAX_AGE_SECONDS)
        assert "3-2" in bot.send_message.call_args[0][1]


class TestAttendanceSummary:
    """Issue #8: the game report's attendance figure shows up 1.5-3 minutes
    after the final horn in about two games of three, so a FINAL: line often
    goes out without it. A missing figure is retried for a few minutes, and
    one results list with every figure follows when the slate is over."""

    SLATE = "2026-10-05"

    def _tracker(self, nhl_command, ended=(False, False)):
        nhl_command._channels["#nhl.fi"] = {"stop_event": MagicMock(), "thread": None, "games": {
            gid: {"home_id": 10, "away_id": 20, "announced": {}, "ended": was_ended, "slate": self.SLATE}
            for gid, was_ended in zip((1, 2), ended)
        }}

    def _poll(self, nhl_command, bot, states, attendance, pbps=None):
        """One poll; `attendance` answers _fetch_attendance by game id."""
        games = {gid: make_schedule_game(gid=gid, game_state=state, home_score=3, away_score=2)
                 for gid, state in states.items()}
        pbps = pbps or {gid: make_pbp(game_id=gid, home_score=3, away_score=2, game_state="FINAL",
                                      season=20262027, home_abbrev=f"H{gid}", away_abbrev=f"A{gid}",
                                      last_period_type="OT" if gid == 2 else None)
                        for gid in states}
        for gid, pbp in pbps.items():
            pbp["startTimeUTC"] = f"2026-10-05T23:{gid:02d}:00Z"
        with patch.object(nhl_command, "_fetch_tracked_games", return_value=games), \
                patch.object(nhl_command, "_fetch_play_by_play", side_effect=lambda gid: pbps[gid]), \
                patch.object(nhl_command, "_fetch_attendance", side_effect=lambda pbp: attendance.get(pbp["id"])):
            return nhl_command._poll_once(bot, "#nhl.fi")

    def _sent(self, bot):
        return [c[0][1] for c in bot.send_message.call_args_list]

    def test_a_late_figure_is_found_by_a_later_poll_and_the_slate_summary_has_it(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command, ended=(False, True))
        nhl_command._channels["#nhl.fi"]["games"][2]["final"] = {
            "text": "H2 3-2 A2 (OT)", "attendance": 17100, "late": False,
            "report": {"season": 20262027, "id": 2}, "start": "2026-10-05T23:30:00Z", "at": time.monotonic(),
        }
        # the last game ends without its figure: the tracker stays open
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {}) is False
        assert [m for m in self._sent(bot) if "NHL results" in m] == []
        # 30 s later the report has it: summary goes out and the tracker may stop
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {1: 18347}) is True
        assert self._sent(bot)[-1] == "NHL results 6.10.: H1 3-2 A1 (18347), H2 3-2 A2 (OT) (17100)"

    def test_figures_that_were_on_every_final_line_get_no_summary(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {1: 18000, 2: 17000}) is True
        assert not [m for m in self._sent(bot) if "NHL results" in m]

    def test_a_figure_that_never_arrives_is_left_out_after_the_retry_window(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {2: 17000}) is False  # game 1 still pending
        for snap in nhl_command._channels["#nhl.fi"]["games"].values():
            snap["final"]["at"] -= nhl_command.ATTENDANCE_RETRY_SECONDS + 1
        nhl_command._channels["#nhl.fi"]["games"][2]["final"]["late"] = True  # 2's figure arrived after its line
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {}) is True
        assert self._sent(bot)[-1] == "NHL results 6.10.: H1 3-2 A1, H2 3-2 A2 (OT) (17000)"

    def test_retrying_stops_after_the_window(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {})
        for snap in nhl_command._channels["#nhl.fi"]["games"].values():
            snap["final"]["at"] -= nhl_command.ATTENDANCE_RETRY_SECONDS + 1
        with patch.object(nhl_command, "_fetch_attendance") as fetch:
            assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {}) is True
        fetch.assert_not_called()

    def test_games_not_announced_by_the_tracker_are_not_in_the_summary(self, nhl_command):
        """A game that was already over when tracking began has no FINAL: record."""
        bot = MagicMock()
        self._tracker(nhl_command, ended=(True, False))
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {}) is False
        assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {2: 17000}) is True
        assert self._sent(bot)[-1] == "NHL results 6.10.: H2 3-2 A2 (OT) (17000)"

    def test_a_long_slate_is_split_into_several_lines(self, nhl_command):
        bot = MagicMock()
        state = {}
        for gid in range(1, 17):
            state[gid] = {"home_id": 10, "away_id": 20, "announced": {}, "ended": True, "slate": self.SLATE,
                          "final": {"text": f"TEAM{gid} 3-2 OTHER{gid}", "attendance": 17000 + gid, "late": True,
                                    "report": {}, "start": f"2026-10-05T23:{gid:02d}:00Z", "at": time.monotonic()}}
        nhl_command._announce_slate_summary(bot, "#nhl.fi", state)
        messages = self._sent(bot)
        assert len(messages) > 1
        assert all(m.startswith("NHL results 6.10.: ") and len(m.encode()) <= nhl_command.MAX_LINE_BYTES for m in messages)
        assert "TEAM1 3-2 OTHER1 (17001)" in messages[0] and "OTHER16 (17016)" in messages[-1]

    def test_a_figure_missing_at_the_final_is_not_looked_up_again_in_the_same_poll(self, nhl_command):
        """Issue #28: the lookup made for the FINAL line just came back empty; the retry waits for the next poll."""
        bot = MagicMock()
        self._tracker(nhl_command)
        games = {gid: make_schedule_game(gid=gid, game_state="FINAL", home_score=3, away_score=2) for gid in (1, 2)}
        pbps = {gid: make_pbp(game_id=gid, home_score=3, away_score=2, game_state="FINAL", season=20262027) for gid in (1, 2)}
        with patch.object(nhl_command, "_fetch_tracked_games", return_value=games), \
                patch.object(nhl_command, "_fetch_play_by_play", side_effect=lambda gid: pbps[gid]), \
                patch.object(nhl_command, "_fetch_attendance", return_value=None) as fetch:
            nhl_command._poll_once(bot, "#nhl.fi")
            assert fetch.call_count == 2  # one per game, for the FINAL line
            nhl_command._poll_once(bot, "#nhl.fi")
            assert fetch.call_count == 4  # the next poll looks again for both

    def test_the_summary_lists_games_by_start_time(self, nhl_command):
        bot = MagicMock()

        def snap(text, start):
            return {"slate": self.SLATE, "final": {"text": text, "attendance": 17000, "late": True,
                                                   "report": {}, "start": start, "at": time.monotonic()}}
        state = {1: snap("LATE 1-0 GAME", "2026-10-06T00:00:00Z"), 2: snap("EARLY 2-1 GAME", "2026-10-05T23:00:00Z")}
        nhl_command._announce_slate_summary(bot, "#nhl.fi", state)
        assert self._sent(bot)[-1] == "NHL results 6.10.: EARLY 2-1 GAME (17000), LATE 1-0 GAME (17000)"

    def test_a_failure_in_the_follow_up_never_stops_the_poll(self, nhl_command):
        bot = MagicMock()
        self._tracker(nhl_command)
        with patch.object(nhl_command, "_retry_missing_attendance", side_effect=RuntimeError("boom")):
            assert self._poll(nhl_command, bot, {1: "FINAL", 2: "FINAL"}, {1: 18000, 2: 17000}) is True
        assert any("FINAL:" in m for m in self._sent(bot))
