import traceback
from electricity import ElectricityCommand
from weather import WeatherCommand
from stock import StockCommand
from crypto import CryptoCommand
from aijamatto import AijaMattoCommand
from time_command import TimeCommand
from f1_command import F1Command
from liiga_command import LiigaCommand
from distance_command import DistanceCommand
from imdb_command import ImdbCommand
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
    # ALLOW_ARGS/CHANNELS enforcement) is driven by each class's own
    # BaseCommand attributes.
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
        SuperpesisCommand,
        YkkospesisCommand,
    ]

    def __init__(self):
        self.commands_by_alias = {}
        for command_class in self.COMMAND_CLASSES:
            instance = command_class()
            for alias in instance.ALIASES:
                self.commands_by_alias[alias] = instance

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
            # -> #pesis.fi only). CHANNELS is None for every other command, so it
            # works in every channel the bot has joined.
            if main_command.CHANNELS and channel not in main_command.CHANNELS:
                print(f"Command {command} is not allowed in channel {channel}. Ignoring.")
                return  # Ignore command

            if not main_command.ALLOW_ARGS and args:
                print(f"Command {command} does not allow arguments. Ignoring.")
                return  # Ignore command

            if getattr(main_command, "needs_irc_context", False) is True:
                response = main_command.execute(args, irc_bot=irc_bot, channel=channel)
            else:
                response = main_command.execute(args)

            if response:
                irc_bot.send_message(channel, response)  # Send response to IRC
        except Exception as e:
            print(f"Error handling command {message}: {e}")
            traceback.print_exc()
