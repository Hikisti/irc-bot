import itertools
import datetime
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from irc_format import BOLD, RESET, ORANGE
from liiga_command import LiigaCommand
from tests.conftest import join_channel_thread


def make_game(gid=1, home="HIFK", away="Ilves", home_goals=None, away_goals=None,
              started=True, ended=False, finished_type="ACTIVE_OR_NOT_STARTED",
              start="2026-09-05T14:00:00Z", end=None):
    game = {
        "id": gid,
        "start": start,
        "homeTeam": {
            "teamName": home,
            "goals": len(home_goals or []),
            "goalEvents": home_goals or [],
        },
        "awayTeam": {
            "teamName": away,
            "goals": len(away_goals or []),
            "goalEvents": away_goals or [],
        },
        "periods": [
            {"index": 1, "startTime": 0, "endTime": 1200},
            {"index": 2, "startTime": 1200, "endTime": 2400},
            {"index": 3, "startTime": 2400, "endTime": 3600},
        ],
        "started": started,
        "ended": ended,
        "finishedType": finished_type,
    }
    if end is not None:
        game["end"] = end  # the scheduled end, as the feed carries it
    return game


_event_ids = itertools.count(1)


def goal_event(period=1, game_time=125, home_score=1, away_score=0,
                first="Kristian", last="Vesalainen", assists=None, tags=None, event_id=None):
    return {
        "eventId": next(_event_ids) if event_id is None else event_id,  # every real goal event carries one
        "period": period,
        "gameTime": game_time,
        "homeTeamScore": home_score,
        "awayTeamScore": away_score,
        "scorerPlayer": {"firstName": first, "lastName": last},
        "assistantPlayers": assists or [],
        "goalTypes": tags or [],
    }


def disallowed_goal_event(period=1, game_time=446):
    # Real shape confirmed live (Ässät-SaiPa, 2026-09-25): a goal
    # overturned by video review stays in goalEvents forever with no
    # scorer and no score change, unlike every real goal.
    return {
        "period": period,
        "gameTime": game_time,
        "homeTeamScore": 0,
        "awayTeamScore": 0,
        "scorerPlayerId": 0,
        "scorerPlayer": None,
        "assistantPlayers": [],
        "goalTypes": ["YV", "VT0"],
    }


# The clock the tests see: 18:00 Helsinki time on the day of make_game's default game (it started at 17:00
# Helsinki time, ends at 20:00). The tracker's rules about a game that never started and about the change of
# day depend on "now", so no test is allowed to depend on the real clock.
FIXED_NOW = datetime.datetime(2026, 9, 5, 15, 0, tzinfo=datetime.timezone.utc).astimezone(LiigaCommand.HELSINKI_TZ)


@pytest.fixture
def liiga_command():
    command = LiigaCommand()
    command._now = lambda: FIXED_NOW
    return command


class TestStartDoesNotBlock:
    def test_start_returns_before_fetch_completes(self, liiga_command):
        """The reply to !liiga start must come back immediately, even if the
        Liiga API is slow to respond - the lookup happens on a background
        thread, never on the caller's (IRC listener) thread."""
        release_fetch = threading.Event()

        def slow_fetch():
            assert release_fetch.wait(timeout=2), "test setup failed to release fetch"
            return {1: make_game()}

        bot = MagicMock()
        with patch.object(liiga_command, "_fetch_today_games", side_effect=slow_fetch):
            start = time.time()
            result = liiga_command.execute("start", irc_bot=bot, channel="#chan")
            elapsed = time.time() - start

            assert elapsed < 1, "execute() blocked on the network fetch"
            assert "Checking" in result

            release_fetch.set()
            join_channel_thread(liiga_command, "#chan")

        bot.send_message.assert_called_once()
        message = bot.send_message.call_args[0][1]
        assert "Tracking 1 Liiga game" in message
        assert "17:00 HIFK 0-0 Ilves" in message  # make_game() defaults to started

    def test_start_reserves_slot_immediately_against_races(self, liiga_command):
        """A second !liiga start while the first lookup is still in flight
        must be rejected, not race past the reservation."""
        release_fetch = threading.Event()

        def slow_fetch():
            release_fetch.wait(timeout=2)
            return {1: make_game()}

        bot = MagicMock()
        with patch.object(liiga_command, "_fetch_today_games", side_effect=slow_fetch):
            liiga_command.execute("start", irc_bot=bot, channel="#chan")
            second = liiga_command.execute("start", irc_bot=bot, channel="#chan")
            release_fetch.set()
            join_channel_thread(liiga_command, "#chan")

        assert "Already tracking" in second


class TestRun:
    """Exercises _run() directly (the background-thread entry point) so the
    initial-lookup outcomes can be asserted deterministically."""

    def test_no_games_today(self, liiga_command):
        bot = MagicMock()
        stop_event = threading.Event()
        liiga_command._channels["#chan"] = {"stop_event": stop_event, "thread": None, "games": {}}

        with patch.object(liiga_command, "_fetch_today_games", return_value={}):
            liiga_command._run(bot, "#chan", stop_event)

        bot.send_message.assert_called_once_with("#chan", "No Liiga games scheduled today.")
        assert "#chan" not in liiga_command._channels

    def test_api_unreachable(self, liiga_command):
        bot = MagicMock()
        stop_event = threading.Event()
        liiga_command._channels["#chan"] = {"stop_event": stop_event, "thread": None, "games": {}}

        with patch.object(liiga_command, "_fetch_today_games", return_value=None):
            liiga_command._run(bot, "#chan", stop_event)

        bot.send_message.assert_called_once_with("#chan", "Error: could not reach the Liiga API.")
        assert "#chan" not in liiga_command._channels

    def test_unexpected_exception_does_not_propagate(self, liiga_command):
        """A crash anywhere in the initial lookup must not escape _run() -
        this runs on a bare background thread with no other safety net."""
        bot = MagicMock()
        stop_event = threading.Event()
        liiga_command._channels["#chan"] = {"stop_event": stop_event, "thread": None, "games": {}}

        with patch.object(liiga_command, "_fetch_today_games", side_effect=RuntimeError("boom")):
            liiga_command._run(bot, "#chan", stop_event)  # must not raise

        bot.send_message.assert_called_once_with("#chan", "Error: could not reach the Liiga API.")

    def test_refuses_to_start_more_than_15_minutes_before_first_game(self, liiga_command):
        # Confirms LiigaCommand's own START_TIME_KEY ("start") wiring into
        # the shared early-start guard - the guard's own logic is covered
        # exhaustively in test_live_tracker_command.py.
        bot = MagicMock()
        stop_event = threading.Event()
        liiga_command._channels["#chan"] = {"stop_event": stop_event, "thread": None, "games": {}}

        future_start = (
            datetime.datetime.now(liiga_command.HELSINKI_TZ) + datetime.timedelta(minutes=30)
        ).isoformat()
        with patch.object(
            liiga_command, "_fetch_today_games",
            return_value={1: make_game(start=future_start)},
        ):
            liiga_command._run(bot, "#chan", stop_event)

        message = bot.send_message.call_args[0][1]
        assert "Too early to track" in message
        assert "#chan" not in liiga_command._channels

    def test_all_games_already_ended_sends_one_message_not_tracking_then_stopped(self, liiga_command):
        # Confirms LiigaCommand's own ENDED_STATE_KEY ("ended") wiring
        # into the shared already-finished guard - the guard's own logic
        # is covered exhaustively in test_live_tracker_command.py.
        bot = MagicMock()
        stop_event = threading.Event()
        liiga_command._channels["#chan"] = {"stop_event": stop_event, "thread": None, "games": {}}

        with patch.object(
            liiga_command, "_fetch_today_games",
            return_value={1: make_game(ended=True), 2: make_game(gid=2, ended=True)},
        ):
            liiga_command._run(bot, "#chan", stop_event)

        bot.send_message.assert_called_once_with(
            "#chan", "All of today's Liiga games have already finished.",
        )
        assert "#chan" not in liiga_command._channels

    def test_stopped_before_lookup_finishes_sends_nothing(self, liiga_command):
        bot = MagicMock()
        stop_event = threading.Event()
        liiga_command._channels["#chan"] = {"stop_event": stop_event, "thread": None, "games": {}}
        # Simulate !liiga stop having already popped the channel out from
        # under us while the (fake, instant) fetch was "in flight".
        liiga_command._channels.pop("#chan")
        stop_event.set()

        with patch.object(liiga_command, "_fetch_today_games", return_value={1: make_game()}):
            liiga_command._run(bot, "#chan", stop_event)

        bot.send_message.assert_not_called()


