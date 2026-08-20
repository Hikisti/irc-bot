import os
import sys

# command_handler.py imports its sibling modules with bare names
# (e.g. "from electricity import ElectricityCommand") rather than
# "from src.electricity import ...", which only resolves when the src/
# directory itself is on sys.path (as it is when the bot is run from
# within src/). Add it here so tests can import command_handler too.
SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
