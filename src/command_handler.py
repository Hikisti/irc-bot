import traceback
from electricity import ElectricityCommand
from weather import WeatherCommand
from stock import StockCommand

class CommandHandler:
    """Handles IRC bot commands and delegates them to specific classes."""
    
    def __init__(self):
        # Define aliases and argument restrictions
        self.command_aliases = {
            ElectricityCommand(): {"aliases": ["!sähkö", "!sahko"], "allow_args": False},
            WeatherCommand(): {"aliases": ["!weather", "!w"], "allow_args": True},
            StockCommand(): {"aliases": ["!stock"], "allow_args": True}
        }

    def handle_command(self, irc_bot, nick, channel, message):
        """Parses and executes commands from IRC messages, handling aliases and argument restrictions."""
        try:
            parts = message.split(" ", 1)  # Split command and arguments
            command = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            # Find the main command object based on alias
            main_command = None
            for cmd_obj, cmd_data in self.command_aliases.items():
                if command in cmd_data["aliases"]:
                    main_command = cmd_obj
                    allow_args = cmd_data["allow_args"]
                    break  # Stop searching once we find a match

            if main_command:
                # Check if arguments are allowed for this command
                if not allow_args and args:
                    print(f"Command {command} does not allow arguments. Ignoring.")
                    return  # Ignore command

                response = main_command.execute(args)

                if response:
                    irc_bot.send_message(channel, response)  # Send response to IRC
            else:
                print(f"Unknown command: {command}")  # Debugging
        except Exception as e:
            print(f"Error handling command {message}: {e}")
            traceback.print_exc()