class TestStop:
    # "stop without active tracking" and the plain stop/set-event/clear
    # state transition are LiveTrackerCommand's own base behavior,
    # covered there - this class only needs the parts genuinely specific
    # to Liiga: stopping while a real background fetch is in flight.
    def test_stop_signals_thread_and_clears_state(self, liiga_command):
        release_fetch = threading.Event()

        def slow_fetch():
            release_fetch.wait(timeout=2)
            return {1: make_game()}

        bot = MagicMock()
        with patch.object(liiga_command, "_fetch_today_games", side_effect=slow_fetch):
            liiga_command.execute("start", irc_bot=bot, channel="#chan")
            stop_event = liiga_command._channels["#chan"]["stop_event"]

            result = liiga_command.execute("stop", irc_bot=bot, channel="#chan")

            assert "Stopped" in result
            assert stop_event.is_set()
            assert "#chan" not in liiga_command._channels

            release_fetch.set()  # let the orphaned thread finish so it doesn't leak into other tests


def at(hour, minute=0, day=5):
    """An ISO timestamp (UTC) on the test day, for a game's `start` or `end`."""
    return datetime.datetime(2026, 9, day, hour, minute, tzinfo=datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def at_helsinki_midnight_plus(minutes, day=6):
    return datetime.datetime(2026, 9, day, 0, minutes, tzinfo=LiigaCommand.HELSINKI_TZ)


class TestNotPlayed:
    """Issue #37: the feed has no marker for a postponed game; it stays started=false, ended=false, 0-0.
    One that has not started by its scheduled end counts as over, so it cannot keep the tracker alive."""

    def test_a_game_not_started_after_its_scheduled_end_is_not_played(self, liiga_command):
        game = make_game(started=False, start=at(12), end=at(14))   # scheduled to end 17:00 Helsinki, "now" is 18:00
        assert liiga_command._not_played(game) is True and liiga_command._is_over(game) is True

    def test_a_game_not_started_before_its_scheduled_end_is_still_to_come(self, liiga_command):
        game = make_game(started=False, start=at(14), end=at(17))
        assert liiga_command._not_played(game) is False and liiga_command._is_over(game) is False

    def test_a_game_that_started_or_ended_is_never_not_played(self, liiga_command):
        assert liiga_command._not_played(make_game(started=True, ended=False, start=at(1), end=at(2))) is False
        assert liiga_command._not_played(make_game(started=True, ended=True, start=at(1), end=at(2))) is False

    def test_without_an_end_the_scheduled_length_is_three_hours(self, liiga_command):
        late = make_game(started=False, start=at(11))   # 11:00Z + 3 h = 14:00Z < 15:00Z "now"
        soon = make_game(started=False, start=at(13))   # 13:00Z + 3 h = 16:00Z > 15:00Z
        assert liiga_command._not_played(late) is True and liiga_command._not_played(soon) is False

    def test_a_game_with_no_times_at_all_is_never_not_played(self, liiga_command):
        game = make_game(started=False)
        game.pop("start")
        assert liiga_command._not_played(game) is False

    def test_the_stop_condition_ignores_a_postponed_game(self, liiga_command):
        bot = MagicMock()
        played = make_game(gid=1, ended=True, start=at(10), end=at(13))
        postponed = make_game(gid=2, started=False, start=at(10), end=at(13))
        liiga_command._channels["#chan"] = {"stop_event": MagicMock(), "thread": None,
                                            "games": {1: liiga_command._snapshot(played), 2: liiga_command._snapshot(postponed)}}
        with patch.object(liiga_command, "_fetch_today_games", return_value={1: played, 2: postponed}):
            assert liiga_command._poll_once(bot, "#chan") is True

    def test_a_postponed_game_is_never_announced_as_finished(self, liiga_command):
        bot = MagicMock()
        postponed = make_game(gid=2, started=False, start=at(10), end=at(13))
        liiga_command._channels["#chan"] = {"stop_event": MagicMock(), "thread": None,
                                            "games": {2: liiga_command._snapshot(postponed)}}
        with patch.object(liiga_command, "_fetch_today_games", return_value={2: postponed}):
            liiga_command._poll_once(bot, "#chan")
        bot.send_message.assert_not_called()

    def test_a_game_still_to_come_keeps_the_tracker_going(self, liiga_command):
        bot = MagicMock()
        played = make_game(gid=1, ended=True, start=at(10), end=at(13))
        later = make_game(gid=2, started=False, start=at(14), end=at(17))
        liiga_command._channels["#chan"] = {"stop_event": MagicMock(), "thread": None,
                                            "games": {1: liiga_command._snapshot(played), 2: liiga_command._snapshot(later)}}
        with patch.object(liiga_command, "_fetch_today_games", return_value={1: played, 2: later}):
            assert liiga_command._poll_once(bot, "#chan") is False

    def test_a_game_that_does_start_late_is_followed_after_all(self, liiga_command):
        bot = MagicMock()
        postponed = make_game(gid=2, started=False, start=at(10), end=at(13), home_goals=[])
        liiga_command._channels["#chan"] = {"stop_event": MagicMock(), "thread": None,
                                            "games": {2: liiga_command._snapshot(postponed)}}
        started = make_game(gid=2, started=True, start=at(10), end=at(13),
                            home_goals=[goal_event(home_score=1, away_score=0)])
        with patch.object(liiga_command, "_fetch_today_games", return_value={2: started}):
            assert liiga_command._poll_once(bot, "#chan") is False  # it is on now: not over
        assert any("GOAL:" in c.args[1] for c in bot.send_message.call_args_list)

    def test_a_new_postponed_game_added_mid_tracking_does_not_keep_it_alive(self, liiga_command):
        bot = MagicMock()
        liiga_command._channels["#chan"] = {"stop_event": MagicMock(), "thread": None, "games": {}}
        postponed = make_game(gid=2, started=False, start=at(10), end=at(13))
        with patch.object(liiga_command, "_fetch_today_games", return_value={2: postponed}):
            assert liiga_command._poll_once(bot, "#chan") is True

    def test_next_skips_a_day_whose_only_open_game_was_postponed(self, liiga_command):
        postponed = make_game(gid=2, started=False, start=at(10), end=at(13))
        played = make_game(gid=1, ended=True, start=at(10), end=at(13))
        assert liiga_command._all_games_ended({1: played, 2: postponed}) is True

    def test_the_start_guard_says_everything_has_already_finished_when_only_a_postponed_game_is_left(self, liiga_command):
        postponed = make_game(gid=2, started=False, start=at(10), end=at(13))
        played = make_game(gid=1, ended=True, start=at(10), end=at(13))
        state = liiga_command._build_initial_state({1: played, 2: postponed})
        assert all(s[liiga_command.ENDED_STATE_KEY] for s in state.values())


class TestGameDayRollover:
    """Issue #37: the tracker follows one game day and stops when the Helsinki date moves on, so it cannot go on
    into the next day announcing games nobody asked it to follow."""

    def _tracking(self, liiga_command, game):
        liiga_command._channels["#chan"] = {"stop_event": MagicMock(), "thread": None,
                                            "games": {game["id"]: liiga_command._snapshot(game)}}

    def test_at_the_change_of_day_it_stops_whatever_the_feed_says(self, liiga_command):
        bot = MagicMock()
        game = make_game(gid=1, started=True, ended=False, start=at(14), end=at(17))   # 5 Sept, never ends in the feed
        self._tracking(liiga_command, game)
        liiga_command._now = lambda: at_helsinki_midnight_plus(5)
        with patch.object(liiga_command, "_fetch_today_games", return_value={1: game}):
            assert liiga_command._poll_once(bot, "#chan") is True

    def test_it_says_so_in_the_journal(self, liiga_command, capsys):
        game = make_game(gid=1, started=True, start=at(14), end=at(17))
        self._tracking(liiga_command, game)
        liiga_command._now = lambda: at_helsinki_midnight_plus(5)
        with patch.object(liiga_command, "_fetch_today_games", return_value={1: game}):
            liiga_command._poll_once(MagicMock(), "#chan")
        assert "Liiga: the game day 2026-09-05 is over, stopping the tracker" in capsys.readouterr().out

    def test_the_next_days_games_are_not_announced(self, liiga_command):
        bot = MagicMock()
        game = make_game(gid=1, started=True, start=at(14), end=at(17))
        self._tracking(liiga_command, game)
        liiga_command._now = lambda: at_helsinki_midnight_plus(5)
        tomorrow = make_game(gid=9, started=True, start=at(14, day=6), home_goals=[goal_event(home_score=1, away_score=0)])
        with patch.object(liiga_command, "_fetch_today_games", return_value={9: tomorrow}):
            assert liiga_command._poll_once(bot, "#chan") is True
        bot.send_message.assert_not_called()

    def test_before_the_change_of_day_nothing_changes(self, liiga_command):
        bot = MagicMock()
        game = make_game(gid=1, started=True, start=at(14), end=at(17))
        self._tracking(liiga_command, game)
        with patch.object(liiga_command, "_fetch_today_games", return_value={1: game}):
            assert liiga_command._poll_once(bot, "#chan") is False

    def test_a_game_late_in_the_evening_is_still_followed_at_half_past_ten(self, liiga_command):
        bot = MagicMock()
        game = make_game(gid=1, started=True, start=at(16, 30), end=at(19, 30))
        self._tracking(liiga_command, game)
        liiga_command._now = lambda: datetime.datetime(2026, 9, 5, 22, 30, tzinfo=LiigaCommand.HELSINKI_TZ)
        with patch.object(liiga_command, "_fetch_today_games", return_value={1: game}):
            assert liiga_command._poll_once(bot, "#chan") is False

    def test_the_state_records_the_helsinki_day_of_each_game(self, liiga_command):
        assert liiga_command._snapshot(make_game(start=at(14)))["day"] == "2026-09-05"
        assert liiga_command._snapshot(make_game(start=at(22)))["day"] == "2026-09-06"   # 01:00 Helsinki time the next day

    def test_games_from_two_days_do_not_end_the_tracking_while_one_of_them_is_from_today(self, liiga_command):
        assert liiga_command._game_day_is_over({1: {"day": "2026-09-04"}, 2: {"day": "2026-09-05"}}) is False

    def test_the_real_clock_is_in_helsinki_time(self):
        now = LiigaCommand()._now()
        assert now.tzinfo is not None and now.utcoffset() == datetime.datetime.now(LiigaCommand.HELSINKI_TZ).utcoffset()

    def test_an_empty_state_never_triggers_the_rollover(self, liiga_command):
        assert liiga_command._game_day_is_over({}) is False


class TestAnnounceByIdentity:
    """Issue #16: the feed was seen to leave a goal out of one 10 s sample and bring it back in the next
    (2026-10-08, twice). Announcing by the identity of the goal event, not by counting events, means that
    a poll landing on such a dip does not make the tracker announce the goal again."""

    def _run(self, liiga_command, games, ended=False):
        """Polls once per game state in `games` (the first one is the baseline); returns the GOAL: lines."""
        bot = MagicMock()
        liiga_command._channels["#chan"] = {"stop_event": MagicMock(), "thread": None,
                                            "games": {1: liiga_command._snapshot(games[0])}}
        for game in games[1:]:
            with patch.object(liiga_command, "_fetch_today_games", return_value={1: game}):
                liiga_command._poll_once(bot, "#chan")
        return [c.args[1] for c in bot.send_message.call_args_list if "GOAL:" in c.args[1]]

    def test_a_goal_that_leaves_the_feed_for_one_poll_is_not_announced_again(self, liiga_command):
        goal = goal_event(home_score=1, away_score=0)
        before = make_game(home_goals=[])
        with_goal = make_game(home_goals=[goal])
        lines = self._run(liiga_command, [before, with_goal, before, with_goal])
        assert len(lines) == 1

    def test_a_goal_gone_for_several_polls_is_still_announced_once(self, liiga_command):
        goal = goal_event(home_score=1, away_score=0)
        before, with_goal = make_game(home_goals=[]), make_game(home_goals=[goal])
        assert len(self._run(liiga_command, [before, with_goal, before, before, before, with_goal])) == 1

    def test_a_dip_before_the_tracker_ever_saw_the_goal_changes_nothing(self, liiga_command):
        goal = goal_event(home_score=1, away_score=0)
        before, with_goal = make_game(home_goals=[]), make_game(home_goals=[goal])
        assert len(self._run(liiga_command, [before, before, with_goal, with_goal])) == 1

    def test_a_removed_goal_replaced_by_another_in_the_same_poll_is_announced(self, liiga_command):
        """With counting, one goal removed and another added in one poll left the count unchanged: swallowed."""
        first = goal_event(home_score=1, away_score=0, first="Eka")
        replacement = goal_event(home_score=1, away_score=0, first="Toka")
        lines = self._run(liiga_command, [make_game(home_goals=[]), make_game(home_goals=[first]),
                                          make_game(home_goals=[replacement])])
        assert len(lines) == 2 and "Toka" in lines[1]

    def test_two_goals_in_one_poll_are_both_announced_and_one_in_each_team(self, liiga_command):
        lines = self._run(liiga_command, [make_game(home_goals=[], away_goals=[]),
                                          make_game(home_goals=[goal_event(home_score=1, away_score=0)],
                                                    away_goals=[goal_event(home_score=1, away_score=1)])])
        assert len(lines) == 2

    def test_the_same_event_twice_in_one_feed_is_announced_once(self, liiga_command):
        goal = goal_event(home_score=1, away_score=0)
        lines = self._run(liiga_command, [make_game(home_goals=[]), make_game(home_goals=[goal, dict(goal)])])
        assert len(lines) == 1

    def test_goals_already_in_the_feed_when_tracking_starts_are_never_announced(self, liiga_command):
        goal = goal_event(home_score=1, away_score=0)
        game = make_game(home_goals=[goal])
        assert self._run(liiga_command, [game, game, game]) == []

    def test_an_event_without_an_event_id_is_identified_by_period_time_and_scorer(self, liiga_command):
        def bare(game_time):
            e = goal_event(home_score=1, away_score=0, game_time=game_time)
            del e["eventId"]
            return e
        goal = bare(300)
        before, with_goal = make_game(home_goals=[]), make_game(home_goals=[goal])
        assert len(self._run(liiga_command, [before, with_goal, before, make_game(home_goals=[dict(goal)])])) == 1

    def test_a_disallowed_entry_without_a_scorer_is_never_tracked(self, liiga_command):
        game = make_game(home_goals=[disallowed_goal_event()])
        assert liiga_command._snapshot(game)["announced"]["homeTeam"] == set()

    def test_the_announced_set_only_grows(self, liiga_command):
        goal = goal_event(home_score=1, away_score=0)
        state = liiga_command._snapshot(make_game(home_goals=[goal]))
        dipped = liiga_command._snapshot(make_game(home_goals=[]), state)
        assert dipped["announced"]["homeTeam"] == state["announced"]["homeTeam"] and dipped["missing"]["homeTeam"] == 1
        assert liiga_command._snapshot(make_game(home_goals=[goal]), dipped)["missing"]["homeTeam"] == 0

    def test_a_change_in_the_number_of_missing_goals_is_journaled(self, liiga_command, capsys):
        goal = goal_event(home_score=1, away_score=0)
        before, with_goal = make_game(home_goals=[]), make_game(home_goals=[goal])
        self._run(liiga_command, [before, with_goal, before, with_goal, with_goal])
        out = capsys.readouterr().out
        assert out.count("announced goals missing from the feed 0 -> 1") == 1
        assert out.count("announced goals missing from the feed 1 -> 0") == 1

    def test_nothing_is_journaled_while_nothing_is_missing(self, liiga_command, capsys):
        goal = goal_event(home_score=1, away_score=0)
        self._run(liiga_command, [make_game(home_goals=[]), make_game(home_goals=[goal]), make_game(home_goals=[goal])])
        assert "missing from the feed" not in capsys.readouterr().out


class TestPollOnce:
    def _seed(self, liiga_command, channel, games):
        liiga_command._channels[channel] = {
            "stop_event": MagicMock(),
            "thread": None,
            "games": {gid: liiga_command._snapshot(g) for gid, g in games.items()},
        }

    def test_new_goal_is_announced(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(home_goals=[])})

        updated = {
            1: make_game(
                home_goals=[goal_event(period=1, game_time=125, home_score=1, away_score=0)]
            )
        }
        with patch.object(liiga_command, "_fetch_today_games", return_value=updated):
            liiga_command._poll_once(bot, "#chan")

        bot.send_message.assert_called_once()
        channel, message = bot.send_message.call_args[0]
        assert channel == "#chan"
        assert "GOAL" in message
        assert "Kristian Vesalainen" in message
        assert "HIFK" in message and "1-0" in message and "Ilves" in message
        assert "02:05" in message  # 125s -> 2:05 into period 1

    def test_new_game_appearing_mid_tracking_is_seeded_silently(self, liiga_command):
        # A game not in the previously-tracked set (e.g. added to the
        # schedule after !liiga start already ran) must be seeded as a
        # fresh baseline, not have its already-existing goals replayed as
        # new GOAL: announcements.
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {})  # nothing tracked yet

        new_game = make_game(gid=2, home_goals=[goal_event(home_score=1, away_score=0)], ended=False)
        with patch.object(liiga_command, "_fetch_today_games", return_value={2: new_game}):
            all_ended = liiga_command._poll_once(bot, "#chan")

        bot.send_message.assert_not_called()
        assert all_ended is False
        assert len(liiga_command._channels["#chan"]["games"][2]["announced"]["homeTeam"]) == 1

    def test_new_game_seeding_excludes_disallowed_goal_from_baseline(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {})  # nothing tracked yet

        new_game = make_game(gid=2, home_goals=[disallowed_goal_event()], ended=False)
        with patch.object(liiga_command, "_fetch_today_games", return_value={2: new_game}):
            liiga_command._poll_once(bot, "#chan")

        bot.send_message.assert_not_called()
        assert liiga_command._channels["#chan"]["games"][2]["announced"]["homeTeam"] == set()

    def test_new_game_that_already_ended_counts_toward_all_ended(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {})

        finished_game = make_game(gid=3, ended=True)
        with patch.object(liiga_command, "_fetch_today_games", return_value={3: finished_game}):
            all_ended = liiga_command._poll_once(bot, "#chan")

        assert all_ended is True

    def test_goal_leads_with_the_bolded_score_final_does_not(self, liiga_command):
        # Explicitly the behavior asked for: the score (bolded) leads a
        # GOAL: line, with scorer/assist detail trailing after the pipe -
        # FINAL: is unaffected. Also covers both prefixes' mIRC colors and
        # that both reset formatting so the rest of the line isn't left
        # bold/colored on the user's client.
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(home_goals=[])})

        updated = {1: make_game(
            home_goals=[goal_event(home_score=1, away_score=0)],
            ended=True,
        )}
        with patch.object(liiga_command, "_fetch_today_games", return_value=updated):
            liiga_command._poll_once(bot, "#chan")

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        goal_msg = next(m for m in messages if "GOAL:" in m)
        final_msg = next(m for m in messages if "FINAL:" in m)

        assert goal_msg == (
            f"{liiga_command.GOAL_PREFIX} {BOLD}HIFK 1-0 Ilves{RESET} 02:05 1st | "
            f"HIFK — Kristian Vesalainen"
        )
        # Only the GOAL: prefix's own bold code, not the score too.
        assert final_msg.count(BOLD) == 1
        assert final_msg.startswith(liiga_command.FINAL_PREFIX)
        assert ORANGE in final_msg
        assert RESET in final_msg

    def test_goal_with_assists_and_tag(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(home_goals=[])})

        updated = {
            1: make_game(
                home_goals=[goal_event(
                    assists=[{"firstName": "Luke", "lastName": "Martin"}],
                    tags=["YV"],
                )]
            )
        }
        with patch.object(liiga_command, "_fetch_today_games", return_value=updated):
            liiga_command._poll_once(bot, "#chan")

        message = bot.send_message.call_args[0][1]
        assert "assists: Luke Martin" in message
        assert "YV" in message

    def test_disallowed_video_review_goal_is_not_announced(self, liiga_command):
        # Regression test for a real incident (Ässät-SaiPa, 2026-09-25):
        # a goal disallowed by video review was announced as "GOAL: ...
        # | Ässät — Unknown (YV/VT0)" - reading as a real goal by an
        # unidentified scorer, when actually no goal happened at all.
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(home_goals=[])})

        updated = {1: make_game(home_goals=[disallowed_goal_event()])}
        with patch.object(liiga_command, "_fetch_today_games", return_value=updated):
            liiga_command._poll_once(bot, "#chan")

        bot.send_message.assert_not_called()

    def test_disallowed_goal_does_not_shift_or_block_a_real_goal_after_it(self, liiga_command):
        # The disallowed entry must be filtered out everywhere it's
        # consulted (seeding, counting, slicing) - not just skipped when
        # formatting the message - otherwise it throws off the
        # count-based "already announced" index and either replays it or
        # swallows the real goal that comes after it.
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(home_goals=[disallowed_goal_event()])})

        updated = {1: make_game(home_goals=[
            disallowed_goal_event(),
            goal_event(home_score=1, away_score=0),
        ])}
        with patch.object(liiga_command, "_fetch_today_games", return_value=updated):
            liiga_command._poll_once(bot, "#chan")

        bot.send_message.assert_called_once()
        message = bot.send_message.call_args[0][1]
        assert "Kristian Vesalainen" in message

    def test_no_new_goals_sends_nothing(self, liiga_command):
        bot = MagicMock()
        events = [goal_event()]
        self._seed(liiga_command, "#chan", {1: make_game(home_goals=events)})

        with patch.object(liiga_command, "_fetch_today_games", return_value={1: make_game(home_goals=events)}):
            liiga_command._poll_once(bot, "#chan")

        bot.send_message.assert_not_called()

    def test_game_end_is_announced(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(ended=False)})

        finished = {1: make_game(
            home_goals=[goal_event(home_score=3, away_score=2)],
            away_goals=[goal_event(), goal_event()],
            ended=True,
            finished_type="ENDED_DURING_REGULAR_GAME_TIME",
        )}
        with patch.object(liiga_command, "_fetch_today_games", return_value=finished):
            liiga_command._poll_once(bot, "#chan")

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        assert any("FINAL:" in m for m in messages)
        final_msg = next(m for m in messages if "FINAL:" in m)
        assert "HIFK 1-2 Ilves" in final_msg

    def test_all_ended_returns_true(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(ended=True)})

        with patch.object(liiga_command, "_fetch_today_games", return_value={1: make_game(ended=True)}):
            result = liiga_command._poll_once(bot, "#chan")

        assert result is True

    def test_not_all_ended_returns_false(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(ended=False), 2: make_game(gid=2, ended=True)})

        with patch.object(
            liiga_command,
            "_fetch_today_games",
            return_value={1: make_game(ended=False), 2: make_game(gid=2, ended=True)},
        ):
            result = liiga_command._poll_once(bot, "#chan")

        assert result is False

    def test_a_feed_flipping_ended_back_does_not_announce_the_final_twice(self, liiga_command):
        # seen live: ended true -> false -> true within 30 s (an older cached copy in between)
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(ended=False)})

        for ended in (True, False, True):
            with patch.object(liiga_command, "_fetch_today_games", return_value={1: make_game(ended=ended)}):
                liiga_command._poll_once(bot, "#chan")

        finals = [c.args[1] for c in bot.send_message.call_args_list if "FINAL" in c.args[1]]
        assert len(finals) == 1

    def test_a_game_seen_as_ended_stays_ended_in_the_stored_state_and_for_all_ended(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(ended=True)})

        with patch.object(liiga_command, "_fetch_today_games", return_value={1: make_game(ended=False)}):
            result = liiga_command._poll_once(bot, "#chan")

        assert result is True
        assert liiga_command._channels["#chan"]["games"][1]["ended"] is True
        bot.send_message.assert_not_called()

    def test_a_game_that_has_not_ended_is_still_not_ended(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(ended=False), 2: make_game(gid=2, ended=True)})

        games = {1: make_game(ended=False), 2: make_game(gid=2, ended=False)}
        with patch.object(liiga_command, "_fetch_today_games", return_value=games):
            result = liiga_command._poll_once(bot, "#chan")

        assert result is False
        assert liiga_command._channels["#chan"]["games"][1]["ended"] is False
        assert liiga_command._channels["#chan"]["games"][2]["ended"] is True

    def test_fetch_failure_does_not_crash(self, liiga_command):
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game()})

        with patch.object(liiga_command, "_fetch_today_games", return_value=None):
            result = liiga_command._poll_once(bot, "#chan")

        assert result is False
        bot.send_message.assert_not_called()

    def test_untracked_channel_stops_polling(self, liiga_command):
        bot = MagicMock()
        with patch.object(liiga_command, "_fetch_today_games", return_value={1: make_game()}):
            result = liiga_command._poll_once(bot, "#never-started")

        assert result is True

    def test_malformed_game_does_not_stop_other_games(self, liiga_command):
        """One game missing expected fields shouldn't prevent the rest of
        the poll cycle (other games' goals/finals) from being announced."""
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {
            1: make_game(gid=1, home_goals=[]),
            2: make_game(gid=2, home_goals=[]),
        })

        broken_game = {"id": 1}  # missing homeTeam/awayTeam/etc entirely
        healthy_game = make_game(gid=2, home_goals=[goal_event()])

        with patch.object(
            liiga_command, "_fetch_today_games",
            return_value={1: broken_game, 2: healthy_game},
        ):
            result = liiga_command._poll_once(bot, "#chan")

        assert result is False
        messages = [c[0][1] for c in bot.send_message.call_args_list]
        assert any("GOAL" in m for m in messages)

    def test_unexpected_game_processing_failure_logs_a_traceback(self, liiga_command):
        # The catch-all around processing one game is the last line of
        # defense against anything a narrower handler didn't anticipate -
        # it must print a full traceback, not just str(e), since that's
        # often the only way to pinpoint where a genuinely new bug broke.
        bot = MagicMock()
        self._seed(liiga_command, "#chan", {1: make_game(gid=1, home_goals=[])})

        with patch.object(liiga_command, "_fetch_today_games", return_value={1: make_game(gid=1)}), \
             patch.object(liiga_command, "_announce_new_goals", side_effect=RuntimeError("boom")), \
             patch("traceback.print_exc") as mock_print_exc:
            liiga_command._poll_once(bot, "#chan")  # must not raise

        mock_print_exc.assert_called_once()

    def test_send_message_failure_does_not_crash_poll(self, liiga_command):
        bot = MagicMock()
        bot.send_message.side_effect = OSError("socket closed")
        self._seed(liiga_command, "#chan", {1: make_game(home_goals=[])})

        updated = {1: make_game(home_goals=[goal_event()])}
        with patch.object(liiga_command, "_fetch_today_games", return_value=updated):
            result = liiga_command._poll_once(bot, "#chan")  # must not raise

        assert result is False


