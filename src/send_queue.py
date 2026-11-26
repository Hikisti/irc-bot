"""Paces the bot's outgoing lines the way the IRC server counts them, so a burst never reaches its flood limit.

The server (QuakeNet, an ircu2 fork; ircu2's defaults, #53) charges each line a client sends 2 s plus 1 s per
120 bytes, and reads a client's lines only while that charge runs less than 10 s ahead of its clock. Lines it
is not reading yet wait in a small receive queue (1024 bytes), and a client that overfills it is cut off with
"Excess Flood", which restarts the bot and stops every tracker. The bot keeps the same account and sends a
line only if its charge, that line included, stays within MAX_AHEAD_SECONDS, a margin under the server's 10.
The server then never holds a backlog, and what leaves at once is at most 8 s of charge, about 700 bytes,
under that queue even if it all arrives in one read. A line on its own goes out at once; only a burst is
spread out.
"""
import collections
import threading
import time


class SendPacer:
    """The decision alone: how long a line must wait, given what has been sent. No clock, no socket."""

    BASE_COST_SECONDS = 2
    BYTES_PER_EXTRA_SECOND = 120
    MAX_AHEAD_SECONDS = 8

    def __init__(self):
        self.charged_until = 0.0

    def cost(self, line):
        """What the server charges for a line, its CRLF included."""
        return self.BASE_COST_SECONDS + (len(line.encode("utf-8", "replace")) + 2) // self.BYTES_PER_EXTRA_SECOND

    def delay(self, line, now):
        """Seconds until `line` may go out (0 = now). A line is never held when nothing is charged ahead."""
        if self.charged_until <= now:
            return 0.0
        return max(0.0, self.charged_until + self.cost(line) - self.MAX_AHEAD_SECONDS - now)

    def charge(self, line, now):
        self.charged_until = max(self.charged_until, now) + self.cost(line)


class PacedSender:
    """Sends lines through `write` in the order given, paced by a SendPacer.

    `send()` is for channel messages: written at once from the caller's thread when nothing is waiting and
    the pacer allows it, otherwise queued for a writer thread. A line given `max_wait` is dropped instead of
    sent once it has waited that long: a late link title is worth nothing, and a long run of them must not
    hold up the trackers' lines behind it in the one queue all channels share. `send_now()` is for what must not wait
    (PONG, PING, NICK, USER, JOIN, QUIT); it skips the queue but is charged, because the server charges it.
    One lock covers every write, so lines never interleave on the wire.
    """

    def __init__(self, write, clock=time.monotonic, pacer=None):
        self._write = write
        self._clock = clock
        self._pacer = pacer or SendPacer()
        self._queue = collections.deque()
        self._cond = threading.Condition()
        self._writer = None

    def send(self, line, max_wait=None):
        with self._cond:
            now = self._clock()
            if not self._queue and self._pacer.delay(line, now) == 0:
                self._emit(line, now)
                return
            self._queue.append((line, now, max_wait))
            self._start_writer()
            self._cond.notify()

    def send_now(self, line):
        with self._cond:
            self._emit(line, self._clock())

    def pending(self):
        with self._cond:
            return len(self._queue)

    def _emit(self, line, now):
        try:
            self._pacer.charge(line, now)
            self._write(line)
        except Exception as e:  # one bad line or failed write must not stop the lines behind it
            print(f"Failed to send message: {e}")

    def _start_writer(self):
        # Also replaces a writer that has died: without this a dead writer left every later line queued for
        # nobody, while PONG kept the connection up so nothing restarted the bot.
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._run_writer, name="send-queue", daemon=True)
            self._writer.start()

    def _run_writer(self):
        with self._cond:
            while True:
                if not self._queue:
                    self._cond.wait()
                    continue
                now = self._clock()
                line, queued_at, max_wait = self._queue[0]
                if max_wait is not None and now - queued_at > max_wait:
                    self._queue.popleft()
                    target = " ".join(line.split(" ", 2)[:2])
                    print(f"Send queue: dropped {target} after {int(now - queued_at)} s in the queue")
                    continue
                wait = self._pacer.delay(line, now)
                if wait > 0:
                    self._cond.wait(wait)
                    continue
                self._queue.popleft()
                self._emit(line, now)
