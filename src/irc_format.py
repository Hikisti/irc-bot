"""mIRC formatting control codes and small helpers built on them, shared
across commands that print bold/colored text to IRC - previously
duplicated as class attributes on LiigaCommand and PesisCommand (BOLD,
COLOR_RESET, GREEN, ORANGE, PURPLE), and as raw \\x02/\\x03 escapes
inline in stock.py/crypto.py.

Named after mIRC's own numbered color palette (00-15) rather than
generic color words, since e.g. a plain "GREEN" name would otherwise
silently mean two different codes depending on which command's own
convention you copied from - which is exactly what happened before this
existed: LiigaCommand/PesisCommand's own "GREEN" was \\x0303, while this
module's was \\x0309 (light green)."""

BOLD = "\x02"
RESET = "\x0F"

GREEN = "\x0303"
RED = "\x0304"
PURPLE = "\x0306"
ORANGE = "\x0307"
LIGHT_GREEN = "\x0309"


def prefix(label: str, color: str) -> str:
    """A bold, colored label like "GOAL:" or "FINAL:", reset back to
    plain text right after it - the shared shape behind every live
    tracker's own LABEL: prefix (LiigaCommand's GOAL:/FINAL:,
    PesisCommand's RUN:/FINAL:/JAKSO:)."""
    return f"{BOLD}{color}{label}{RESET}"


def signed_change_color(change: float) -> str:
    """mIRC color code for a signed change: light green at/above zero,
    red below it - matches the convention already used for stock/crypto
    price changes before this was shared."""
    return LIGHT_GREEN if change >= 0 else RED
