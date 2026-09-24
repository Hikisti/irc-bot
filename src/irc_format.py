"""Small formatting helpers shared across commands that print a signed
price/value change to IRC - currently StockCommand and CryptoCommand,
which both used to define this exact same snippet independently."""

GREEN = "\x0309"
RED = "\x0304"


def signed_change_color(change: float) -> str:
    """mIRC color code for a signed change: green at/above zero, red
    below it - matches the convention already used for stock/crypto
    price changes before this was shared."""
    return GREEN if change >= 0 else RED
