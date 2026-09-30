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

    def test_args_on_a_no_args_command_get_a_usage_reply_not_silence(self, handler, capsys):
        # Used to be dropped silently, which made "!f1 2026" look like a
        # broken bot.
        bot = MagicMock()
        mock_f1 = MagicMock()
        replace_command(handler, "!f1", mock_f1)

        handler.handle_command(bot, "nick", "#chan", "!f1 extra stuff")

        mock_f1.execute.assert_not_called()
        bot.send_message.assert_called_once_with("#chan", "Usage: !f1 (takes no arguments)")
        assert "!f1 does not allow arguments" in capsys.readouterr().out

    def test_usage_reply_names_the_alias_the_user_actually_typed(self, handler):
        bot = MagicMock()
        replace_command(handler, "!sähkö", MagicMock())

        handler.handle_command(bot, "nick", "#chan", "!sahko huomenna")

        bot.send_message.assert_called_once_with("#chan", "Usage: !sahko (takes no arguments)")

    def test_unknown_command_does_nothing(self, handler, capsys):
        bot = MagicMock()
        handler.handle_command(bot, "nick", "#chan", "!nonsense")
        bot.send_message.assert_not_called()
        assert "Unknown command: !nonsense" in capsys.readouterr().out

    def test_empty_response_does_not_send_message(self, handler):
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.return_value = ""
        replace_command(handler, "!weather", mock_weather)

        handler.handle_command(bot, "nick", "#chan", "!weather austin")

        bot.send_message.assert_not_called()

    def test_exception_in_command_does_not_propagate(self, handler, capsys):
        bot = MagicMock()
        mock_weather = MagicMock()
        mock_weather.execute.side_effect = RuntimeError("boom")
        replace_command(handler, "!weather", mock_weather)

        # Should not raise.
        handler.handle_command(bot, "nick", "#chan", "!weather austin")

        bot.send_message.assert_not_called()
        captured = capsys.readouterr()
        assert "Error handling command !weather austin: boom" in captured.out
        assert "Traceback" in captured.err

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

    def test_restricted_command_is_silently_ignored_in_other_channels(self, handler, capsys):
        bot = MagicMock()
        mock_superpesis = MagicMock()
        replace_command(handler, "!superpesis", mock_superpesis)

        handler.handle_command(bot, "nick", "#smliiga", "!superpesis start")

        mock_superpesis.execute.assert_not_called()
        bot.send_message.assert_not_called()
        out = capsys.readouterr().out
        assert "!superpesis is not allowed in channel #smliiga" in out

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



# The channels the bot actually joins (see src/irc_bot.py).
PRODUCTION_CHANNELS = ["#smliiga", "#valioliiga", "#nakkimuusi", "#pesis.fi", "#nhl.fi", "#veikkaus"]
GENERAL_COMMANDS = ["!weather", "!stock", "!crypto", "!sähkö", "!time", "!f1", "!distance", "!imdb", "!bjorck"]


class TestHelp:
    def test_help_lists_the_general_commands_everywhere(self, handler):
        text = handler.help_text("#nakkimuusi")
        assert text.startswith("Commands: ")
        for command in GENERAL_COMMANDS:
            assert command in text

    def test_channel_restricted_commands_only_show_in_their_own_channel(self, handler):
        general = handler.help_text("#nakkimuusi")
        for restricted in ("!liiga", "!nhl", "!superpesis", "!ykkospesis"):
            assert restricted not in general

        assert "!nhl start|stop|next" in handler.help_text("#nhl.fi")
        assert "!liiga start|stop|next" in handler.help_text("#smliiga")
        pesis = handler.help_text("#pesis.fi")
        assert "!superpesis start|stop|next" in pesis
        assert "!ykkospesis start|stop|next" in pesis
        assert "!nhl" not in pesis

    def test_each_command_is_listed_once_even_with_several_aliases(self, handler):
        text = handler.help_text("#nakkimuusi")
        assert text.count("!weather") == 1  # !w is an alias of the same command
        assert text.count("!sähkö") == 1  # so is !sahko

    def test_help_does_not_list_itself(self, handler):
        assert "!help" not in handler.help_text("#nakkimuusi")

    def test_bang_help_is_dispatched_through_handle_command(self, handler):
        bot = MagicMock()

        handler.handle_command(bot, "nick", "#nhl.fi", "!help")

        bot.send_message.assert_called_once()
        channel, message = bot.send_message.call_args[0]
        assert channel == "#nhl.fi"
        assert message.startswith("Commands: ") and "!nhl start|stop|next" in message

    def test_extra_text_after_help_still_gets_the_list(self, handler):
        bot = MagicMock()
        handler.handle_command(bot, "nick", "#nakkimuusi", "!help stock")
        assert bot.send_message.call_args[0][1].startswith("Commands: ")

    def test_every_registered_command_has_help_text(self, handler):
        # The guard that keeps the list complete: a new command that
        # forgets to set HELP fails here instead of silently going
        # missing from !help.
        for command in handler._commands:
            assert command.HELP, f"{type(command).__name__} has no HELP string"

    def test_help_fits_on_one_irc_line_in_every_production_channel(self, handler):
        # IRC lines cap at 512 bytes including the protocol framing the
        # server adds when relaying - stay well clear of it.
        for channel in PRODUCTION_CHANNELS:
            text = handler.help_text(channel)
            assert len(text.encode("utf-8")) <= 400, f"{channel}: {len(text)} chars"


