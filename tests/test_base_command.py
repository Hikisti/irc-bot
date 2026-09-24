import pytest

from src.base_command import BaseCommand


class TestBaseCommand:
    def test_needs_irc_context_defaults_to_false(self):
        assert BaseCommand.needs_irc_context is False

    def test_execute_is_not_implemented(self):
        with pytest.raises(NotImplementedError):
            BaseCommand().execute("some args")