class TestAnnounceEnd:
    def _game_with_periods(self, finished_type, periods):
        game = make_game(gid=2701280, home="Sport", away="Jokerit",
                          home_goals=[goal_event()] * 5, away_goals=[goal_event()] * 4,
                          ended=True, finished_type=finished_type)
        game["periods"] = periods
        return game

    def test_regulation_final_has_no_suffix(self, liiga_command):
        bot = MagicMock()
        game = self._game_with_periods("ENDED_DURING_REGULAR_GAME_TIME", [
            {"index": 1, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 1},
            {"index": 2, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 1},
            {"index": 3, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 2},
        ])

        liiga_command._announce_end(bot, "#chan", game)

        message = bot.send_message.call_args[0][1]
        assert message == f"{liiga_command.FINAL_PREFIX} Sport 5-4 Jokerit"

    def test_overtime_final_gets_ot_suffix(self, liiga_command):
        # Regression test built from the real payload for Liiga game
        # 2701280 (Sport-Jokerit, 2026-09-01): a decisive overtime period
        # in "periods" pairs with an "ENDED_DURING_..." finishedType that
        # mentions "OVERTIME".
        bot = MagicMock()
        game = self._game_with_periods("ENDED_DURING_OVERTIME", [
            {"index": 1, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 2},
            {"index": 2, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 2},
            {"index": 3, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 0},
            {"index": 4, "category": "OVERTIME", "homeTeamGoals": 1, "awayTeamGoals": 0},
        ])

        liiga_command._announce_end(bot, "#chan", game)

        message = bot.send_message.call_args[0][1]
        assert message == f"{liiga_command.FINAL_PREFIX} Sport 5-4 Jokerit (OT)"

    def test_shootout_final_gets_so_suffix(self, liiga_command):
        # Regression test built from the real payload for Liiga game
        # 2701280 (Sport-Jokerit, 2026-09-01, finishedType
        # "ENDED_DURING_WINNING_SHOT_COMPETITION") - confirmed live this
        # exact game actually went to a shootout.
        bot = MagicMock()
        game = self._game_with_periods("ENDED_DURING_WINNING_SHOT_COMPETITION", [
            {"index": 1, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 2},
            {"index": 2, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 2},
            {"index": 3, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 0},
            {"index": 4, "category": "OVERTIME", "homeTeamGoals": 0, "awayTeamGoals": 0},
            {"index": 5, "category": "WINNING_SHOT_COMPETITION", "homeTeamGoals": 1, "awayTeamGoals": 0},
        ])

        liiga_command._announce_end(bot, "#chan", game)

        message = bot.send_message.call_args[0][1]
        assert message == f"{liiga_command.FINAL_PREFIX} Sport 5-4 Jokerit (SO)"

    def test_finishedtype_and_periods_disagreement_is_logged(self, liiga_command, capsys):
        # A stale/generic finishedType at the exact "ended" transition
        # (a plausible eventual-consistency gap, not reproduced after the
        # fact - see the real incident this is a diagnostic for) must not
        # be silently swallowed: it should still be logged so a recurrence
        # leaves hard evidence instead of another unexplained missing
        # suffix.
        bot = MagicMock()
        game = self._game_with_periods("ACTIVE_OR_NOT_STARTED", [
            {"index": 1, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 2},
            {"index": 2, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 2},
            {"index": 3, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 0},
            {"index": 4, "category": "OVERTIME", "homeTeamGoals": 0, "awayTeamGoals": 0},
            {"index": 5, "category": "WINNING_SHOT_COMPETITION", "homeTeamGoals": 1, "awayTeamGoals": 0},
        ])

        liiga_command._announce_end(bot, "#chan", game)

        message = bot.send_message.call_args[0][1]
        assert message == f"{liiga_command.FINAL_PREFIX} Sport 5-4 Jokerit (SO)"  # periods still wins
        captured = capsys.readouterr()
        assert "end-suffix mismatch" in captured.out
        assert "2701280" in captured.out

    def test_periods_so_wins_over_a_generic_overtime_finishedtype(self, liiga_command, capsys):
        # Regression test for a real gap: finishedType "ENDED_DURING_
        # OVERTIME" is non-empty, so the old "suffix_from_type or
        # suffix_from_periods" logic picked "(OT)" and never even looked
        # at periods - even though a shootout only ever happens after a
        # scoreless overtime, so periods showing WINNING_SHOT_COMPETITION
        # here is strictly the more specific, and more reliable
        # (confirmed live), signal of the two.
        bot = MagicMock()
        game = self._game_with_periods("ENDED_DURING_OVERTIME", [
            {"index": 1, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 2},
            {"index": 2, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 2},
            {"index": 3, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 0},
            {"index": 4, "category": "OVERTIME", "homeTeamGoals": 0, "awayTeamGoals": 0},
            {"index": 5, "category": "WINNING_SHOT_COMPETITION", "homeTeamGoals": 1, "awayTeamGoals": 0},
        ])

        liiga_command._announce_end(bot, "#chan", game)

        message = bot.send_message.call_args[0][1]
        assert message == f"{liiga_command.FINAL_PREFIX} Sport 5-4 Jokerit (SO)"
        assert "end-suffix mismatch" in capsys.readouterr().out  # still logged, still diverges

    def test_scoreless_overtime_period_entry_is_not_mistaken_for_a_played_one(self, liiga_command):
        # A period category can apparently appear in the list even when
        # nothing happened in it (e.g. scheduling metadata) - only count
        # it if it actually has goals recorded.
        bot = MagicMock()
        game = self._game_with_periods("ENDED_DURING_REGULAR_GAME_TIME", [
            {"index": 1, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 1},
            {"index": 2, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 1},
            {"index": 3, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 2},
            {"index": 4, "category": "OVERTIME", "homeTeamGoals": 0, "awayTeamGoals": 0},
        ])

        liiga_command._announce_end(bot, "#chan", game)

        message = bot.send_message.call_args[0][1]
        assert message == f"{liiga_command.FINAL_PREFIX} Sport 5-4 Jokerit"

    def test_attendance_is_appended_when_present(self, liiga_command):
        # Regression test built from the real payload for Liiga game
        # 2701291 (HPK-Ilves, 2026-09-08): "spectators" matches liiga.fi's
        # own "Yleisöä: N" figure exactly.
        bot = MagicMock()
        game = self._game_with_periods("ENDED_DURING_REGULAR_GAME_TIME", [
            {"index": 1, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 1},
            {"index": 2, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 1},
            {"index": 3, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 2},
        ])
        game["spectators"] = 3532

        liiga_command._announce_end(bot, "#chan", game)

        message = bot.send_message.call_args[0][1]
        assert message == f"{liiga_command.FINAL_PREFIX} Sport 5-4 Jokerit | Yleisöä: 3532"

    def test_attendance_is_omitted_when_absent(self, liiga_command):
        # Some preseason/training games don't carry an attendance figure
        # at all - must not print a misleading "Yleisöä: None"/"0".
        bot = MagicMock()
        game = self._game_with_periods("ENDED_DURING_REGULAR_GAME_TIME", [
            {"index": 1, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 1},
            {"index": 2, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 1},
            {"index": 3, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 2},
        ])

        liiga_command._announce_end(bot, "#chan", game)

        message = bot.send_message.call_args[0][1]
        assert "Yleisöä" not in message

    def test_attendance_follows_the_ot_so_suffix(self, liiga_command):
        bot = MagicMock()
        game = self._game_with_periods("ENDED_DURING_OVERTIME", [
            {"index": 1, "category": "NORMAL", "homeTeamGoals": 2, "awayTeamGoals": 2},
            {"index": 2, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 2},
            {"index": 3, "category": "NORMAL", "homeTeamGoals": 1, "awayTeamGoals": 0},
            {"index": 4, "category": "OVERTIME", "homeTeamGoals": 1, "awayTeamGoals": 0},
        ])
        game["spectators"] = 4200

        liiga_command._announce_end(bot, "#chan", game)

        message = bot.send_message.call_args[0][1]
        assert message == f"{liiga_command.FINAL_PREFIX} Sport 5-4 Jokerit (OT) | Yleisöä: 4200"


