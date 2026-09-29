from base_command import BaseCommand


class HelpCommand(BaseCommand):
    """Lists the commands usable in the channel it's asked in, on one IRC
    line. Built from each command's own HELP string and CHANNELS
    restriction (see CommandHandler.help_text()), so a command added to
    COMMAND_CLASSES shows up here without touching this class.

    Takes the CommandHandler itself (not just a list of commands) since it
    can only be built once every other command exists - which is why it's
    registered by CommandHandler.__init__ directly rather than listed in
    COMMAND_CLASSES like the rest.
    """

    ALIASES = ("!help",)
    needs_irc_context = True

    def __init__(self, command_handler):
        self._command_handler = command_handler

    def execute(self, args=None, irc_bot=None, channel=None):
        return self._command_handler.help_text(channel)
