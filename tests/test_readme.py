"""README.md's commands table and Channels section, checked against the code and channels.toml: the
aliases every command answers to, the channels the bot joins, and what each adds to the default set. A command or
channel change that leaves the README behind fails here (the bot left a channel once and the README
went on naming it in five places)."""
import os
import re

import pytest

from command_handler import CommandHandler

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
        """{channel: [commands added to the default set]} as the README's Channels table states them."""
        table = {}
        for cells in table_rows(section(readme, "Channels")):
            (channel,) = code_spans(cells[0])
            table[channel] = [] if cells[1] == NO_COMMANDS else code_spans(cells[1])
        return table

    def test_the_table_lists_exactly_the_channels_the_bot_joins(self, readme, handler):
        assert list(self._table(readme)) == list(handler.channel_config.channels)

    def test_each_channels_additions_are_the_ones_in_channels_toml(self, readme, handler):
        rules = handler.channel_config.rules
        for channel, documented in self._table(readme).items():
            assert sorted(documented) == sorted(rules[channel].add), channel

    def test_the_default_set_is_every_command_but_the_channel_only_ones_as_the_section_says(self, handler):
        general = {c.ALIASES[0] for c in handler._commands if not c.CHANNEL_ONLY}
        assert handler.channel_config.default_commands == general
        assert handler.channel_config.default_link_titles is True

    def test_a_channel_off_the_default_rules_is_named_in_the_section(self, readme, handler):
        body = section(readme, "Channels")
        config = handler.channel_config
        unusual = {
            channel: (*rule.remove, *(rule.only or ()))
            for channel, rule in config.rules.items()
            if rule.remove or rule.only is not None or rule.link_titles != config.default_link_titles
        }
        if not unusual:
            assert "No channel does now." in body
            return
        assert "No channel does now." not in body
        for channel, aliases in unusual.items():
            assert f"`{channel}`" in body and all(f"`{alias}`" in body for alias in aliases), channel

    def test_no_other_part_of_the_readme_names_a_channel(self, readme):
        # Prose that names channels cannot be checked, so the Channels table is the only place for them.
        outside = readme.replace(section(readme, "Channels"), "")
        # "#name" in or out of backticks; issue numbers (#44) and section links ((#channels)) are not channels
        named = {"#" + name for name in re.findall(r"(?<![\w(/&])`?#([A-Za-z][\w.-]*)", outside)}
        named -= {"#bottest123"}  # the --debug channel, in Running
        assert not named, f"channels named outside the Channels section: {sorted(named)}"
