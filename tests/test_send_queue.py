import threading
import time
from unittest.mock import MagicMock

from irc_bot import IrcBot
from send_queue import PacedSender, SendPacer

GOAL_LINE = "PRIVMSG #nhl.fi :" + "x" * 115  # 132 bytes with CRLF, like a GOAL line: 3 s


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class FastPacer(SendPacer):
    """The real rules scaled to hundredths of a second, so the writer thread can be tested in real time."""
    BASE_COST_SECONDS = 0.02
    BYTES_PER_EXTRA_SECOND = 10**9
    MAX_AHEAD_SECONDS = 0.04


class StalledPacer(SendPacer):
    """Lets one line out, then holds the next for a while: a queue that is visibly waiting."""
    BASE_COST_SECONDS = 0.3
    MAX_AHEAD_SECONDS = 0


def send_times(lines, pacer, start=0.0):
    """When each line of a burst handed over at `start` goes out, by the pacer's rule."""
    now, times = start, []
    for line in lines:
        now += pacer.delay(line, now)
        pacer.charge(line, now)
        times.append(now)
    return times


class TestSendPacer:
    def test_cost_is_two_seconds_plus_one_per_120_bytes_crlf_included(self):
        pacer = SendPacer()
        assert pacer.cost("PONG :x") == 2
        assert pacer.cost("x" * 117) == 2
        assert pacer.cost("x" * 118) == 3
        assert pacer.cost(GOAL_LINE) == 3
        assert pacer.cost("x" * 400) == 5

    def test_bytes_not_characters_are_counted(self):
        assert SendPacer().cost("ä" * 59) == 3  # 118 bytes + CRLF

    def test_a_single_line_goes_out_at_once(self):
        assert send_times([GOAL_LINE], SendPacer()) == [0.0]

    def test_a_burst_of_ten_is_spread_as_the_server_charges(self):
        # Two lines fit within 8 s of charge; after that one goes out every 3 s, as the server reads them.
        assert send_times([GOAL_LINE] * 10, SendPacer()) == [0, 0, 1, 4, 7, 10, 13, 16, 19, 22]

    def test_long_lines_wait_longer(self):
        long_line = "x" * 400  # 5 s each
        assert send_times([long_line] * 4, SendPacer()) == [0, 2, 7, 12]

    def test_what_leaves_at_once_stays_under_the_servers_receive_queue(self):
        # The charge is checked with the line included: three long titles at once came to ~1.1 KB, over the
        # server's 1024-byte queue, when it was checked before the line (security audit of #53).
        for sizes in ([336, 336, 380], [216, 380, 380], [100] * 6, [380] * 3):
            lines = ["PRIVMSG #nakkimuusi :" + "x" * n for n in sizes]
            times = send_times(lines, SendPacer())
            burst = sum(len(line) + 2 for line, t in zip(lines, times) if t == 0)
            assert burst <= 1024 - 300, (sizes, times, burst)

    def test_a_line_on_its_own_is_never_held_however_long(self):
        assert SendPacer().delay("x" * 2000, 0.0) == 0

    def test_the_charge_runs_down_with_the_clock(self):
        pacer = SendPacer()
        send_times([GOAL_LINE] * 2, pacer)  # charged 6 s ahead
        assert pacer.delay(GOAL_LINE, 0.5) == 0.5
        assert pacer.delay(GOAL_LINE, 20.0) == 0
        assert send_times([GOAL_LINE] * 2, pacer, start=20.0) == [20, 20]

    def test_an_unencodable_line_is_charged_without_raising(self):
        assert SendPacer().cost("x\ud800") == 2


