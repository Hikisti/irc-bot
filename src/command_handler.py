import traceback
from electricity import ElectricityCommand
from weather import WeatherCommand

class CommandHandler:
    """Handles IRC bot commands and delegates them to specific classes."""
    
    def __init__(self):
        self.commands = {
            "!sähkö": ElectricityCommand(),
            "!weather": WeatherCommand()
        }

    def handle_command(self, irc_bot, nick, channel, message):
        """Parses and executes commands from IRC messages."""
        try:
            parts = message.split(" ", 1)  # Split command and arguments
            command = parts[0].lower()

            if command in self.commands:
                args = parts[1] if len(parts) > 1 else ""
                response = self.commands[command].execute(args)
                
                if response:
                    irc_bot.send_message(channel, response)  # Send response to IRC
            else:
                print(f"Unknown command: {command}")  # Debugging
        except Exception as e:
            print(f"Error handling command {message}: {e}")
            traceback.print_exc()
