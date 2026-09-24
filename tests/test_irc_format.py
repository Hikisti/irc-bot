from src.irc_format import GREEN, RED, signed_change_color


class TestSignedChangeColor:
    def test_positive_change_is_green(self):
        assert signed_change_color(1.5) == GREEN

    def test_negative_change_is_red(self):
        assert signed_change_color(-1.5) == RED

    def test_zero_is_green(self):
        # Matches the pre-existing stock.py/crypto.py convention this was
        # extracted from: >= 0, not > 0, so an unchanged price shows green.
        assert signed_change_color(0) == GREEN
