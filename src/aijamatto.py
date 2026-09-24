import os
import random

from base_command import BaseCommand


class AijaMattoCommand(BaseCommand):
    """Returns one random line from aijamatto.txt (an old in-joke/copypasta
    collection) - one per !bjorck call.

    Usage:
      !bjorck
    """

    ALIASES = ("!bjorck",)
    ALLOW_ARGS = False

    # Resolved relative to this file, not the process's current working
    # directory - a bare "aijamatto.txt" only worked by coincidence of the
    # bot always being run from within src/ (see README's "cd src" run
    # instructions), not because it was actually anchored anywhere.
    LINES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aijamatto.txt")

    def __init__(self):
        # Read once at startup rather than on every !bjorck call - this
        # file is ~36MB/100k+ lines, so re-reading it from disk (and
        # rebuilding the whole list) on every single invocation was real,
        # unnecessary I/O and memory churn, not just a theoretical
        # inefficiency. A failure here (e.g. the file got moved) doesn't
        # crash the whole bot at startup - same "one broken command
        # shouldn't take down everything else" principle as WeatherCommand
        # - execute() just reports it instead.
        try:
            with open(self.LINES_FILE, encoding="utf-8") as f:
                self._lines = f.readlines()
        except OSError as e:
            print(f"AijaMatto: failed to load {self.LINES_FILE}: {e}")
            self._lines = []

    def execute(self, args=None):
        if not self._lines:
            return "Error: aijamatto.txt could not be loaded."
        return random.choice(self._lines)
