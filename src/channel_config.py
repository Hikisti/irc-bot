"""The channels the bot joins and which commands work in each, read from channels.toml at the
repository root. The file is checked as it is read: a mistake in it stops the bot at startup with a
message naming it, instead of leaving a command quietly on or off somewhere."""
import os
import tomllib
from dataclasses import dataclass

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "channels.toml")
GENERAL = "general"
HELP_ALIAS = "!help"


class ChannelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ChannelRule:
    """One channel's entry as written: what it adds to or removes from the default set, or its own set."""
    add: tuple = ()
    remove: tuple = ()
    only: tuple | None = None
    link_titles: bool = True


class ChannelConfig:
    def __init__(self, rules, debug_channel, default_commands, default_link_titles):
        self.rules = rules  # {channel as written: ChannelRule}, in file order
        self.debug_channel = debug_channel
        self.default_commands = frozenset(default_commands)
        self.default_link_titles = default_link_titles
        self._commands = {}
        self._link_titles = {}
        for channel, rule in rules.items():
            if rule.only is not None:
                commands = set(rule.only)
            else:
                commands = (self.default_commands | set(rule.add)) - set(rule.remove)
            self._commands[channel.lower()] = frozenset(commands)
            self._link_titles[channel.lower()] = rule.link_titles

    @property
    def channels(self):
        """The channels the bot joins in production, in file order."""
        return tuple(self.rules)

    def allows(self, main_alias, channel) -> bool:
        """IRC channel names are case-insensitive. A channel not in the file (the debug one) gets the default set."""
        if main_alias == HELP_ALIAS:
            return True
        return main_alias in self._commands.get((channel or "").lower(), self.default_commands)

    def link_titles(self, channel) -> bool:
        return self._link_titles.get((channel or "").lower(), self.default_link_titles)


def load(commands, path=CONFIG_FILE):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ChannelConfigError(f"{os.path.basename(path)}: {e}") from e
    try:
        return parse(data, commands)
    except ChannelConfigError as e:
        raise ChannelConfigError(f"{os.path.basename(path)}: {e}") from e


def parse(data, commands):
    """`data` is the parsed TOML; `commands` the command instances, whose first alias names each one."""
    main_aliases = {c.ALIASES[0]: c for c in commands}
    channel_only = {alias for alias, c in main_aliases.items() if getattr(c, "CHANNEL_ONLY", False)}
    main_alias_of = {alias: c.ALIASES[0] for c in commands for alias in c.ALIASES}

    def names(value, where):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ChannelConfigError(f"{where}: expected a list of commands, e.g. [\"!stock\"], got {value!r}")
        for name in value:
            if name == HELP_ALIAS:
                raise ChannelConfigError(f"{where}: {HELP_ALIAS} always works, leave it out")
            if name not in main_aliases:
                if name in main_alias_of:
                    raise ChannelConfigError(f"{where}: name {name} by its first alias, {main_alias_of[name]}")
                raise ChannelConfigError(f"{where}: unknown command {name}")
        return tuple(value)

    def link_titles(table, where, default):
        value = table.get("link_titles", default)
        if not isinstance(value, bool):
            raise ChannelConfigError(f"{where}: link_titles must be true or false, got {value!r}")
        return value

    _only_keys(data, {"default", "debug", "channels"}, "the file")

    default = _table(data.get("default", {}), "[default]")
    _only_keys(default, {"commands", "link_titles"}, "[default]")
    default_spec = default.get("commands", GENERAL)
    if default_spec == GENERAL:
        default_commands = set(main_aliases) - channel_only
    else:
        default_commands = set(names(default_spec, "[default] commands"))
        if default_commands & channel_only:
            raise ChannelConfigError(
                f"[default] commands: {', '.join(sorted(default_commands & channel_only))} is channel-only;"
                " add it in the channels that should have it")
    default_link_titles = link_titles(default, "[default]", True)

    debug = _table(data.get("debug"), "[debug]")
    _only_keys(debug, {"channel"}, "[debug]")
    debug_channel = _channel_name(debug.get("channel"), "[debug] channel")

    channels = _table(data.get("channels"), "[channels]")
    if not channels:
        raise ChannelConfigError("no channels: the bot would join nothing")
    rules, seen = {}, set()
    for channel, entry in channels.items():
        where = f'[channels."{channel}"]'
        _channel_name(channel, where)
        if channel.lower() in seen:
            raise ChannelConfigError(f"{where}: listed twice (channel names ignore case)")
        seen.add(channel.lower())
        entry = _table(entry, where)
        _only_keys(entry, {"add", "remove", "only", "link_titles"}, where)
        if "only" in entry and ({"add", "remove"} & set(entry)):
            raise ChannelConfigError(f"{where}: `only` replaces the default set, so it cannot go with add/remove")
        rules[channel] = ChannelRule(
            add=names(entry.get("add", []), f"{where} add"),
            remove=names(entry.get("remove", []), f"{where} remove"),
            only=names(entry["only"], f"{where} only") if "only" in entry else None,
            link_titles=link_titles(entry, where, default_link_titles),
        )
    return ChannelConfig(rules, debug_channel, default_commands, default_link_titles)


def _table(value, where):
    if not isinstance(value, dict):
        raise ChannelConfigError(f"{where}: missing, or not a table")
    return value


def _only_keys(table, allowed, where):
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ChannelConfigError(f"{where}: unknown key {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def _channel_name(value, where):
    if not isinstance(value, str) or not value.startswith("#") or " " in value:
        raise ChannelConfigError(f"{where}: {value!r} is not a channel name")
    return value
