from unittest.mock import MagicMock, patch

import pytest

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


class TestStartStop:
    def test_start_with_no_games_today(self, liiga_command):
        with patch.object(liiga_command, "_fetch_today_games", return_value={}):
            result = liiga_command.execute("start", irc_bot=MagicMock(), channel="#chan")
        assert "No Liiga games" in result

    def test_start_api_failure(self, liiga_command):
        with patch.object(liiga_command, "_fetch_today_games", return_value=None):
            result = liiga_command.execute("start", irc_bot=MagicMock(), channel="#chan")
        assert "Error" in result

    def test_start_spawns_thread_and_reports_games(self, liiga_command):
        games = {1: make_game()}
        with patch.object(liiga_command, "_fetch_today_games", return_value=games):
            result = liiga_command.execute("start", irc_bot=MagicMock(), channel="#chan")
        assert "Tracking 1 Liiga game" in result
        assert "HIFK-Ilves" in result
        assert "#chan" in liiga_command._channels
        liiga_command._channels["#chan"]["stop_event"].set()  # stop the real thread

    def test_start_twice_is_rejected(self, liiga_command):
        games = {1: make_game()}
        with patch.object(liiga_command, "_fetch_today_games", return_value=games):
            liiga_command.execute("start", irc_bot=MagicMock(), channel="#chan")
            result = liiga_command.execute("start", irc_bot=MagicMock(), channel="#chan")
        assert "Already tracking" in result
        liiga_command._channels["#chan"]["stop_event"].set()

    def test_stop_without_active_tracking(self, liiga_command):
        result = liiga_command.execute("stop", irc_bot=MagicMock(), channel="#chan")
        assert "Not currently tracking" in result

    def test_stop_signals_thread(self, liiga_command):
        games = {1: make_game()}
        with patch.object(liiga_command, "_fetch_today_games", return_value=games):
            liiga_command.execute("start", irc_bot=MagicMock(), channel="#chan")
        stop_event = liiga_command._channels["#chan"]["stop_event"]

        result = liiga_command.execute("stop", irc_bot=MagicMock(), channel="#chan")

        assert "Stopped" in result
        assert stop_event.is_set()
        assert "#chan" not in liiga_command._channels


class TestPollOnce:
    def _seed(self, liiga_command, channel, games):
        liiga_command._channels[channel] = {
            "stop_event": MagicMock(),
            "thread": MagicMock(),
            "games": {gid: liiga_command._snapshot(g) for gid, g in games.items()},
        }

    def test_new_goal_is_announced(self, liiga_command):
        bot = MagicMock()
        initial = {1: make_game(home_goals=[])}
        self._seed(liiga_command, "#chan", initial)

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
        assert any(m.startswith("FINAL:") for m in messages)
        final_msg = next(m for m in messages if m.startswith("FINAL:"))
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


class TestSeasonCalculation:
    def test_autumn_date_uses_next_year(self, liiga_command):
        import datetime
        dt = liiga_command.HELSINKI_TZ.localize(datetime.datetime(2024, 9, 10))
        assert liiga_command._current_season(dt) == 2025

    def test_spring_date_uses_same_year(self, liiga_command):
        import datetime
        dt = liiga_command.HELSINKI_TZ.localize(datetime.datetime(2025, 3, 15))
        assert liiga_command._current_season(dt) == 2025
