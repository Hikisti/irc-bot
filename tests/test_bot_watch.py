"""tools/bot_watch.py: the decision (pure) and the IRC talk against a scripted fake server."""
import datetime
import inspect
import json
import os
import socket
import sys

import pytest

TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import bot_watch  # noqa: E402
from bot_watch import ALERT, MISSING, OK, ONLINE, QUIET, UNREACHABLE  # noqa: E402

NOW = datetime.datetime(2026, 11, 23, 1, 7, tzinfo=datetime.timezone.utc)


def test_server_and_nick_match_the_bot():
    from irc_bot import IrcBot
    params = inspect.signature(IrcBot.__init__).parameters
    assert (bot_watch.SERVER, bot_watch.PORT, bot_watch.BOT_NICK) == (
        params["server"].default, params["port"].default, params["nickname"].default)


@pytest.mark.parametrize("first, second, verdict", [
    (True, None, ONLINE),
    (False, True, ONLINE),       # back within the wait: a restart, no alarm
    (None, True, ONLINE),
    (False, False, MISSING),
    (False, None, UNREACHABLE),  # gone once, second check never answered: not proven missing
    (None, None, UNREACHABLE),
])
def test_combine(first, second, verdict):
    assert bot_watch.combine(first, second) == verdict


def test_first_missing_run_alerts_and_records_the_outage():
    d = bot_watch.decide(MISSING, {}, NOW)
    assert d.action == ALERT
    assert d.state == {"problem": MISSING, "since": "2026-11-23 01:07 UTC"}
    assert "KukistiBot is not on the network (01:07 UTC" in d.message


def test_a_later_missing_run_stays_quiet_and_keeps_the_start():
    previous = {"problem": MISSING, "since": "2026-11-23 01:07 UTC"}
    d = bot_watch.decide(MISSING, previous, NOW + datetime.timedelta(hours=2))
    assert d.action == QUIET
    assert d.state == previous


def test_back_online_clears_the_state_so_the_next_outage_alerts():
    d = bot_watch.decide(ONLINE, {"problem": MISSING, "since": "x"}, NOW)
    assert (d.action, d.state) == (OK, {})
    assert "back after being missing since x" in d.message
    assert bot_watch.decide(MISSING, d.state, NOW).action == ALERT


def test_unreachable_is_its_own_outage():
    d = bot_watch.decide(UNREACHABLE, {"problem": MISSING, "since": "x"}, NOW)
    assert d.action == ALERT
    assert "could not reach irc.quakenet.org" in d.message
    assert bot_watch.decide(UNREACHABLE, d.state, NOW).action == QUIET


@pytest.mark.parametrize("junk", [None, [], "missing", 3])
def test_junk_state_counts_as_none(junk):
    assert bot_watch.decide(MISSING, junk, NOW).action == ALERT


@pytest.mark.parametrize("content", [None, "", "not json", "[1, 2]"])
def test_load_state_never_raises(tmp_path, content):
    path = tmp_path / "state.json"
    if content is not None:
        path.write_text(content)
    assert bot_watch.load_state(path) == {}


def test_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    bot_watch.save_state(path, {"problem": MISSING, "since": "x"})
    assert bot_watch.load_state(path) == {"problem": MISSING, "since": "x"}


class FakeServer:
    """A scripted IRC server. `online` is the answer to each ISON in turn; the clock moves a second per
    read, so every deadline in the session ends even if a reply never comes."""

    def __init__(self, online=(True,), owner_present=True, refuse=False, welcome=True):
        self.online = list(online)
        self.owner_present = owner_present
        self.refuse = refuse
        self.welcome = welcome
        self.sent = []
        self.connections = 0
        self.now = 0.0
        self._inbox = b""

    def clock(self):
        return self.now

    def connect(self, address, timeout=None):
        self.connections += 1
        if self.refuse:
            raise ConnectionRefusedError("refused")
        self._inbox += b"PING :123\r\n"
        return self

    def sendall(self, data):
        line = data.decode().rstrip("\r\n")
        self.sent.append(line)
        cmd, _, rest = line.partition(" ")
        if cmd == "USER" and self.welcome:
            self._reply(":srv 001 me :Welcome")
        elif cmd == "ISON":
            if self.online:
                self._reply(f":srv 303 me :{'KukistiBot ' if self.online.pop(0) else ''}")
        elif cmd == "PRIVMSG" and not self.owner_present:
            self._reply(f":srv 401 me {rest.split()[0]} :No such nick")
        elif cmd == "PING":
            self._reply(f":srv PONG srv {rest}")

    def _reply(self, line):
        self._inbox += (line + "\r\n").encode()

    def recv(self, size):
        self.now += 1
        if not self._inbox:
            raise socket.timeout()
        data, self._inbox = self._inbox, b""
        return data

    def close(self):
        pass


