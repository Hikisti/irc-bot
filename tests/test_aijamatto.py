from unittest.mock import patch

from aijamatto import AijaMattoCommand


class TestInit:
    def test_loads_lines_from_file_once(self, tmp_path):
        lines_file = tmp_path / "aijamatto.txt"
        lines_file.write_text("first line\nsecond line\nthird line\n", encoding="utf-8")

        with patch.object(AijaMattoCommand, "LINES_FILE", str(lines_file)):
            command = AijaMattoCommand()

        # Regression test: readlines() keeps each line's trailing "\n" -
        # without stripping it here, every !bjorck reply ended with a
        # visible trailing space (IrcBot.send_message() only replaces
        # "\n"/"\r" with a space, it doesn't strip the result).
        assert command._lines == ["first line", "second line", "third line"]

    def test_missing_file_does_not_raise(self, tmp_path):
        # A broken/missing data file shouldn't crash the whole bot at
        # startup (command_handler.py constructs every command's __init__
        # unguarded) - same principle as WeatherCommand not raising when
        # its API key is unset.
        missing_path = str(tmp_path / "does-not-exist.txt")

        with patch.object(AijaMattoCommand, "LINES_FILE", missing_path):
            command = AijaMattoCommand()  # must not raise

        assert command._lines == []


class TestExecute:
    def test_returns_a_random_line(self, tmp_path):
        lines_file = tmp_path / "aijamatto.txt"
        lines_file.write_text("only line\n", encoding="utf-8")

        with patch.object(AijaMattoCommand, "LINES_FILE", str(lines_file)):
            command = AijaMattoCommand()

        assert command.execute() == "only line"

    def test_picks_from_all_loaded_lines(self, tmp_path):
        lines_file = tmp_path / "aijamatto.txt"
        lines_file.write_text("a\nb\nc\n", encoding="utf-8")

        with patch.object(AijaMattoCommand, "LINES_FILE", str(lines_file)):
            command = AijaMattoCommand()

        results = {command.execute() for _ in range(50)}
        assert results == {"a", "b", "c"}

    def test_does_not_reread_the_file_on_execute(self, tmp_path):
        lines_file = tmp_path / "aijamatto.txt"
        lines_file.write_text("line one\n", encoding="utf-8")

        with patch.object(AijaMattoCommand, "LINES_FILE", str(lines_file)):
            command = AijaMattoCommand()

        with patch("builtins.open") as mock_open:
            command.execute()

        mock_open.assert_not_called()

    def test_error_message_when_file_failed_to_load(self, tmp_path):
        missing_path = str(tmp_path / "does-not-exist.txt")

        with patch.object(AijaMattoCommand, "LINES_FILE", missing_path):
            command = AijaMattoCommand()

        assert command.execute() == "Error: aijamatto.txt could not be loaded."

    def test_ignores_args(self, tmp_path):
        lines_file = tmp_path / "aijamatto.txt"
        lines_file.write_text("only line\n", encoding="utf-8")

        with patch.object(AijaMattoCommand, "LINES_FILE", str(lines_file)):
            command = AijaMattoCommand()

        assert command.execute("some ignored args") == "only line"
