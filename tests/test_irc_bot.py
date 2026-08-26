import threading
import time
from unittest.mock import MagicMock

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
