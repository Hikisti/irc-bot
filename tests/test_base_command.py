import pytest

from src.base_command import BaseCommand


class TestBaseCommand:
    def test_needs_irc_context_defaults_to_false(self):
        assert BaseCommand.needs_irc_context is False

    def test_aliases_defaults_to_empty(self):
        assert BaseCommand.ALIASES == ()

    def test_allow_args_defaults_to_true(self):
        assert BaseCommand.ALLOW_ARGS is True

    def test_channels_defaults_to_none(self):
        assert BaseCommand.CHANNELS is None

    def test_execute_is_not_implemented(self):
        with pytest.raises(NotImplementedError):
            BaseCommand().execute("some args")
