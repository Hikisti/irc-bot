"""Looks for the bot on IRC and tells the owner when it is gone, run every 30 minutes by the Bot watch
workflow on GitHub (.github/workflows/bot-watch.yml). Nothing on the server watches the bot after a
deploy: the service restarts it after every exit, so it never reaches a failed state an alert could
hang on, and the server cannot report itself being down. This runs elsewhere and asks the network.

It connects under a throwaway nick and asks ISON for the bot. If the bot is not there it asks again
after WAIT_SECONDS, so a deploy restart or the morning automatic-update restart raises no alarm.
Still missing: one private message to the owner (nick in WATCH_NOTIFY_NICK) and a failing exit, so
GitHub's failed-run notification arrives too. Once per outage: the state file carries "already
reported" from run to run (the workflow keeps it as an artifact), and later runs only warn. A
watcher that cannot reach the network says so instead of calling the bot missing.

Unlike the probes, this talks to IRC, but only to the network: it joins no channel, and its one
message goes to the owner.

    python tools/bot_watch.py --state bot_watch_state.json [--wait SECONDS]
"""
import argparse
import datetime
import json
import os
import random
import socket
import sys
import time
from dataclasses import dataclass

# The same as IrcBot's defaults in src/irc_bot.py (a test keeps them in step).
SERVER = "irc.quakenet.org"
PORT = 6667
BOT_NICK = "KukistiBot"

WAIT_SECONDS = 180
REPLY_TIMEOUT_SECONDS = 60
UTC = datetime.timezone.utc

ONLINE, MISSING, UNREACHABLE = "online", "missing", "unreachable"
ALERT, QUIET, OK = "alert", "quiet", "ok"


def combine(first, second):
    """One verdict from the two ISON checks, each True (there), False (not there) or None (no answer).
    The bot counts as missing only when both checks saw it gone."""
    if first or second:
        return ONLINE
    if first is False and second is False:
        return MISSING
    return UNREACHABLE


@dataclass
class Decision:
    action: str      # ALERT: tell the owner and fail; QUIET: already told, only warn; OK: nothing wrong
    state: dict      # what the next run reads
    message: str


def decide(verdict, previous, now, wait_seconds=WAIT_SECONDS):
    """What to do about this run's verdict, given the state the previous run left (any dict; junk
    counts as no state, so a lost state errs towards one extra alert, never towards silence)."""
    stamp = now.strftime("%H:%M UTC")
    if verdict == ONLINE:
        back = previous.get("problem") if isinstance(previous, dict) else None
        note = f" (back after being {back} since {previous.get('since')})" if back else ""
        return Decision(OK, {}, f"{BOT_NICK} is on the network{note}")
    if isinstance(previous, dict) and previous.get("problem") == verdict:
        since = previous.get("since", "?")
        return Decision(QUIET, dict(previous), f"{BOT_NICK} still {verdict} since {since}, already reported")
    state = {"problem": verdict, "since": now.strftime("%Y-%m-%d %H:%M UTC")}
    if verdict == MISSING:
        return Decision(ALERT, state, f"{BOT_NICK} is not on the network ({stamp}, checked twice "
                                      f"{wait_seconds} s apart)")
    return Decision(ALERT, state, f"the watcher could not reach {SERVER} at {stamp}, "
                                  f"so it cannot tell whether {BOT_NICK} is there")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


