from unittest.mock import MagicMock

import pytest

# Bare import to match command_handler.py's own sibling-import style (see
# tests/conftest.py for the sys.path setup that makes this resolve).
from command_handler import CommandHandler


@pytest.fixture
def handler(monkeypatch):
    # WeatherCommand.__init__ raises if WEATHER_API_KEY is unset; the other
    # command constructors don't make network calls, so a real
    # CommandHandler can be built safely as long as this is set.
    monkeypatch.setenv("WEATHER_API_KEY", "test-key")
    return CommandHandler()


def replace_command(handler, alias, mock_command):
    """Swap the real command object registered for `alias` with a mock,
    keeping its original allow_args setting."""
    for cmd_obj, cmd_data in list(handler.command_aliases.items()):
        if alias in cmd_data["aliases"]:
            cmd_data_copy = dict(cmd_data)
            del handler.command_aliases[cmd_obj]
            handler.command_aliases[mock_command] = cmd_data_copy
            return
    raise AssertionError(f"No command registered for alias {alias}")


class TestCommandHandler:
    def test_known_command_dispatches_to_handler(self, handler):
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.return_value = "sunny"
        replace_command(handler, "!weather", mock_weather)

        handler.handle_command(bot, "nick", "#chan", "!weather austin")

        mock_weather.execute.assert_called_once_with("austin")
        bot.send_message.assert_called_once_with("#chan", "sunny")

    def test_alias_routes_to_same_handler(self, handler):
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.return_value = "sunny"
        replace_command(handler, "!weather", mock_weather)

        handler.handle_command(bot, "nick", "#chan", "!w austin")

        mock_weather.execute.assert_called_once_with("austin")

    def test_command_is_case_insensitive(self, handler):
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.return_value = "sunny"
        replace_command(handler, "!weather", mock_weather)

        handler.handle_command(bot, "nick", "#chan", "!WEATHER austin")

        mock_weather.execute.assert_called_once_with("austin")

    def test_no_args_command_gets_empty_string(self, handler):
        bot = MagicMock()
        mock_f1 = MagicMock()
        mock_f1.execute.return_value = "race info"
        replace_command(handler, "!f1", mock_f1)

        handler.handle_command(bot, "nick", "#chan", "!f1")

        mock_f1.execute.assert_called_once_with("")

    def test_args_rejected_for_no_args_command(self, handler):
        bot = MagicMock()
        mock_f1 = MagicMock()
        replace_command(handler, "!f1", mock_f1)

        handler.handle_command(bot, "nick", "#chan", "!f1 extra stuff")

        mock_f1.execute.assert_not_called()
        bot.send_message.assert_not_called()

    def test_unknown_command_does_nothing(self, handler):
        bot = MagicMock()
        handler.handle_command(bot, "nick", "#chan", "!nonsense")
        bot.send_message.assert_not_called()

    def test_empty_response_does_not_send_message(self, handler):
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.return_value = ""
        replace_command(handler, "!weather", mock_weather)

        handler.handle_command(bot, "nick", "#chan", "!weather austin")

        bot.send_message.assert_not_called()

    def test_exception_in_command_does_not_propagate(self, handler):
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.side_effect = RuntimeError("boom")
        replace_command(handler, "!weather", mock_weather)

        # Should not raise.
        handler.handle_command(bot, "nick", "#chan", "!weather austin")

        bot.send_message.assert_not_called()


class TestChannelRestriction:
    """!superpesis is registered restricted to #pesis.fi; every other
    command has no "channels" key at all, so this is also the coverage
    for "absent = allowed everywhere" not regressing."""

    def test_restricted_command_is_silently_ignored_in_other_channels(self, handler):
        bot = MagicMock()
        mock_superpesis = MagicMock()
        replace_command(handler, "!superpesis", mock_superpesis)

        handler.handle_command(bot, "nick", "#smliiga", "!superpesis start")

        mock_superpesis.execute.assert_not_called()
        bot.send_message.assert_not_called()

    def test_restricted_command_works_in_its_designated_channel(self, handler):
        bot = MagicMock()
        mock_superpesis = MagicMock()
        mock_superpesis.execute.return_value = "ok"
        replace_command(handler, "!superpesis", mock_superpesis)

        handler.handle_command(bot, "nick", "#pesis.fi", "!superpesis start")

        mock_superpesis.execute.assert_called_once()
        bot.send_message.assert_called_once_with("#pesis.fi", "ok")

    def test_unrestricted_command_still_works_in_the_restricted_channel(self, handler):
        # Explicitly the behavior asked for: #pesis.fi isn't made
        # exclusive to !superpesis - other commands keep working there.
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.return_value = "sunny"
        replace_command(handler, "!weather", mock_weather)

        handler.handle_command(bot, "nick", "#pesis.fi", "!weather kokkola")

        mock_weather.execute.assert_called_once_with("kokkola")
        bot.send_message.assert_called_once_with("#pesis.fi", "sunny")

    def test_command_without_channels_key_is_allowed_everywhere(self, handler):
        # Every command besides !superpesis has no "channels" key at all -
        # confirm that absence never restricts anything (regression guard
        # for the mechanism itself, not any one command).
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.return_value = "sunny"
        replace_command(handler, "!weather", mock_weather)

        handler.handle_command(bot, "nick", "#some-random-channel", "!weather kokkola")

        mock_weather.execute.assert_called_once_with("kokkola")
        bot.send_message.assert_called_once_with("#some-random-channel", "sunny")
