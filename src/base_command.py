"""The interface CommandHandler expects from every command class -
dispatch itself still goes through getattr()/plain attribute access, not
isinstance(), so a command technically doesn't have to subclass this to
work; every command in this codebase does anyway, since it's the only
place these class attributes' defaults live."""


class BaseCommand:
    """The shape CommandHandler.handle_command() expects:

    - `ALIASES` (required, tuple/list of str): the command words that
      route to this class, e.g. `("!weather", "!w")`. CommandHandler
      builds its whole alias -> command-instance lookup from this at
      startup - a class with no entry here is never reachable.
    - `ALLOW_ARGS` (default True): set to False for a command that takes
      no arguments (e.g. "!f1") - a message like "!f1 something" is then
      silently ignored rather than reaching execute() at all.
    - `CHANNELS` (default None, meaning "every channel the bot has
      joined"): restrict a command to specific channels, e.g.
      `("#pesis.fi",)` for !superpesis/!ykkospesis.
    - `execute(args, irc_bot=None, channel=None) -> str | None`. `args`
      is the text after the command word (e.g. "austin" for
      "!weather austin"), or "" if none was given. The return value is
      sent back to the channel verbatim via irc_bot.send_message() -
      return None (or anything falsy) to send nothing.
    - `needs_irc_context` (default False): set to True only if execute()
      needs the `irc_bot`/`channel` keyword arguments (e.g. to spawn a
      background thread that sends messages of its own later, the way
      LiveTrackerCommand's live trackers do). Most commands are a single
      synchronous request-and-reply and don't need this.

    A command's own __init__() should never raise on a recoverable
    problem (e.g. a missing API key) - check for that lazily in
    execute() instead and return an error string, so one misconfigured
    command doesn't prevent CommandHandler from constructing every other
    command at startup (see WeatherCommand/DistanceCommand for the
    pattern this refers to).
    """

    ALIASES = ()
    ALLOW_ARGS = True
    CHANNELS = None
    needs_irc_context = False

    def execute(self, args=None, irc_bot=None, channel=None) -> str:
        raise NotImplementedError