class TestPacedSender:
    def test_a_line_is_written_at_once_from_the_callers_thread(self):
        written = []
        sender = PacedSender(written.append)
        sender.send("PRIVMSG #chan :hello")
        assert written == ["PRIVMSG #chan :hello"]
        assert sender._writer is None

    def test_a_burst_goes_out_whole_and_in_order(self):
        written = []
        sender = PacedSender(written.append, pacer=FastPacer())
        lines = [f"PRIVMSG #chan :{i}" for i in range(10)]
        for line in lines:
            sender.send(line)
        assert sender.pending() > 0
        assert wait_until(lambda: len(written) == 10)
        assert written == lines

    def test_a_burst_takes_as_long_as_its_charge(self):
        stamps = []
        sender = PacedSender(lambda line: stamps.append(time.monotonic()), pacer=FastPacer())
        for i in range(10):
            sender.send(f"PRIVMSG #chan :{i}")
        assert wait_until(lambda: len(stamps) == 10)
        # 10 lines at 0.02 s each, 0.04 s of it allowed up front: at least 0.14 s from first to last.
        assert stamps[-1] - stamps[0] >= 0.13

    def test_lines_from_many_threads_are_each_written_once(self):
        written = []
        sender = PacedSender(written.append, pacer=FastPacer())
        threads = [
            threading.Thread(target=lambda n=n: [sender.send(f"PRIVMSG #c{n} :{i}") for i in range(5)])
            for n in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)
        assert wait_until(lambda: len(written) == 20)
        assert sorted(written) == sorted(f"PRIVMSG #c{n} :{i}" for n in range(4) for i in range(5))
        for n in range(4):  # each thread's own lines keep their order
            assert [w for w in written if w.startswith(f"PRIVMSG #c{n} ")] == [f"PRIVMSG #c{n} :{i}" for i in range(5)]

    def test_send_now_goes_ahead_of_the_queue(self):
        written = []
        sender = PacedSender(written.append, pacer=StalledPacer())
        sender.send("PRIVMSG #chan :first")
        sender.send("PRIVMSG #chan :second")
        assert sender.pending() == 1
        sender.send_now("PONG :irc.example.net")
        assert written == ["PRIVMSG #chan :first", "PONG :irc.example.net"]
        assert wait_until(lambda: len(written) == 3)
        assert written[2] == "PRIVMSG #chan :second"

    def test_send_now_is_charged_like_any_line(self):
        written = []
        sender = PacedSender(written.append, pacer=StalledPacer())
        sender.send_now("JOIN #chan")
        sender.send("PRIVMSG #chan :hello")
        assert written == ["JOIN #chan"]
        assert wait_until(lambda: len(written) == 2)

    def test_a_failed_write_does_not_stop_the_lines_behind_it(self, capsys):
        written = []

        def write(line):
            if line.endswith(":1"):
                raise OSError("broken pipe")
            written.append(line)

        sender = PacedSender(write, pacer=FastPacer())
        for i in range(6):
            sender.send(f"PRIVMSG #chan :{i}")
        assert wait_until(lambda: len(written) == 5)
        assert written == [f"PRIVMSG #chan :{i}" for i in (0, 2, 3, 4, 5)]
        assert "Failed to send message: broken pipe" in capsys.readouterr().out


    def test_an_unencodable_queued_line_does_not_stop_the_lines_behind_it(self, capsys):
        # A feed can carry a lone surrogate; its encode error once ended the writer thread for good.
        written = []

        def write(line):
            line.encode("utf-8")
            written.append(line)

        sender = PacedSender(write, pacer=FastPacer())
        lines = ["PRIVMSG #t :0", "PRIVMSG #t :1", "PRIVMSG #t :bad\ud800", "PRIVMSG #t :after"]
        for line in lines:
            sender.send(line)
        assert wait_until(lambda: written[-1:] == ["PRIVMSG #t :after"])
        assert written == ["PRIVMSG #t :0", "PRIVMSG #t :1", "PRIVMSG #t :after"]
        assert "Failed to send message" in capsys.readouterr().out

    def test_a_dead_writer_is_replaced_by_the_next_queued_line(self):
        written = []
        sender = PacedSender(written.append, pacer=StalledPacer())
        sender.send("PRIVMSG #t :first")
        sender._writer = threading.Thread(target=lambda: None)  # a writer that has already ended
        sender._writer.start()
        sender._writer.join()
        sender.send("PRIVMSG #t :second")
        assert wait_until(lambda: len(written) == 2)


