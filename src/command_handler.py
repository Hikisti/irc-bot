import traceback
from electricity import ElectricityCommand
from weather import WeatherCommand
from stock import StockCommand
from crypto import CryptoCommand
from aijamatto import AijaMattoCommand
from time_command import TimeCommand
from f1_command import F1Command
from help_command import HelpCommand
from liiga_command import LiigaCommand
from distance_command import DistanceCommand
from imdb_command import ImdbCommand
from nhl_command import NHLCommand
from pesis_command import SuperpesisCommand, YkkospesisCommand

class CommandHandler:
    """Handles IRC bot commands and delegates them to specific classes.

    See base_command.py for the interface every command class here
    implements (ALIASES/ALLOW_ARGS/CHANNELS, execute()'s signature, the
    optional needs_irc_context attribute) - dispatch below is still
    plain duck-typing (attribute access, not isinstance()), just built
    once from each class's own declared ALIASES instead of a
    hand-maintained dict of instance -> config here.
    """

    # Every command class the bot knows about - the only place that has
    # to change to register a new one; everything else (alias lookup,
    # ALLOW_ARGS/CHANNELS enforcement, its line in !help) is driven by
    # each class's own BaseCommand attributes. (HelpCommand is the one
    # exception: it needs the finished handler, so __init__ registers it.)
    COMMAND_CLASSES = [
        ElectricityCommand,
        WeatherCommand,
        StockCommand,
        CryptoCommand,
        AijaMattoCommand,
        TimeCommand,
        F1Command,
        LiigaCommand,
        DistanceCommand,
        ImdbCommand,
        NHLCommand,
        SuperpesisCommand,
        YkkospesisCommand,
    ]

    # Channels where ONLY the listed aliases work (checked before each
    # command's own CHANNELS) and links posted are not expanded into page
    # titles either. Every channel not named here keeps the default: all
    # commands, subject to their own CHANNELS, plus link titles. To open
    # one more command up in such a channel, add its alias here (and the
    # channel to its own CHANNELS if it has one).
    EXCLUSIVE_CHANNELS = {
        "#veikkaus": ("!liiga", "!nhl", "!help"),
    }

    def __init__(self):
        self.commands_by_alias = {}
        self._commands = []  # each command once (its aliases share one instance)
        for command_class in self.COMMAND_CLASSES:
            instance = command_class()
            self._commands.append(instance)
            for alias in instance.ALIASES:
                self.commands_by_alias[alias] = instance

        help_command = HelpCommand(self)
        for alias in help_command.ALIASES:
            self.commands_by_alias[alias] = help_command

    def is_allowed(self, command, channel, aliases=None) -> bool:
        """The one place that decides whether `command` may run in
        `channel`, used by both handle_command() and help_text() so !help
        can never list something dispatch would ignore (or the reverse).
        `aliases` defaults to all of the command's own; dispatch passes
        just the one that was typed. IRC channel names are
        case-insensitive, so both rules compare lowercased."""
        channel = (channel or "").lower()
        exclusive = self.EXCLUSIVE_CHANNELS.get(channel)
        if exclusive is not None and not any(a in exclusive for a in (aliases or command.ALIASES)):
            return False
        return not command.CHANNELS or channel in (c.lower() for c in command.CHANNELS)

    def url_titles_allowed(self, channel) -> bool:
        """False in an exclusive channel: the bot does nothing there
        beyond its listed commands, so posted links get no title reply."""
        return (channel or "").lower() not in self.EXCLUSIVE_CHANNELS

    def help_text(self, channel) -> str:
        """One line listing every command usable in `channel` (same
        is_allowed() rule handle_command() enforces), in COMMAND_CLASSES
        order."""
        entries = [
            command.HELP for command in self._commands
            if command.HELP and self.is_allowed(command, channel)
        ]
        return "Commands: " + " | ".join(entries)

    def handle_command(self, irc_bot, nick, channel, message):
        """Parses and executes commands from IRC messages, handling aliases and argument restrictions."""
        try:
            parts = message.split(" ", 1)  # Split command and arguments
            command = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            main_command = self.commands_by_alias.get(command)
            if main_command is None:
                print(f"Unknown command: {command}")  # Debugging
                return

            # Some commands are restricted to specific channels (e.g. !superpesis
            # -> #pesis.fi only), and some channels to specific commands
            # (EXCLUSIVE_CHANNELS). Otherwise a command works in every
            # channel the bot has joined.
            if not self.is_allowed(main_command, channel, aliases=(command,)):
                print(f"Command {command} is not allowed in channel {channel}. Ignoring.")
                return  # Ignore command

            if not main_command.ALLOW_ARGS and args:
                # Replying (rather than silently dropping) so "!f1 2026"
                # doesn't look like a broken bot.
                print(f"Command {command} does not allow arguments. Replying with usage.")
                irc_bot.send_message(channel, f"Usage: {command} (takes no arguments)")
                return

            if getattr(main_command, "needs_irc_context", False) is True:
                response = main_command.execute(args, irc_bot=irc_bot, channel=channel)
            else:
                response = main_command.execute(args)

            if response:
                irc_bot.send_message(channel, response)  # Send response to IRC
        except Exception as e:
            print(f"Error handling command {message}: {e}")
            traceback.print_exc()
