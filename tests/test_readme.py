"""README.md's commands table and Channels section, checked against the code: the aliases every command
answers to, the channels the bot joins, and which channel-restricted command works where. A command or
channel change that leaves the README behind fails here (the bot left a channel once and the README
went on naming it in five places)."""
import os
import re

import pytest

from command_handler import CommandHandler
from irc_bot import PRODUCTION_CHANNELS

README = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
NO_COMMANDS = "none"


def section(text, heading):
    """The body of the `## heading` section."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match, f"README.md has no '## {heading}' section"
    return match.group(1)


def table_rows(body):
    """The cells of each row of the first table in `body`, header and separator left out."""
    rows = [line for line in body.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows[2:]]


def code_spans(cell):
    return re.findall(r"`([^`]+)`", cell)


@pytest.fixture(scope="module")
def readme():
    with open(README, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def handler():
    return CommandHandler()


class TestCommandsTable:
    def test_every_alias_the_bot_answers_to_is_in_the_table_and_nothing_else(self, readme, handler):
        documented = {alias for row in table_rows(section(readme, "Commands")) for alias in code_spans(row[1])}
        assert documented == set(handler.commands_by_alias)


class TestChannelsSection:
    def _table(self, readme):
        """{channel: [channel-restricted commands]} as the README's Channels table states them."""
        table = {}
        for cells in table_rows(section(readme, "Channels")):
            (channel,) = code_spans(cells[0])
            table[channel] = [] if cells[1] == NO_COMMANDS else code_spans(cells[1])
        return table

    def test_the_table_lists_exactly_the_channels_the_bot_joins(self, readme):
        assert sorted(self._table(readme)) == sorted(PRODUCTION_CHANNELS)

    def test_each_channels_restricted_commands_are_the_ones_the_code_allows_there(self, readme, handler):
        restricted = [c for c in handler._commands if c.CHANNELS]
        for channel, documented in self._table(readme).items():
            allowed = [c.ALIASES[0] for c in restricted if handler.is_allowed(c, channel)]
            assert sorted(documented) == sorted(allowed), channel

    def test_no_command_is_restricted_to_a_channel_the_bot_does_not_join(self, handler):
        joined = {c.lower() for c in PRODUCTION_CHANNELS}
        for command in handler._commands:
            stray = [c for c in command.CHANNELS or () if c.lower() not in joined]
            assert not stray, f"{command.ALIASES[0]} is restricted to {stray}, which the bot does not join"

    def test_exclusive_channels_are_joined_and_named_in_the_section(self, readme, handler):
        body = section(readme, "Channels")
        if not handler.EXCLUSIVE_CHANNELS:
            assert "No channel is limited now." in body
            return
        assert "No channel is limited now." not in body
        joined = {c.lower() for c in PRODUCTION_CHANNELS}
        for channel, aliases in handler.EXCLUSIVE_CHANNELS.items():
            assert channel in joined, f"exclusive channel {channel} is not joined"
            assert f"`{channel}`" in body and all(f"`{alias}`" in body for alias in aliases), channel

    def test_no_other_part_of_the_readme_names_a_channel(self, readme):
        # Prose that names channels cannot be checked, so the Channels table is the only place for them.
        outside = readme.replace(section(readme, "Channels"), "")
        # "#name" in or out of backticks; issue numbers (#44) and section links ((#channels)) are not channels
        named = {"#" + name for name in re.findall(r"(?<![\w(/&])`?#([A-Za-z][\w.-]*)", outside)}
        named -= {"#bottest123"}  # the --debug channel, in Running
        assert not named, f"channels named outside the Channels section: {sorted(named)}"
