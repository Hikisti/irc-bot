"""Documents the interface CommandHandler expects from every command
class - not enforced by inheritance (existing commands aren't changed to
subclass this; CommandHandler still dispatches by plain duck-typing, via
getattr() for the optional attributes), just a single place spelling out
a contract that was previously only implicit and inferred by reading
command_handler.py itself.

A new command doesn't have to subclass this - it just has to look like
this.
"""


class BaseCommand:
    """The shape CommandHandler.handle_command() expects:

    - `execute(args, irc_bot=None, channel=None) -> str | None`. `args`
      is the text after the command word (e.g. "austin" for
      "!weather austin"), or "" if none was given. The return value is
      sent back to the channel verbatim via irc_bot.send_message() -
      return None (or anything falsy) to send nothing.
    - `needs_irc_context` (optional class attribute, default False): set
      to True only if execute() needs the `irc_bot`/`channel` keyword
      arguments (e.g. to spawn a background thread that sends messages
      of its own later, the way LiigaCommand/PesisCommand's live
      trackers do). Most commands are a single synchronous
      request-and-reply and don't need this.

    A command's own __init__() should never raise on a recoverable
    problem (e.g. a missing API key) - check for that lazily in
    execute() instead and return an error string, so one misconfigured
    command doesn't prevent CommandHandler from constructing every other
    command at startup (see WeatherCommand/DistanceCommand for the
    pattern this refers to).
    """

    needs_irc_context = False

    def execute(self, args=None, irc_bot=None, channel=None) -> str:
        raise NotImplementedError
