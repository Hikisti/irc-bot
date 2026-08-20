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
