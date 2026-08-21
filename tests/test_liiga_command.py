import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from liiga_command import LiigaCommand


def make_game(gid=1, home="HIFK", away="Ilves", home_goals=None, away_goals=None,
              started=True, ended=False, finished_type="ACTIVE_OR_NOT_STARTED"):
    return {
        "id": gid,
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


def goal_event(period=1, game_time=125, home_score=1, away_score=0,
                first="Kristian", last="Vesalainen", assists=None, tags=None):
    return {
        "period": period,
        "gameTime": game_time,
        "homeTeamScore": home_score,
        "awayTeamScore": away_score,
        "scorerPlayer": {"firstName": first, "lastName": last},
        "assistantPlayers": assists or [],
        "goalTypes": tags or [],
    }


def join_channel_thread(liiga_command, channel, timeout=2):
    """Wait for the background thread started for `channel` to finish."""
    entry = liiga_command._channels.get(channel)
    if entry and entry.get("thread"):
        entry["thread"].join(timeout=timeout)


@pytest.fixture
def liiga_command():
    return LiigaCommand()


class TestUsage:
    def test_no_args_shows_usage(self, liiga_command):
        result = liiga_command.execute("", irc_bot=MagicMock(), channel="#chan")
        assert "Usage:" in result

    def test_unknown_arg_shows_usage(self, liiga_command):
        result = liiga_command.execute("bogus", irc_bot=MagicMock(), channel="#chan")
        assert "Usage:" in result

    def test_start_without_context_errors(self, liiga_command):
        result = liiga_command.execute("start")
        assert "Error" in result


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
        assert "Tracking 1 Liiga game" in bot.send_message.call_args[0][1]

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
    def test_stop_without_active_tracking(self, liiga_command):
        result = liiga_command.execute("stop", irc_bot=MagicMock(), channel="#chan")
        assert "Not currently tracking" in result

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
        assert "HIFK 1-0 Ilves" in message
        assert "02:05" in message  # 125s -> 2:05 into period 1

    def test_goal_and_final_use_mirc_colors(self, liiga_command):
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

        assert goal_msg.startswith(liiga_command.GOAL_PREFIX)
        assert liiga_command.GREEN in goal_msg
        assert final_msg.startswith(liiga_command.FINAL_PREFIX)
        assert liiga_command.ORANGE in final_msg
        # Both prefixes must reset formatting so the rest of the line isn't
        # left bold/colored on the user's client.
        assert liiga_command.COLOR_RESET in goal_msg
        assert liiga_command.COLOR_RESET in final_msg

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

    def test_send_message_failure_does_not_crash_poll(self, liiga_command):
        bot = MagicMock()
        bot.send_message.side_effect = OSError("socket closed")
        self._seed(liiga_command, "#chan", {1: make_game(home_goals=[])})

        updated = {1: make_game(home_goals=[goal_event()])}
        with patch.object(liiga_command, "_fetch_today_games", return_value=updated):
            result = liiga_command._poll_once(bot, "#chan")  # must not raise

        assert result is False


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


class TestSeasonCalculation:
    def test_autumn_date_uses_next_year(self, liiga_command):
        import datetime
        dt = liiga_command.HELSINKI_TZ.localize(datetime.datetime(2024, 9, 10))
        assert liiga_command._current_season(dt) == 2025

    def test_spring_date_uses_same_year(self, liiga_command):
        import datetime
        dt = liiga_command.HELSINKI_TZ.localize(datetime.datetime(2025, 3, 15))
        assert liiga_command._current_season(dt) == 2025