class IRCSession:
    """Just enough IRC for the watcher: register, ISON, one PRIVMSG. Each call returns None instead of
    raising when the connection fails or the server does not answer in time."""

    def __init__(self, connect=socket.create_connection, clock=time.monotonic, log=print):
        self._connect = connect
        self._clock = clock
        self._log = log
        self.sock = None
        self._buf = b""
        self.nick = f"kbwatch{random.randint(10000, 99999)}"

    def _send(self, line):
        self.sock.sendall((line + "\r\n").encode("utf-8"))

    def _lines(self, deadline):
        """Server lines until the deadline, answering PINGs on the way."""
        while self._clock() < deadline:
            while b"\r\n" in self._buf:
                raw, self._buf = self._buf.split(b"\r\n", 1)
                line = raw.decode("utf-8", "replace")
                if line.startswith("PING"):
                    self._send("PONG" + line[4:])
                    continue
                yield line
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                return
            self._buf += data

    def _until(self, wanted, timeout):
        """The first line whose numeric/command is in `wanted`, or None (closed, ERROR or timeout)."""
        for line in self._lines(self._clock() + timeout):
            parts = line.split()
            if parts and parts[0] == "ERROR":
                self._log(f"server closed the connection: {line}")
                return None
            if len(parts) > 1 and parts[1] in wanted:
                return line
        return None

    def register(self):
        try:
            self.sock = self._connect((SERVER, PORT), timeout=30)
            self._send(f"NICK {self.nick}")
            self._send(f"USER {self.nick} 0 * :{self.nick}")
            return self._until({"001"}, REPLY_TIMEOUT_SECONDS) is not None
        except OSError as e:
            self._log(f"could not connect to {SERVER}:{PORT}: {e}")
            return False

    def reconnect(self):
        self.close()
        self.sock, self._buf = None, b""
        return self.register()

    def ison(self, nick):
        try:
            self._send(f"ISON {nick}")
            line = self._until({"303"}, REPLY_TIMEOUT_SECONDS)
        except OSError as e:
            self._log(f"connection lost: {e}")
            return None
        if line is None:
            return None
        listed = line.split(" :", 1)[1].split() if " :" in line else []
        return nick.lower() in (n.lower() for n in listed)

    def wait(self, seconds):
        """Stay connected for `seconds`, answering PINGs."""
        try:
            for _ in self._lines(self._clock() + seconds):
                pass
        except OSError:
            pass

    def privmsg(self, target, text):
        """True when the server took the message, False when the target is not on the network (401),
        None when that could not be told."""
        token = f"bw{random.randint(10000, 99999)}"
        try:
            self._send(f"PRIVMSG {target} :{text}")
            self._send(f"PING :{token}")
            line = self._until({"401", "PONG"}, REPLY_TIMEOUT_SECONDS)
        except OSError:
            return None
        if line is None:
            return None
        return line.split()[1] != "401"

    def close(self):
        if self.sock is None:
            return
        try:
            self._send("QUIT :bot watch")
            self.sock.close()
        except OSError:
            pass


def check(session, wait_seconds):
    """Both ISON checks over one connection; reconnects once if the first connection is lost."""
    if not session.register():
        return None, None
    first = session.ison(BOT_NICK)
    if first:
        return first, None
    session.wait(wait_seconds)
    second = session.ison(BOT_NICK)
    if second is None:
        second = session.ison(BOT_NICK) if session.reconnect() else None
    return first, second


def main(argv=None, session=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", required=True, help="state file read and written between runs")
    parser.add_argument("--wait", type=int, default=WAIT_SECONDS, help="seconds between the two checks")
    args = parser.parse_args(argv)

    session = session or IRCSession()
    try:
        first, second = check(session, args.wait)
        decision = decide(combine(first, second), load_state(args.state), now or datetime.datetime.now(UTC),
                          args.wait)
        print(f"checks: {first}, {second} -> {decision.action}: {decision.message}")
        if decision.action == ALERT and combine(first, second) == MISSING:
            owner = os.environ.get("WATCH_NOTIFY_NICK", "").strip()
            if not owner:
                print("::warning::WATCH_NOTIFY_NICK is not set, no private message sent")
            else:
                delivered = session.privmsg(owner, decision.message)
                print({True: "private message sent to the owner",
                       False: "the owner is not on the network, the message was not delivered",
                       None: "could not tell whether the private message was delivered"}[delivered])
    finally:
        session.close()

    save_state(args.state, decision.state)
    if decision.action == ALERT:
        print(f"::error::{decision.message}")
        return 1
    if decision.action == QUIET:
        print(f"::warning::{decision.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
