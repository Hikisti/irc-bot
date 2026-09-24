from irc_format import BOLD, RESET, LIGHT_GREEN, RED, prefix, signed_change_color


class TestSignedChangeColor:
    def test_positive_change_is_light_green(self):
        assert signed_change_color(1.5) == LIGHT_GREEN

    def test_negative_change_is_red(self):
        assert signed_change_color(-1.5) == RED

    def test_zero_is_light_green(self):
        # Matches the pre-existing stock.py/crypto.py convention this was
        # extracted from: >= 0, not > 0, so an unchanged price shows green.
        assert signed_change_color(0) == LIGHT_GREEN


class TestPrefix:
    def test_wraps_label_in_bold_and_color_then_resets(self):
        assert prefix("GOAL:", "\x0303") == f"{BOLD}\x0303GOAL:{RESET}"