class SlowPacer(SendPacer):
    """One line every 0.05 s once a line is out: a queue that drains slowly, in real time."""
    BASE_COST_SECONDS = 0.05
    BYTES_PER_EXTRA_SECOND = 10**9
    MAX_AHEAD_SECONDS = 0


class TestMaxWait:
    def test_lines_that_waited_too_long_are_dropped_and_the_rest_go_out(self, capsys):
        written = []
        sender = PacedSender(written.append, pacer=SlowPacer())
        for i in range(20):
            sender.send(f"PRIVMSG #links :title {i}", max_wait=0.12)
        sender.send("PRIVMSG #nhl.fi :GOAL: Reds 1-0")
        assert wait_until(lambda: written[-1:] == ["PRIVMSG #nhl.fi :GOAL: Reds 1-0"])
        titles = [w for w in written if w.startswith("PRIVMSG #links")]
        assert 2 <= len(titles) <= 6  # the first at once, a few within 0.12 s, the rest dropped
        assert titles == [f"PRIVMSG #links :title {i}" for i in range(len(titles))]
        dropped = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("Send queue: dropped")]
        assert len(dropped) == 20 - len(titles)
        assert dropped[0].startswith("Send queue: dropped PRIVMSG #links after 0 s in the queue")

    def test_a_line_without_max_wait_is_never_dropped(self):
        written = []
        sender = PacedSender(written.append, pacer=SlowPacer())
        for i in range(6):
            sender.send(f"PRIVMSG #nhl.fi :GOAL: {i}")
        assert wait_until(lambda: len(written) == 6)

    def test_a_line_written_at_once_is_not_affected(self):
        written = []
        PacedSender(written.append).send("PRIVMSG #links :title", max_wait=0)
        assert written == ["PRIVMSG #links :title"]


class TestBotPacing:
    def test_send_message_passes_max_wait_to_the_queue(self):
        bot = IrcBot()
        bot._sender = MagicMock()
        bot.send_message("#chan", "Title", max_wait=60)
        bot._sender.send.assert_called_once_with("PRIVMSG #chan :Title", max_wait=60)

    def test_send_message_replaces_a_lone_surrogate(self):
        bot = IrcBot()
        bot.sock = MagicMock()
        bot.send_message("#chan", "Kärpät\ud83d 2-1")
        bot.sock.sendall.assert_called_once_with("PRIVMSG #chan :Kärpät? 2-1\r\n".encode())

    def test_a_burst_of_channel_messages_reaches_the_socket_and_the_journal_once_each(self, capsys):
        bot = IrcBot()
        bot.sock = MagicMock()
        bot._sender._pacer = FastPacer()
        for i in range(10):
            bot.send_message("#chan", f"GOAL: {i}")
        assert wait_until(lambda: bot.sock.sendall.call_count == 10)
        sent = [c.args[0] for c in bot.sock.sendall.call_args_list]
        assert sent == [f"PRIVMSG #chan :GOAL: {i}\r\n".encode() for i in range(10)]
        journal = [line for line in capsys.readouterr().out.splitlines() if line.startswith("> ")]
        assert journal == [f"> PRIVMSG #chan :GOAL: {i}" for i in range(10)]

    def test_pong_is_not_held_behind_a_queued_burst(self):
        bot = IrcBot()
        bot.sock = MagicMock()
        bot._sender._pacer = StalledPacer()
        bot.send_message("#chan", "one")
        bot.send_message("#chan", "two")
        bot.pong("PING :irc.example.net")
        sent = [c.args[0] for c in bot.sock.sendall.call_args_list]
        assert sent[:2] == [b"PRIVMSG #chan :one\r\n", b"PONG :irc.example.net\r\n"]
        assert wait_until(lambda: bot.sock.sendall.call_count == 3)