class TestFetchTodayGames:
    def _make_response(self, status_ok=True, payload=None):
        resp = MagicMock()
        if status_ok:
            resp.raise_for_status.return_value = None
        else:
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError("boom")
        resp.json.return_value = payload if payload is not None else {"games": []}
        return resp

    def test_all_tournaments_failing_returns_none(self, liiga_command):
        with patch.object(liiga_command.session, "get", side_effect=requests.exceptions.Timeout("slow")):
            result = liiga_command._fetch_today_games()
        assert result is None

    def test_one_tournament_failing_keeps_the_others(self, liiga_command):
        good_game = make_game(gid=42)

        def fake_get(url, params=None, timeout=None):
            if params["tournament"] == "runkosarja":
                return self._make_response(payload={"games": [good_game]})
            raise requests.exceptions.Timeout("slow tournament")

        with patch.object(liiga_command.session, "get", side_effect=fake_get):
            result = liiga_command._fetch_today_games()

        assert result == {42: good_game}

    def test_malformed_json_shape_for_one_tournament_is_skipped(self, liiga_command):
        good_game = make_game(gid=7)

        def fake_get(url, params=None, timeout=None):
            if params["tournament"] == "runkosarja":
                return self._make_response(payload={"games": [good_game]})
            return self._make_response(payload=["not", "a", "dict"])

        with patch.object(liiga_command.session, "get", side_effect=fake_get):
            result = liiga_command._fetch_today_games()

        assert result == {7: good_game}

    def test_invalid_json_is_logged_as_unexpected_data_not_a_request_failure(self, liiga_command, capsys):
        # Regression test: requests.exceptions.JSONDecodeError (raised by
        # resp.json() on a non-JSON body) is also a RequestException, so
        # without checking ValueError first this used to be logged (and
        # treated) as a request failure even though the server did
        # respond.
        resp = self._make_response()
        resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "not json", 0)
        with patch.object(liiga_command.session, "get", return_value=resp):
            result = liiga_command._fetch_today_games()

        assert result is None  # every tournament hit the same invalid-JSON response
        captured = capsys.readouterr()
        assert "returned unexpected data" in captured.out
        assert "request failed" not in captured.out