class TestUsageConvention:
    def test_argument_taking_commands_reply_with_usage_when_called_bare(self, handler):
        # The guard behind CLAUDE.md's "Adding a command" step 3: a new
        # argument-taking command that answers a bare call with anything
        # other than a "Usage:" line fails here instead of going unnoticed.
        checked = []
        for command in handler._commands:
            if not command.ALLOW_ARGS:
                continue
            if command.needs_irc_context:
                reply = command.execute("", irc_bot=MagicMock(), channel="#chan")
            else:
                reply = command.execute("")
            assert reply and reply.startswith("Usage:"), (
                f"{type(command).__name__} answered a bare call with: {reply!r}"
            )
            checked.append(type(command).__name__)

        # Guards against the loop silently checking nothing (e.g. if
        # ALLOW_ARGS defaults ever flip).
        assert len(checked) >= 6


class TestExclusiveChannel:
    """#veikkaus allows only !liiga (and !help, which lists just !liiga
    there). Every other channel keeps the default rules."""

    def _run(self, handler, channel, message):
        bot = MagicMock()
        handler.handle_command(bot, "nick", channel, message)
        return bot

    def test_liiga_is_dispatched_in_veikkaus(self, handler):
        mock = MagicMock()
        mock.execute.return_value = "ok"
        replace_command(handler, "!liiga", mock)

        bot = self._run(handler, "#veikkaus", "!liiga start")

        mock.execute.assert_called_once()
        assert mock.execute.call_args.kwargs["channel"] == "#veikkaus"

    @pytest.mark.parametrize("message", [
        "!weather helsinki", "!w helsinki", "!stock aapl", "!crypto btc", "!sähkö", "!sahko",
        "!time", "!f1", "!distance a b", "!imdb terminator", "!bjorck", "!nhl start",
        "!superpesis start",
    ])
    def test_every_other_command_is_ignored_in_veikkaus(self, handler, message):
        bot = self._run(handler, "#veikkaus", message)
        bot.send_message.assert_not_called()

    def test_other_commands_are_not_even_executed_in_veikkaus(self, handler):
        mock = MagicMock()
        replace_command(handler, "!weather", mock)
        self._run(handler, "#veikkaus", "!weather helsinki")
        mock.execute.assert_not_called()

    def test_help_works_in_veikkaus_and_lists_only_liiga(self, handler):
        bot = self._run(handler, "#veikkaus", "!help")
        assert bot.send_message.call_args[0] == ("#veikkaus", "Commands: !liiga start|stop|next")

    def test_channel_name_matching_is_case_insensitive(self, handler):
        assert handler.help_text("#Veikkaus") == "Commands: !liiga start|stop|next"
        bot = self._run(handler, "#VEIKKAUS", "!stock aapl")
        bot.send_message.assert_not_called()

    def test_liiga_still_works_in_smliiga_and_general_commands_stay_out_of_it_only_via_veikkaus(self, handler):
        assert "!liiga start|stop|next" in handler.help_text("#smliiga")
        assert "!weather" in handler.help_text("#smliiga")
        assert "!liiga" not in handler.help_text("#nakkimuusi")

    def test_help_and_dispatch_agree_for_every_command_in_every_channel(self, handler):
        for channel in PRODUCTION_CHANNELS:
            listed = handler.help_text(channel)
            for command in handler._commands:
                allowed = handler.is_allowed(command, channel)
                assert (command.HELP in listed) == allowed, f"{command.ALIASES} in {channel}"
