from unittest.mock import MagicMock

import pytest

# Bare import to match command_handler.py's own sibling-import style (see
# tests/conftest.py for the sys.path setup that makes this resolve).
from aijamatto import AijaMattoCommand
from command_handler import CommandHandler


@pytest.fixture
def handler(monkeypatch, tmp_path):
    # No command constructor raises or makes a network call on a missing
    # API key (see base_command.py's contract) - setting this isn't
    # strictly required, but keeps WeatherCommand's own tests' env
    # expectations consistent with a real CommandHandler being built here.
    monkeypatch.setenv("WEATHER_API_KEY", "test-key")
    # AijaMattoCommand.__init__ reads its real ~36MB aijamatto.txt in
    # full - harmless for the bot itself (once, at startup) but real,
    # unnecessary I/O on every single test in this file, none of which
    # exercise !bjorck itself. Point it at a tiny stand-in instead.
    lines_file = tmp_path / "aijamatto.txt"
    lines_file.write_text("placeholder line\n")
    monkeypatch.setattr(AijaMattoCommand, "LINES_FILE", str(lines_file))
    return CommandHandler()


def replace_command(handler, alias, mock_command):
    """Swap the real command object registered for `alias` with a mock,
    carrying over its ALLOW_ARGS/CHANNELS/needs_irc_context so
    CommandHandler enforces the same restrictions and dispatch shape
    against the mock - and swap every OTHER alias pointing at that same
    real instance too (e.g. !weather and !w both need to route to the
    same mock)."""
    real_command = handler.commands_by_alias.get(alias)
    if real_command is None:
        raise AssertionError(f"No command registered for alias {alias}")

    mock_command.ALLOW_ARGS = real_command.ALLOW_ARGS
    mock_command.CHANNELS = real_command.CHANNELS
    mock_command.needs_irc_context = real_command.needs_irc_context
    for other_alias, command in list(handler.commands_by_alias.items()):
        if command is real_command:
            handler.commands_by_alias[other_alias] = mock_command


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

    def test_needs_irc_context_command_receives_bot_and_channel(self, handler):
        # LiigaCommand/PesisCommand's own dispatch shape - the only
        # branch in handle_command() that passes irc_bot/channel through
        # to execute() at all.
        bot = MagicMock()
        mock_liiga = MagicMock()
        mock_liiga.execute.return_value = "Checking today's Liiga games..."
        replace_command(handler, "!liiga", mock_liiga)

        handler.handle_command(bot, "nick", "#smliiga", "!liiga start")

        mock_liiga.execute.assert_called_once_with("start", irc_bot=bot, channel="#smliiga")


class TestChannelRestriction:
    """!superpesis and !ykkospesis are registered restricted to #pesis.fi
    and !liiga to #smliiga; every other command has no "channels" key at
    all, so this is also the coverage for "absent = allowed everywhere"
    not regressing."""

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

    def test_ykkospesis_is_silently_ignored_outside_pesis_fi(self, handler):
        bot = MagicMock()
        mock_ykkospesis = MagicMock()
        replace_command(handler, "!ykkospesis", mock_ykkospesis)

        handler.handle_command(bot, "nick", "#smliiga", "!ykkospesis start")

        mock_ykkospesis.execute.assert_not_called()
        bot.send_message.assert_not_called()

    def test_ykkospesis_works_in_pesis_fi_alongside_superpesis(self, handler):
        # Explicitly the behavior asked for: both leagues share the same
        # channel restriction, and neither command shadows the other.
        bot = MagicMock()
        mock_ykkospesis = MagicMock()
        mock_ykkospesis.execute.return_value = "ok"
        replace_command(handler, "!ykkospesis", mock_ykkospesis)

        handler.handle_command(bot, "nick", "#pesis.fi", "!ykkospesis start")

        mock_ykkospesis.execute.assert_called_once()
        bot.send_message.assert_called_once_with("#pesis.fi", "ok")

    def test_liiga_is_silently_ignored_outside_smliiga(self, handler):
        bot = MagicMock()
        mock_liiga = MagicMock()
        replace_command(handler, "!liiga", mock_liiga)

        handler.handle_command(bot, "nick", "#pesis.fi", "!liiga start")

        mock_liiga.execute.assert_not_called()
        bot.send_message.assert_not_called()

    def test_liiga_works_in_smliiga(self, handler):
        bot = MagicMock()
        mock_liiga = MagicMock()
        mock_liiga.execute.return_value = "ok"
        replace_command(handler, "!liiga", mock_liiga)

        handler.handle_command(bot, "nick", "#smliiga", "!liiga start")

        mock_liiga.execute.assert_called_once()
        bot.send_message.assert_called_once_with("#smliiga", "ok")

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
        # Every command besides !superpesis/!liiga has no "channels" key
        # at all - confirm that absence never restricts anything
        # (regression guard for the mechanism itself, not any one
        # command).
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.return_value = "sunny"
        replace_command(handler, "!weather", mock_weather)

        handler.handle_command(bot, "nick", "#some-random-channel", "!weather kokkola")

        mock_weather.execute.assert_called_once_with("kokkola")
        bot.send_message.assert_called_once_with("#some-random-channel", "sunny")