class TestNext:
    def test_next_returns_immediately_without_blocking(self, liiga_command):
        release_fetch = threading.Event()

        def slow_next():
            release_fetch.wait(timeout=2)
            return "2026-08-25", {1: make_game()}

        bot = MagicMock()
        with patch.object(liiga_command, "_fetch_next_gameday", side_effect=slow_next):
            start = time.time()
            result = liiga_command.execute("next", irc_bot=bot, channel="#chan")
            elapsed = time.time() - start

            assert elapsed < 1, "execute() blocked on the network fetch"
            assert "Checking" in result

            release_fetch.set()
            time.sleep(0.2)  # let the one-shot background thread finish

        bot.send_message.assert_called_once()

    # "next" without context, and _run_next's not-found/unreachable/
    # exception handling, are LiveTrackerCommand's own base behavior
    # (Liiga's _fetch_next_period is a pure pass-through to
    # _fetch_next_gameday(), with no logic of its own) - covered there.
    # This class only needs Liiga's own summary format.
    def test_run_next_reports_games_and_date_label(self, liiga_command):
        bot = MagicMock()
        with patch.object(
            liiga_command, "_fetch_next_gameday",
            return_value=("2026-08-25", {1: make_game(home="TPS", away="Jokerit", started=False)}),
        ), patch.object(liiga_command, "_format_date_label", return_value="tomorrow"):
            liiga_command._run_next(bot, "#chan")

        message = bot.send_message.call_args[0][1]
        assert "Next Liiga gameday (tomorrow)" in message
        assert "TPS-Jokerit" in message


