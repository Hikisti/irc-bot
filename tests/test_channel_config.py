"""channels.toml and the rules read from it. The real file is loaded here too, so a mistake in it
fails CI before a deploy instead of stopping the bot at startup."""
import pytest

import channel_config
from channel_config import ChannelConfigError, parse
from command_handler import CommandHandler


class Command:
    def __init__(self, *aliases, channel_only=False):
        self.ALIASES = aliases
        self.CHANNEL_ONLY = channel_only


COMMANDS = [Command("!weather", "!w"), Command("!stock"), Command("!liiga", channel_only=True)]


def config(channels, default=None, debug=None):
    data = {"debug": debug or {"channel": "#debug"}, "channels": channels}
    if default is not None:
        data["default"] = default
    return parse(data, COMMANDS)


@pytest.fixture(scope="module")
def handler():
    return CommandHandler()


class TestTheRealFile:
    # What worked where when the rules were spread over PRODUCTION_CHANNELS, each command's
    # CHANNELS and EXCLUSIVE_CHANNELS. Moving them into channels.toml changed none of it.
    TRACKERS = {
        "!liiga": {"#smliiga"},
        "!nhl": {"#nhl.fi"},
        "!superpesis": {"#pesis.fi"},
        "!ykkospesis": {"#pesis.fi"},
    }
    JOINED = ("#smliiga", "#valioliiga", "#nakkimuusi", "#pesis.fi", "#nhl.fi")

    def test_it_loads(self, handler):
        assert isinstance(handler.channel_config, channel_config.ChannelConfig)

    def test_the_bot_joins_the_same_channels_as_before(self, handler):
        assert handler.channel_config.channels == self.JOINED
        assert handler.channel_config.debug_channel == "#bottest123"

    def test_every_command_works_in_exactly_the_channels_it_did_before(self, handler):
        for command in handler._commands:
            alias = command.ALIASES[0]
            expected = self.TRACKERS.get(alias, {*self.JOINED, "#bottest123"})
            allowed = {c for c in (*self.JOINED, "#bottest123") if handler.is_allowed(command, c)}
            assert allowed == expected, alias

    def test_link_titles_are_on_everywhere(self, handler):
        assert all(handler.url_titles_allowed(c) for c in (*self.JOINED, "#bottest123"))

    def test_the_live_trackers_are_the_channel_only_commands(self, handler):
        assert {c.ALIASES[0] for c in handler._commands if c.CHANNEL_ONLY} == set(self.TRACKERS)


class TestRules:
    def test_a_channel_with_nothing_to_say_gets_the_general_commands(self):
        rules = config({"#a": {}})
        assert rules.allows("!weather", "#a") and rules.allows("!stock", "#a")
        assert not rules.allows("!liiga", "#a")
        assert rules.link_titles("#a")

    def test_add_brings_a_channel_only_command_in(self):
        rules = config({"#a": {"add": ["!liiga"]}, "#b": {}})
        assert rules.allows("!liiga", "#a") and not rules.allows("!liiga", "#b")

    def test_remove_takes_a_command_out(self):
        rules = config({"#a": {"remove": ["!stock"]}})
        assert not rules.allows("!stock", "#a") and rules.allows("!weather", "#a")

    def test_only_replaces_the_default_set(self):
        rules = config({"#a": {"only": ["!stock", "!liiga"], "link_titles": False}})
        assert rules.allows("!stock", "#a") and rules.allows("!liiga", "#a")
        assert not rules.allows("!weather", "#a")
        assert not rules.link_titles("#a")

    def test_help_always_works(self):
        rules = config({"#a": {"only": ["!stock"]}})
        assert rules.allows("!help", "#a")

    def test_an_explicit_default_list(self):
        rules = config({"#a": {}}, default={"commands": ["!stock"], "link_titles": False})
        assert rules.allows("!stock", "#a") and not rules.allows("!weather", "#a")
        assert not rules.link_titles("#a")

    def test_a_channel_can_turn_link_titles_back_on(self):
        rules = config({"#a": {"link_titles": True}, "#b": {}}, default={"link_titles": False})
        assert rules.link_titles("#a") and not rules.link_titles("#b")

    def test_channel_names_ignore_case(self):
        rules = config({"#A": {"add": ["!liiga"], "link_titles": False}})
        assert rules.allows("!liiga", "#a") and not rules.link_titles("#a")

    def test_a_channel_not_in_the_file_gets_the_default_set(self):
        rules = config({"#a": {"add": ["!liiga"]}})
        assert rules.allows("!weather", "#debug") and not rules.allows("!liiga", "#debug")
        assert rules.link_titles("#debug")

    def test_channels_keep_file_order(self):
        assert config({"#b": {}, "#a": {}}).channels == ("#b", "#a")


class TestMistakesFailLoudly:
    @pytest.mark.parametrize("channels, default, message", [
        ({"#a": {"add": ["!nope"]}}, None, "unknown command !nope"),
        ({"#a": {"add": ["!w"]}}, None, "name !w by its first alias, !weather"),
        ({"#a": {"only": ["!help"]}}, None, "!help always works"),
        ({"#a": {"only": ["!stock"], "add": ["!liiga"]}}, None, "cannot go with add/remove"),
        ({"#a": {}}, {"commands": ["!liiga"]}, "!liiga is channel-only"),
        ({"#a": {}}, {"commands": "everything"}, "expected a list of commands"),
        ({"#a": {"add": "!liiga"}}, None, "expected a list of commands"),
        ({"#a": {"ad": ["!liiga"]}}, None, "unknown key ad"),
        ({"#a": {"link_titles": "no"}}, None, "link_titles must be true or false"),
        ({"a": {}}, None, "'a' is not a channel name"),
        ({"#a": {}, "#A": {}}, None, "listed twice"),
        ({}, None, "no channels"),
    ])
    def test_a_mistake_is_named(self, channels, default, message):
        with pytest.raises(ChannelConfigError, match=message):
            config(channels, default=default)

    def test_a_missing_debug_channel(self):
        with pytest.raises(ChannelConfigError, match=r"\[debug\]"):
            parse({"channels": {"#a": {}}}, COMMANDS)

    def test_an_unknown_top_level_table(self):
        with pytest.raises(ChannelConfigError, match="unknown key channel"):
            parse({"debug": {"channel": "#d"}, "channels": {"#a": {}}, "channel": {}}, COMMANDS)

    def test_the_file_name_is_in_the_message(self, tmp_path):
        path = tmp_path / "channels.toml"
        path.write_text('[debug]\nchannel = "#d"\n[channels."#a"]\nadd = ["!nope"]\n')
        with pytest.raises(ChannelConfigError, match=r"channels.toml: \[channels.\"#a\"\] add: unknown command"):
            channel_config.load(COMMANDS, path=str(path))

    def test_a_toml_syntax_error_names_the_file(self, tmp_path):
        path = tmp_path / "channels.toml"
        path.write_text("[channels\n")
        with pytest.raises(ChannelConfigError, match="channels.toml: "):
            channel_config.load(COMMANDS, path=str(path))