def session_for(server):
    return bot_watch.IRCSession(connect=server.connect, clock=server.clock, log=lambda _: None)


def test_session_answers_ping_registers_and_reads_ison():
    server = FakeServer(online=[True])
    session = session_for(server)
    assert session.register()
    assert "PONG :123" in server.sent
    assert session.ison("KukistiBot") is True


def test_session_without_welcome_gives_up_at_the_deadline():
    assert session_for(FakeServer(welcome=False)).register() is False


def test_ison_with_no_reply_is_none():
    server = FakeServer(online=[])
    session = session_for(server)
    session.register()
    assert session.ison("KukistiBot") is None


def test_check_waits_and_asks_again_only_when_missing():
    server = FakeServer(online=[False, True])
    assert bot_watch.check(session_for(server), 180) == (False, True)
    assert server.now >= 180
    assert bot_watch.check(session_for(FakeServer(online=[True])), 180) == (True, None)


def test_check_reconnects_once_when_the_second_check_gets_no_answer():
    server = FakeServer(online=[False])
    session = session_for(server)
    original = server.sendall
    calls = {"ison": 0}

    def flaky(data):
        if data.startswith(b"ISON"):
            calls["ison"] += 1
            if calls["ison"] == 3:   # the one after the reconnect
                server.online.append(False)
        original(data)

    server.sendall = flaky
    assert bot_watch.check(session, 5) == (False, False)
    assert server.connections == 2


def run_main(tmp_path, server, monkeypatch, owner="owner", state=None):
    path = tmp_path / "state.json"
    if state is not None:
        path.write_text(json.dumps(state))
    if owner:
        monkeypatch.setenv("WATCH_NOTIFY_NICK", owner)
    else:
        monkeypatch.delenv("WATCH_NOTIFY_NICK", raising=False)
    code = bot_watch.main(["--state", str(path), "--wait", "5"], session=session_for(server), now=NOW)
    return code, json.loads(path.read_text())


def test_main_bot_present(tmp_path, monkeypatch):
    server = FakeServer(online=[True])
    assert run_main(tmp_path, server, monkeypatch) == (0, {})
    assert not any(s.startswith("PRIVMSG") for s in server.sent)


def test_main_first_missing_run_messages_the_owner_and_fails(tmp_path, monkeypatch, capsys):
    server = FakeServer(online=[False, False])
    code, state = run_main(tmp_path, server, monkeypatch)
    assert code == 1
    assert state["problem"] == MISSING
    assert [s for s in server.sent if s.startswith("PRIVMSG")] == [
        "PRIVMSG owner :KukistiBot is not on the network (01:07 UTC, checked twice 5 s apart)"]
    assert "private message sent to the owner" in capsys.readouterr().out


def test_main_owner_offline_is_said(tmp_path, monkeypatch, capsys):
    run_main(tmp_path, FakeServer(online=[False, False], owner_present=False), monkeypatch)
    assert "the owner is not on the network" in capsys.readouterr().out


def test_main_already_reported_sends_nothing_and_passes(tmp_path, monkeypatch, capsys):
    server = FakeServer(online=[False, False])
    previous = {"problem": MISSING, "since": "2026-11-23 00:37 UTC"}
    assert run_main(tmp_path, server, monkeypatch, state=previous) == (0, previous)
    assert not any(s.startswith("PRIVMSG") for s in server.sent)
    assert "::warning::" in capsys.readouterr().out


def test_main_without_owner_still_fails(tmp_path, monkeypatch, capsys):
    code, _ = run_main(tmp_path, FakeServer(online=[False, False]), monkeypatch, owner="")
    assert code == 1
    assert "WATCH_NOTIFY_NICK is not set" in capsys.readouterr().out


def test_main_unreachable_fails_without_a_message(tmp_path, monkeypatch):
    server = FakeServer(refuse=True)
    code, state = run_main(tmp_path, server, monkeypatch)
    assert (code, state["problem"]) == (1, UNREACHABLE)
    assert server.sent == []