class TestFetchNextGameday:
    def _make_response(self, games=None, next_game_date=None, ok=True):
        resp = MagicMock()
        resp.raise_for_status.return_value = None if ok else None
        if not ok:
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError("boom")
        resp.json.return_value = {"games": games or [], "nextGameDate": next_game_date}
        return resp

    def test_returns_today_if_games_already_scheduled_today(self, liiga_command):
        today_game = make_game(gid=1)
        today_str = datetime.datetime.now(liiga_command.HELSINKI_TZ).strftime("%Y-%m-%d")

        def fake_get(url, params=None, timeout=None):
            if params["tournament"] == "runkosarja":
                return self._make_response(games=[today_game])
            return self._make_response()

        with patch.object(liiga_command.session, "get", side_effect=fake_get):
            date_str, games = liiga_command._fetch_next_gameday()

        assert games == {1: today_game}
        assert date_str == today_str

    def test_ignores_a_backward_pointing_next_game_date(self, liiga_command):
        # Regression test for a real incident: once valmistavat_ottelut
        # (preseason)'s own schedule was exhausted, liiga.fi's API didn't
        # return a null nextGameDate for it - it wrapped back to that
        # tournament's very first date, weeks in the past relative to
        # what was actually queried. Confirmed live: runkosarja's real
        # "2026-09-01" got beaten by valmistavat_ottelut's stale
        # "2026-08-07" under plain min() - the stale one must be
        # discarded instead of competing with a real one.
        now = datetime.datetime.now(liiga_command.HELSINKI_TZ)
        today_str = now.strftime("%Y-%m-%d")
        real_next_date = (now + datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        stale_wraparound_date = (now - datetime.timedelta(days=21)).strftime("%Y-%m-%d")

        def fake_get(url, params=None, timeout=None):
            if params["tournament"] == "runkosarja":
                return self._make_response(next_game_date=real_next_date)
            if params["tournament"] == "valmistavat_ottelut":
                return self._make_response(next_game_date=stale_wraparound_date)
            return self._make_response()

        with patch.object(liiga_command.session, "get", side_effect=fake_get):
            games, next_date = liiga_command._fetch_games_and_next_date(today_str, 2027)

        assert next_date == real_next_date

    def test_falls_forward_to_earliest_next_game_date_across_tournaments(self, liiga_command):
        # Relative to "now" (not hardcoded absolute dates) so this doesn't
        # bit-rot into a false failure as real time passes.
        now = datetime.datetime.now(liiga_command.HELSINKI_TZ)
        nearer_date = (now + datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        farther_date = (now + datetime.timedelta(days=8)).strftime("%Y-%m-%d")
        future_game = make_game(gid=99)
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append((params["tournament"], params["date"]))
            # First round (today): no games, differing nextGameDate per tournament.
            if len(calls) <= len(liiga_command.TOURNAMENTS):
                next_dates = {
                    "runkosarja": farther_date,
                    "playoffs": None,
                    "playout": None,
                    "qualifications": None,
                    "valmistavat_ottelut": nearer_date,
                }
                return self._make_response(next_game_date=next_dates[params["tournament"]])
            # Second round (the earliest next date): return a game.
            if params["date"] == nearer_date and params["tournament"] == "valmistavat_ottelut":
                return self._make_response(games=[future_game])
            return self._make_response()

        with patch.object(liiga_command.session, "get", side_effect=fake_get):
            date_str, games = liiga_command._fetch_next_gameday()

        assert date_str == nearer_date
        assert games == {99: future_game}

    def test_all_requests_failing_returns_none(self, liiga_command):
        with patch.object(liiga_command.session, "get", side_effect=requests.exceptions.Timeout("slow")):
            date_str, games = liiga_command._fetch_next_gameday()
        assert date_str is None
        assert games is None

    def test_no_next_date_reported_anywhere_returns_none(self, liiga_command):
        with patch.object(liiga_command.session, "get", side_effect=lambda *a, **k: self._make_response()):
            date_str, games = liiga_command._fetch_next_gameday()
        assert date_str is None
        assert games is None

    def test_skips_today_when_all_of_todays_games_already_ended(self, liiga_command):
        # Regression test for a real report: checking !liiga next hours
        # after today's games finished repeated today's stale result
        # instead of finding the actual next gameday. The API gives no
        # nextGameDate hint when today's query *did* have games (ended or
        # not), so this must fall back to the day-by-day search.
        finished_today = make_game(gid=1, ended=True, finished_type="ENDED_DURING_REGULAR_GAME_TIME")
        tomorrow_game = make_game(gid=2, ended=False)
        today_str = datetime.datetime.now(liiga_command.HELSINKI_TZ).strftime("%Y-%m-%d")
        tomorrow_str = (datetime.datetime.now(liiga_command.HELSINKI_TZ) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        def fake_get(url, params=None, timeout=None):
            if params["date"] == today_str and params["tournament"] == "runkosarja":
                return self._make_response(games=[finished_today])
            if params["date"] == tomorrow_str and params["tournament"] == "runkosarja":
                return self._make_response(games=[tomorrow_game])
            return self._make_response()

        with patch.object(liiga_command.session, "get", side_effect=fake_get):
            date_str, games = liiga_command._fetch_next_gameday()

        assert date_str == tomorrow_str
        assert games == {2: tomorrow_game}

    def test_skips_today_when_the_only_game_left_open_was_postponed(self, liiga_command):
        """Issue #37: a postponed game stays not started in the feed; it must not make `next` offer today."""
        finished_today = make_game(gid=1, ended=True, start=at(10), end=at(13))
        postponed = make_game(gid=3, started=False, start=at(10), end=at(13))
        tomorrow_game = make_game(gid=2, started=False, start=at(14, day=6), end=at(17, day=6))
        today_str = datetime.datetime.now(liiga_command.HELSINKI_TZ).strftime("%Y-%m-%d")
        tomorrow_str = (datetime.datetime.now(liiga_command.HELSINKI_TZ) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        def fake_get(url, params=None, timeout=None):
            if params["date"] == today_str and params["tournament"] == "runkosarja":
                return self._make_response(games=[finished_today, postponed])
            if params["date"] == tomorrow_str and params["tournament"] == "runkosarja":
                return self._make_response(games=[tomorrow_game])
            return self._make_response()

        with patch.object(liiga_command.session, "get", side_effect=fake_get):
            date_str, games = liiga_command._fetch_next_gameday()

        assert date_str == tomorrow_str and games == {2: tomorrow_game}

    def test_returns_today_if_only_some_of_todays_games_have_ended(self, liiga_command):
        finished = make_game(gid=1, ended=True)
        still_live = make_game(gid=2, ended=False)

        def fake_get(url, params=None, timeout=None):
            if params["tournament"] == "runkosarja":
                return self._make_response(games=[finished, still_live])
            return self._make_response()

        with patch.object(liiga_command.session, "get", side_effect=fake_get):
            date_str, games = liiga_command._fetch_next_gameday()

        assert games == {1: finished, 2: still_live}

    def test_malformed_next_game_date_hint_falls_back_to_day_by_day_search(self, liiga_command):
        # Regression coverage: an unparseable nextGameDate hint must not
        # crash the lookup - it should just be discarded in favor of the
        # bounded day-by-day fallback search.
        tomorrow_game = make_game(gid=5)
        tomorrow_str = (
            datetime.datetime.now(liiga_command.HELSINKI_TZ) + datetime.timedelta(days=1)
        ).strftime("%Y-%m-%d")

        def fake_get(url, params=None, timeout=None):
            if params["tournament"] == "runkosarja" and params["date"] != tomorrow_str:
                return self._make_response(next_game_date="not-a-real-date")
            if params["date"] == tomorrow_str and params["tournament"] == "runkosarja":
                return self._make_response(games=[tomorrow_game])
            return self._make_response()

        with patch.object(liiga_command.session, "get", side_effect=fake_get):
            date_str, games = liiga_command._fetch_next_gameday()

        assert date_str == tomorrow_str
        assert games == {5: tomorrow_game}


class TestAllGamesEnded:
    def test_true_when_every_game_ended(self, liiga_command):
        assert liiga_command._all_games_ended({1: make_game(gid=1, ended=True)}) is True

    def test_false_when_any_game_not_ended(self, liiga_command):
        games = {1: make_game(gid=1, ended=True), 2: make_game(gid=2, ended=False)}
        assert liiga_command._all_games_ended(games) is False

    def test_false_for_empty_dict(self, liiga_command):
        assert liiga_command._all_games_ended({}) is False


class TestFormatClock:
    def test_computes_elapsed_time_into_the_period(self, liiga_command):
        game = make_game()  # periods: index 1 starts at 0, index 2 at 1200, index 3 at 2400
        event = goal_event(period=2, game_time=1325)  # 125s into period 2
        assert liiga_command._format_clock(game, event) == "02:05"

    def test_missing_game_time_returns_empty_string(self, liiga_command):
        game = make_game()
        event = goal_event(period=1)
        del event["gameTime"]
        assert liiga_command._format_clock(game, event) == ""

    def test_missing_period_returns_empty_string(self, liiga_command):
        game = make_game()
        event = goal_event(game_time=125)
        del event["period"]
        assert liiga_command._format_clock(game, event) == ""

    def test_unmatched_period_defaults_start_to_zero(self, liiga_command):
        game = make_game()
        event = goal_event(period=99, game_time=45)  # no periods entry has index 99
        assert liiga_command._format_clock(game, event) == "00:45"


class TestGamesSummary:
    def test_single_game_shows_its_start_time(self, liiga_command):
        games = [make_game(start="2026-09-05T14:00:00Z")]  # 17:00 Helsinki (EEST, UTC+3)
        assert liiga_command._format_games_summary(games) == "17:00 HIFK-Ilves"

    def test_same_start_time_is_grouped(self, liiga_command):
        games = [
            make_game(gid=1, home="HIFK", away="Ilves", start="2026-09-05T14:00:00Z"),
            make_game(gid=2, home="Tappara", away="Kärpät", start="2026-09-05T14:00:00Z"),
        ]
        assert liiga_command._format_games_summary(games) == "17:00 HIFK-Ilves, Tappara-Kärpät"

    def test_different_start_times_are_separate_groups_in_order(self, liiga_command):
        games = [
            make_game(gid=1, home="JYP", away="Lukko", start="2026-09-05T15:30:00Z"),
            make_game(gid=2, home="HIFK", away="Ilves", start="2026-09-05T14:00:00Z"),
        ]
        assert liiga_command._format_games_summary(games) == "17:00 HIFK-Ilves | 18:30 JYP-Lukko"

    def test_missing_start_time_falls_back_and_sorts_last(self, liiga_command):
        games = [
            make_game(gid=1, home="JYP", away="Lukko", start=None),
            make_game(gid=2, home="HIFK", away="Ilves", start="2026-09-05T14:00:00Z"),
        ]
        assert liiga_command._format_games_summary(games) == "17:00 HIFK-Ilves | ??:?? JYP-Lukko"

    def test_start_time_is_converted_to_helsinki_local(self, liiga_command):
        # 2026-01-05 is outside DST (EET, UTC+2): 14:00 UTC -> 16:00 local.
        games = [make_game(start="2026-01-05T14:00:00Z")]
        assert liiga_command._format_games_summary(games) == "16:00 HIFK-Ilves"


class TestSeasonCalculation:
    def test_autumn_date_uses_next_year(self, liiga_command):
        dt = datetime.datetime(2024, 9, 10, tzinfo=liiga_command.HELSINKI_TZ)
        assert liiga_command._current_season(dt) == 2025

    def test_spring_date_uses_same_year(self, liiga_command):
        dt = datetime.datetime(2025, 3, 15, tzinfo=liiga_command.HELSINKI_TZ)
        assert liiga_command._current_season(dt) == 2025


class TestTrackingSummary:
    """The "Tracking N games today: ..." list adds scores for games already
    underway (goals scored before !liiga start are deliberately never
    announced, so without this a mid-game start leaves the channel
    guessing); "!liiga next" shows the same scores for a day that is partly played."""

    def test_game_not_started_shows_no_score(self, liiga_command):
        games = [make_game(started=False)]
        assert liiga_command._format_tracking_summary(games) == "17:00 HIFK-Ilves"

    def test_live_game_shows_its_score(self, liiga_command):
        game = make_game(home_goals=[goal_event(), goal_event()], away_goals=[goal_event()])
        assert liiga_command._format_tracking_summary([game]) == "17:00 HIFK 2-1 Ilves"

    def test_finished_game_is_marked_final(self, liiga_command):
        game = make_game(home_goals=[goal_event()], ended=True)
        assert liiga_command._format_tracking_summary([game]) == "17:00 HIFK 1-0 Ilves (final)"

    def test_mixed_slate_keeps_time_grouping(self, liiga_command):
        games = [
            make_game(gid=1, home="HIFK", away="Ilves", home_goals=[goal_event()], start="2026-09-05T14:00:00Z"),
            make_game(gid=2, home="JYP", away="Lukko", started=False, start="2026-09-05T15:30:00Z"),
        ]
        assert liiga_command._format_tracking_summary(games) == "17:00 HIFK 1-0 Ilves | 18:30 JYP-Lukko"

    def test_non_numeric_goal_counts_show_no_score_rather_than_a_wrong_one(self, liiga_command):
        game = make_game()
        del game["homeTeam"]["goals"]  # _team_goals() falls back to "?"
        assert liiga_command._format_tracking_summary([game]) == "17:00 HIFK-Ilves"

    def test_next_shows_the_score_of_a_started_game(self, liiga_command):
        game = make_game(home_goals=[goal_event()])
        assert liiga_command._format_period_summary([game]) == "17:00 HIFK 1-0 Ilves"

    def test_next_on_a_day_nothing_has_started_on_is_the_plain_list(self, liiga_command):
        assert liiga_command._format_period_summary([make_game(started=False)]) == "17:00 HIFK-Ilves"

    def test_next_on_a_partly_played_day_marks_the_final(self, liiga_command):
        games = [
            make_game(gid=1, home="HIFK", away="Ilves", home_goals=[goal_event()], ended=True,
                      start="2026-09-05T14:00:00Z"),
            make_game(gid=2, home="JYP", away="Lukko", started=False, start="2026-09-05T15:30:00Z"),
        ]
        assert liiga_command._format_period_summary(games) == "17:00 HIFK 1-0 Ilves (final) | 18:30 JYP-Lukko"
