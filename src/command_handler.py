import traceback
from electricity import ElectricityCommand
from weather import WeatherCommand

class CommandHandler:
    """Handles IRC bot commands and delegates them to specific classes."""
    
    def __init__(self):
        self.commands = {}

        # Define aliases
        self.command_aliases = {
            ElectricityCommand(): ["!sähkö", "!sahko"],
            WeatherCommand(): ["!weather", "!w"]
        }

        # Populate commands dictionary with aliases
        for command, aliases in self.command_aliases.items():
            for alias in aliases:
                self.commands[alias] = command

    def handle_command(self, irc_bot, nick, channel, message):
        """Parses and executes commands from IRC messages, handling aliases."""
        try:
            parts = message.split(" ", 1)  # Split command and arguments
            command = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            # Resolve alias to main command
            main_command = self.command_aliases.get(command, command)

            if main_command in self.commands:
                response = self.commands[main_command].execute(args)

                if response:
                    irc_bot.send_message(channel, response)  # Send response to IRC
            else:
                print(f"Unknown command: {command}")  # Debugging
        except Exception as e:
            print(f"Error handling command {message}: {e}")
            traceback.print_exc()
            