import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from irc_bot import IrcBot


@pytest.fixture
def bot():
    b = IrcBot()
    b.sock = MagicMock()
    return b


class TestSendRaw:
    def test_sends_the_message_with_crlf(self, bot):
        bot.send_raw("PRIVMSG #chan :hello")
        bot.sock.sendall.assert_called_once_with(b"PRIVMSG #chan :hello\r\n")

    def test_uses_sendall_not_send(self, bot):
        # sendall() guarantees the whole line goes out in one call rather
        # than potentially partial-writing, unlike send().
        bot.send_raw("PRIVMSG #chan :hello")
        bot.sock.send.assert_not_called()
        bot.sock.sendall.assert_called_once()

    def test_socket_error_does_not_raise(self, bot):
        bot.sock.sendall.side_effect = OSError("broken pipe")
        bot.send_raw("PRIVMSG #chan :hello")  # must not raise

    def test_concurrent_calls_are_serialized_by_the_lock(self, bot):
        # Makes each sendall() call take a moment, so overlapping calls
        # would interleave if the lock weren't actually held around them.
        order = []
        lock_held_concurrently = threading.Event()
        currently_inside = threading.Event()

        def slow_sendall(data):
            if currently_inside.is_set():
                lock_held_concurrently.set()  # a second thread got in - bug
            currently_inside.set()
            order.append(data)
            time.sleep(0.05)
            currently_inside.clear()

        bot.sock.sendall.side_effect = slow_sendall

        threads = [
            threading.Thread(target=bot.send_raw, args=(f"PRIVMSG #chan :{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)

        assert not lock_held_concurrently.is_set()
        assert len(order) == 5


class TestSendMessage:
    def test_delegates_to_send_raw_with_privmsg_format(self, bot):
        bot.send_message("#chan", "hello world")
        bot.sock.sendall.assert_called_once_with(b"PRIVMSG #chan :hello world\r\n")

    def test_strips_newlines_from_the_message(self, bot):
        bot.send_message("#chan", "line one\nline two\rline three")
        sent = bot.sock.sendall.call_args[0][0]
        assert b"\n" not in sent[:-2]  # only the trailing \r\n terminator
        assert b"line one line two line three" in sent


class TestListen:
    """listen()'s two ways of ending are both logged with a consistent
    "DISCONNECTED: ..." prefix, so a future incident's cause is one grep
    away instead of having to infer it from what's absent (see the real
    investigation this was built from: a network-side event that never
    touched the bot's own connection at all, confirmed only after several
    rounds of manual log correlation)."""

    def test_server_eof_is_logged_as_disconnected(self, bot, capsys):
        bot.running = True
        bot.sock.recv.return_value = b""  # server closed the connection

        bot.listen()  # loop exits on its own once running flips False

        assert bot.running is False
        captured = capsys.readouterr()
        assert "DISCONNECTED: server closed the connection (EOF)" in captured.out

    def test_recv_exception_is_logged_as_disconnected_with_traceback(self, bot, capsys):
        bot.running = True
        bot.sock.recv.side_effect = OSError("connection reset by peer")

        bot.listen()

        assert bot.running is False
        captured = capsys.readouterr()
        assert "DISCONNECTED: error in listen loop: connection reset by peer" in captured.out
        # traceback.print_exc() writes to stderr, not stdout.
        assert "Traceback" in captured.err  # not just str(e) - see class docstring
        assert "OSError" in captured.err

    def test_healthy_recv_does_not_log_disconnected(self, bot, capsys):
        # A single healthy cycle must not print anything implying a
        # disconnect - "DISCONNECTED" is reserved for the two real ways
        # the loop actually ends.
        call_count = 0

        def recv_then_stop(bufsize):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return b":irc.example.net PING :abc\r\n"
            bot.running = False
            return b""

        bot.running = True
        bot.sock.recv.side_effect = recv_then_stop

        bot.listen()

        captured = capsys.readouterr()
        assert "DISCONNECTED: error" not in captured.out


class TestListenDispatch:
    """Regression coverage for dispatching on the parsed command (parts[1]
    of a ":prefix COMMAND ..." line) rather than a substring match against
    the whole line - the previous "001" in line / "PRIVMSG" in line checks
    would misfire on ordinary chat text containing those words."""

    def _stop_after_one_line(self, bot, line_bytes):
        call_count = 0

        def recv_then_stop(bufsize):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return line_bytes
            bot.running = False
            return b""

        bot.running = True
        bot.sock.recv.side_effect = recv_then_stop

    def test_welcome_reply_triggers_join(self, bot):
        self._stop_after_one_line(bot, b":irc.example.net 001 KukistiBot :Welcome\r\n")
        with patch.object(bot, "join_channels") as mock_join:
            bot.listen()
        mock_join.assert_called_once()

    def test_chat_message_containing_001_does_not_trigger_join(self, bot):
        # Regression test: a real report of chat text containing "001"
        # (e.g. "order 001 arrived") getting misdispatched as the server's
        # welcome reply because of a substring match on " 001 ".
        line = b":alice!a@host PRIVMSG #chan :order 001 arrived\r\n"
        self._stop_after_one_line(bot, line)
        with patch.object(bot, "join_channels") as mock_join, \
             patch.object(bot, "process_message") as mock_process:
            bot.listen()
        mock_join.assert_not_called()
        mock_process.assert_called_once()

    def test_privmsg_dispatches_to_process_message(self, bot):
        line = b":alice!a@host PRIVMSG #chan :hello\r\n"
        self._stop_after_one_line(bot, line)
        with patch.object(bot, "process_message") as mock_process:
            bot.listen()
        mock_process.assert_called_once_with(line.decode().strip())


class TestListenBuffering:
    """Regression coverage for lines split across two recv() calls (e.g.
    at the 2048-byte boundary) - a naive per-call split("\n") would
    process each half as its own broken line."""

    def test_line_split_across_two_recv_calls_is_reassembled(self, bot):
        first_half, second_half = b":alice!a@host PRIVMSG #ch", b"an :hello\r\n"
        call_count = 0

        def recv_then_stop(bufsize):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_half
            if call_count == 2:
                return second_half
            bot.running = False
            return b""

        bot.running = True
        bot.sock.recv.side_effect = recv_then_stop

        with patch.object(bot, "process_message") as mock_process:
            bot.listen()

        mock_process.assert_called_once_with(":alice!a@host PRIVMSG #chan :hello")

    def test_multiple_complete_lines_in_one_recv_are_all_processed(self, bot):
        chunk = b":a!a@h PRIVMSG #c :one\r\n:a!a@h PRIVMSG #c :two\r\n"
        call_count = 0

        def recv_then_stop(bufsize):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return chunk
            bot.running = False
            return b""

        bot.running = True
        bot.sock.recv.side_effect = recv_then_stop

        with patch.object(bot, "process_message") as mock_process:
            bot.listen()

        assert mock_process.call_count == 2


class TestConnect:
    def test_prints_a_startup_banner_with_the_pid(self, bot, capsys):
        bot.sock.connect.side_effect = OSError("unreachable")  # short-circuits before the blocking loop

        bot.connect()

        captured = capsys.readouterr()
        assert f"BOT STARTED pid={os.getpid()}" in captured.out

    def test_successful_connect_logs_connected(self, bot, capsys):
        # Patch time.sleep so the real 3s wait + "while self.running"
        # loop can't block the test - the first sleep() call (before
        # join_channels()) flips running off, same as a real shutdown
        # would, so the loop below it never actually spins.
        with patch("irc_bot.time.sleep", side_effect=lambda _: setattr(bot, "running", False)), \
             patch("irc_bot.threading.Thread"):
            bot.connect()

        captured = capsys.readouterr()
        assert f"CONNECTED: {bot.server}:{bot.port} as {bot.nickname}" in captured.out

    def test_failed_connect_logs_connect_failed_with_traceback(self, bot, capsys):
        bot.sock.connect.side_effect = OSError("unreachable")

        bot.connect()

        captured = capsys.readouterr()
        assert "CONNECT FAILED: unreachable" in captured.out
        assert "Traceback" in captured.err


class TestStop:
    def test_logs_disconnected_for_a_requested_shutdown(self, bot, capsys):
        bot.stop()

        captured = capsys.readouterr()
        assert "DISCONNECTED: stop() called" in captured.out
