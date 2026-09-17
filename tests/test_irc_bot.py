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
