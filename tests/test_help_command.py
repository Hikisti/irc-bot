from unittest.mock import MagicMock

from help_command import HelpCommand


class TestHelpCommand:
    def test_execute_returns_the_handlers_help_text_for_the_channel(self):
        handler = MagicMock()
        handler.help_text.return_value = "Commands: !x"

        result = HelpCommand(handler).execute("", irc_bot=MagicMock(), channel="#chan")

        assert result == "Commands: !x"
        handler.help_text.assert_called_once_with("#chan")

    def test_extra_text_after_help_is_ignored_not_rejected(self):
        # "!help stock" is a natural thing to type - it should just work.
        assert HelpCommand(MagicMock()).ALLOW_ARGS is True

    def test_it_receives_the_irc_context_it_needs_for_the_channel(self):
        assert HelpCommand(MagicMock()).needs_irc_context is True
