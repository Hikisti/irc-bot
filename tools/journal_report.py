"""Report on one game night from the bot's own journal. Read-only; standard library only.

Reads `journalctl -a` text (a file, or stdin) and prints, per channel and game, the GOAL / NO GOAL
/ FINAL lines the bot posted, how long after the last goal the FINAL came, whether the attendance
arrived, and the lines the bot journals about its own decisions (a repeated goal ignored, a goal
retracted, the attendance found late, the FINAL score source). It flags what looks wrong: a
duplicate GOAL line, a goal retracted and then announced again, a NO GOAL with no reason, a FINAL
whose score differs from the last goal, a FINAL that never got its attendance, a game with goals
and no FINAL, restarts and errors.

Only the bot's own lines are used (`> PRIVMSG ...` it sent, and its own prints). Incoming chat
(`< ...`) is ignored, and host names, process ids, nicks and addresses never reach the report;
free text that is printed is scrubbed of home paths, IPv4 addresses and user@host strings.

Usage:
    sudo journalctl -u <the bot's unit> -a --since "..." --until "..." | python3 tools/journal_report.py
    python3 tools/journal_report.py journal.txt [--year 2026]
Journal lines carry no year; --year (default: this year) is only used to order a night that spans
midnight. NHL and Liiga lines are covered; the Pesis trackers' lines are not.
"""
import re
import sys
from collections import defaultdict
from datetime import datetime

LINE = re.compile(r"^(?P<mon>[A-Z][a-z]{2}) +(?P<day>\d{1,2}) (?P<time>\d\d:\d\d:\d\d) \S+ [^\s:\[]+(?:\[\d+\])?: (?P<msg>.*)$")
CONTROL = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|[\x02\x0f\x1d\x1f\x16]")

_WHEN = r"(?: (?P<clock>\d{1,3}:\d{2}) (?P<period>\S+(?: \d+)?))?"
GOAL = re.compile(r"^GOAL: (?P<home>.+?) (?P<h>\d+)-(?P<a>\d+) (?P<away>.+?)" + _WHEN +
                  r" \| (?P<team>.+?) \u2014 (?P<rest>.+)$")
NOGOAL_OLD = re.compile(r"^NO GOAL: (?P<home>.+?) (?P<h>\d+)-(?P<a>\d+) (?P<away>.+?) \| the (?P<clock>\d{1,3}:\d{2}) "
                        r"(?P<period>\S+(?: \d+)?) goal by (?P<scorer>.+?) \((?P<team>[^()]+)\) was disallowed"
                        r"(?: \((?P<reason>[^()]+)\))?$")
NOGOAL_NEW = re.compile(r"^NO GOAL: (?P<home>.+?) (?P<h>\d+)-(?P<a>\d+) (?P<away>.+?)" + _WHEN +
                        r" \| (?P<scorer>.+?) \((?P<team>[^()]+)\) was disallowed(?: \((?P<reason>[^()]+)\))?$")
FINAL = re.compile(r"^FINAL: (?P<home>.+?) (?P<h>\d+)-(?P<a>\d+) (?P<away>.+?)(?: \((?P<ot>OT|SO)\))?"
                   r"(?: \| Yleis\u00f6\u00e4: (?P<att>\d+))?$")
RESULTS = re.compile(r"^NHL results (?P<label>\S+): (?P<body>.+)$")
RESULT_ENTRY = re.compile(r"\b[A-Z]{2,3} \d+-\d+ [A-Z]{2,3}(?: \((?:OT|SO)\))?(?: \((?P<att>\d+)\))?")
FINISHED = re.compile(r"^All of today's (?P<sport>\S+) games have finished")
BOARD = re.compile(r"^(?:Live|Final|Upcoming): ")  # a `!nhl now` reply
BOARD_STATUS = re.compile(r"(?:\d?(?:1st|2nd|3rd|OT)|SO)(?: (?:\d+:\d\d left|int\.|end))?|\bover\b|\[[A-Z?]+\]")
SUMMARY_FOLLOWS_SECONDS = 5  # the end-of-slate results list is followed at once by "all ... games have finished"

PRINTS = (  # the bot's own journal lines (not IRC traffic): kind, pattern
    ("retract", re.compile(r"^NHL: retracting goal (?P<label>.+?) by (?P<name>.+?) in (?P<channel>#\S+)$")),
    ("back", re.compile(r"^NHL: game (?P<gid>\d+): goal \((?P<key>.*?)\) is back after (?P<n>\d+) missed poll\(s\), "
                        r"not announced again$")),
    ("listed_again", re.compile(r"^NHL: game (?P<gid>\d+): goal \((?P<key>.*?)\) listed again as event .*, "
                                r"not announced again$")),
    ("renumbered", re.compile(r"^NHL: game (?P<gid>\d+): goal \((?P<old>.*?)\) is now \((?P<new>.*?)\), "
                              r"not announced again$")),
    ("guard", re.compile(r"^NHL: game (?P<gid>\d+): the feed shows no goal for (?P<n>\d+) announced one\(s\), "
                         r"not counting this poll as a miss$")),
    ("found", re.compile(r"^NHL: game (?P<gid>\d+): attendance (?P<att>\d+) found (?P<s>\d+) s after the FINAL line$")),
    ("disagree", re.compile(r"^NHL: game (?P<gid>\d+): the play-by-play header says (?P<header>.+?) at FINAL, "
                            r"the score endpoint says (?P<endpoint>.+?); using the endpoint's$")),
    ("no_endpoint", re.compile(r"^NHL: game (?P<gid>\d+): no result from the score endpoint for the FINAL line, "
                               r"using the play-by-play header$")),
    ("liiga_missing", re.compile(r"^Liiga: game (?P<gid>\d+): (?P<team>.+?): announced goals missing from the feed "
                                 r"(?P<before>\d+) -> (?P<after>\d+)$")),
    ("start", re.compile(r"^==== BOT STARTED")),
    ("disconnect", re.compile(r"^(?:DISCONNECTED|CONNECT FAILED)")),
)
ERRORISH = re.compile(r"(?i)\b(?:error|failed|traceback|SEND REFUSED)\b")
FOUND_MATCH_SECONDS = 3  # a "found N s after the FINAL line" line belongs to the FINAL posted N s earlier, +-3 s


def strip_irc(text):
    """The text of an IRC message without its bold / colour / reset codes."""
    return CONTROL.sub("", text)


def _clean(text):
    """strip_irc, plus the stray colour digits ('07FINAL:') left when a copy through a terminal lost the
    colour byte but kept its digits."""
    return re.sub(r"^\d{2}(?=(?:GOAL|NO GOAL|FINAL):)", "", strip_irc(text))


def scrub(text):
    """Free text made safe to paste: home paths, IPv4 addresses and user@host strings removed."""
    text = re.sub(r"/home/[^/\s]+", "/home/<user>", text)
    text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<ip>", text)
    return re.sub(r"[\w.~-]+@[\w.-]+", "<user@host>", text)


def parse_line(line, year):
    """One journal line as a dict (kind, ts, ...), or None for lines that are not the bot's own."""
    m = LINE.match(line.rstrip("\r\n"))
    if not m:
        return None
    ts = datetime.strptime(f"{year} {m['mon']} {m['day']} {m['time']}", "%Y %b %d %H:%M:%S")
    msg = m["msg"]
    if msg.startswith("< "):
        return None  # incoming traffic, chat included: never used
    if msg.startswith("> "):
        sent = re.match(r"> PRIVMSG (\S+) :(.*)$", msg, re.S)
        return _parse_sent(ts, sent[1], _clean(sent[2])) if sent else None
    for kind, pattern in PRINTS:
        found = pattern.match(msg)
        if found:
            return {"kind": kind, "ts": ts, **found.groupdict()}
    if ERRORISH.search(msg):
        return {"kind": "error", "ts": ts, "text": scrub(msg)[:200]}
    return None


def _parse_sent(ts, channel, text):
    base = {"ts": ts, "channel": channel}
    m = GOAL.match(text)
    if m:
        rest = m["rest"]
        return {**base, "kind": "goal", **_game(m), "clock": m["clock"], "period": m["period"],
                "scorer": re.split(r" \(", rest, maxsplit=1)[0], "assists": "(assists:" in rest,
                "tags": [t for t in re.findall(r"\(([^()]*)\)", rest) if not t.startswith("assists:")]}
    m = NOGOAL_OLD.match(text) or NOGOAL_NEW.match(text)
    if m:
        return {**base, "kind": "nogoal", **_game(m), "clock": m["clock"], "period": m["period"],
                "scorer": m["scorer"], "reason": m["reason"]}
    m = FINAL.match(text)
    if m:
        return {**base, "kind": "final", **_game(m), "ot": m["ot"], "att": int(m["att"]) if m["att"] else None}
    m = RESULTS.match(text)
    if m:
        entries = list(RESULT_ENTRY.finditer(m["body"]))
        return {**base, "kind": "results", "label": m["label"], "games": len(entries),
                "with_figure": sum(1 for e in entries if e["att"])}
    m = FINISHED.match(text)
    if m:
        return {**base, "kind": "finished", "sport": m["sport"]}
    if BOARD.match(text):
        return {**base, "kind": "board", "text": text[:300]}
    return None


def _game(m):
    return {"home": m["home"], "away": m["away"], "h": int(m["h"]), "a": int(m["a"])}


def parse_journal(lines, year):
    entries = [e for e in (parse_line(line, year) for line in lines) if e]
    return sorted(entries, key=lambda e: e["ts"])  # stable: lines of the same second keep their order


# ---- the report ------------------------------------------------------------------------------

def build_report(entries):
    """Per channel and game what was posted, plus the journal events and the anomalies found."""
    games = {}  # (channel, home, away) -> {"goals": [...], "nogoals": [...], "final": entry | None}
    anomalies = []
    events = defaultdict(list)
    for e in entries:
        if "home" in e:
            g = games.setdefault((e["channel"], e["home"], e["away"]),
                                 {"goals": [], "nogoals": [], "final": None, "first": e["ts"], "live": {}})
            _apply(g, e, anomalies)
        else:
            events[e["kind"]].append(e)
    _split_results(events)
    founds = list(events["found"])
    for key, g in games.items():
        final = g["final"]
        if final:
            g["delay"] = int((final["ts"] - g["goals"][-1]["ts"]).total_seconds()) if g["goals"] else None
            _check_final(key, g, final, anomalies)
            if final["att"] is None:
                found = _match_found(final, founds)
                if found:
                    g["found"] = (int(found["att"]), int(found["s"]))
                else:
                    anomalies.append(f"{_name(key)} [{key[0]}]: FINAL without attendance and no figure was found later")
        elif g["goals"]:
            anomalies.append(f"{_name(key)} [{key[0]}]: goals posted but no FINAL line in the journal")
    for e in events["board"]:
        if "00:00 left" in e["text"]:
            anomalies.append(f"{e['ts']:%b %d %H:%M:%S}: a `!nhl now` board shows '00:00 left' (issue #11): {e['text'][:120]}")
    for e in events["error"]:
        anomalies.append(f"{e['ts']:%b %d %H:%M:%S}: {e['text']}")
    return {"games": games, "events": events, "anomalies": anomalies,
            "span": (entries[0]["ts"], entries[-1]["ts"]) if entries else None}


def _split_results(events):
    """A results line is the end-of-slate summary only if the 'all ... games have finished' message follows it
    at once in the same channel; any other one is the reply to someone's `!nhl results`."""
    finished = defaultdict(list)
    for e in events["finished"]:
        finished[e["channel"]].append(e["ts"])
    for e in events["results"]:
        follows = any(0 <= (t - e["ts"]).total_seconds() <= SUMMARY_FOLLOWS_SECONDS for t in finished[e["channel"]])
        events["summary" if follows else "results_reply"].append(e)


def _apply(g, e, anomalies):
    name = f"{e['home']} - {e['away']}"
    if e["kind"] == "goal":
        identity = (e["h"], e["a"], e["clock"], e["period"], e["scorer"])
        again = g["live"].get(identity)
        if again is not None:
            anomalies.append(f"{name} [{e['channel']}]: duplicate GOAL line {e['h']}-{e['a']} {e['clock']} "
                             f"{e['period']} {e['scorer']} ({int((e['ts'] - again).total_seconds())} s after the first)")
        removed = g.setdefault("removed", {}).pop((e["clock"], e["period"], e["scorer"]), None)
        if removed is not None:
            anomalies.append(f"{name} [{e['channel']}]: the {e['clock']} {e['period']} goal by {e['scorer']} was "
                             f"retracted and announced again {_minutes(e['ts'] - removed)} later (reinstated)")
        g["live"][identity] = e["ts"]
        g["goals"].append(e)
    elif e["kind"] == "nogoal":
        for identity in [i for i in g["live"] if i[2:] == (e["clock"], e["period"], e["scorer"])]:
            del g["live"][identity]
        g.setdefault("removed", {})[(e["clock"], e["period"], e["scorer"])] = e["ts"]
        g["nogoals"].append(e)
        if not e["reason"]:
            anomalies.append(f"{name} [{e['channel']}]: NO GOAL for the {e['clock']} {e['period']} goal by "
                             f"{e['scorer']} without a reason")
    else:
        g["final"] = e


def _check_final(key, g, final, anomalies):
    if final["ot"] == "SO":
        return  # the shootout winner's extra goal is not a GOAL line
    posted = sorted(g["goals"] + g["nogoals"], key=lambda e: e["ts"])
    if posted and (posted[-1]["h"], posted[-1]["a"]) != (final["h"], final["a"]):
        anomalies.append(f"{_name(key)} [{key[0]}]: FINAL {final['h']}-{final['a']} but the last "
                         f"{'GOAL' if posted[-1]['kind'] == 'goal' else 'NO GOAL'} line said "
                         f"{posted[-1]['h']}-{posted[-1]['a']}")


def _match_found(final, founds):
    """The not yet used 'attendance found N s after the FINAL line' that belongs to this FINAL."""
    best = None
    for f in founds:
        gap = abs((f["ts"] - final["ts"]).total_seconds() - int(f["s"]))
        if gap <= FOUND_MATCH_SECONDS and (best is None or gap < best[0]):
            best = (gap, f)
    if best:
        founds.remove(best[1])
        return best[1]
    return None


def _name(key):
    return f"{key[1]} - {key[2]}"


def _minutes(delta):
    seconds = int(delta.total_seconds())
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


def render(report):
    out = []
    span = report["span"]
    ev = report["events"]
    out.append("GAME NIGHT REPORT (journal time)")
    if not span:
        return "GAME NIGHT REPORT\nNo bot lines found in the input.\n"
    out.append(f"  from {span[0]:%b %d %H:%M:%S} to {span[1]:%b %d %H:%M:%S}")
    out.append(f"  bot starts: {len(ev['start'])}, disconnects: {len(ev['disconnect'])}")
    out.append("")
    by_channel = defaultdict(lambda: [0, 0, 0])
    for (channel, _, _), g in report["games"].items():
        by_channel[channel][0] += len(g["goals"])
        by_channel[channel][1] += len(g["nogoals"])
        by_channel[channel][2] += 1 if g["final"] else 0
    out.append("Lines posted")
    for channel, (goals, nogoals, finals) in sorted(by_channel.items()):
        out.append(f"  {channel}: {goals} GOAL, {nogoals} NO GOAL, {finals} FINAL")
    out.append("")
    out.append("Games")
    names = defaultdict(list)
    for key in report["games"]:
        names[(key[1], key[2])].append(key)
    for (home, away), keys in sorted(names.items(), key=lambda item: min(report["games"][k]["first"] for k in item[1])):
        final = next((report["games"][k]["final"] for k in keys if report["games"][k]["final"]), None)
        score = f"FINAL {final['h']}-{final['a']}{' (' + final['ot'] + ')' if final['ot'] else ''}" if final else "no FINAL"
        out.append(f"  {home} - {away}: {score}")
        for key in sorted(keys):
            g = report["games"][key]
            line = f"    {key[0]}: {len(g['goals'])} GOAL" + (f", {len(g['nogoals'])} NO GOAL" if g["nogoals"] else "")
            if g["final"]:
                line += f"; FINAL {g['final']['ts']:%H:%M:%S}"
                if g.get("delay") is not None:
                    line += f", {g['delay']} s after the last GOAL line"
                if g["final"]["att"] is not None:
                    line += f"; attendance {g['final']['att']} on the line"
                elif g.get("found"):
                    line += f"; attendance {g['found'][0]} found {g['found'][1]} s later"
                else:
                    line += "; no attendance"
            out.append(line)
    out.append("")
    out.append("Journal events")
    out.append(f"  repeated goals ignored (feed flicker): {len(ev['back'])}"
               f", listed twice: {len(ev['listed_again'])}, renumbered: {len(ev['renumbered'])}"
               f", broken-feed polls not counted: {len(ev['guard'])}")
    out.append(f"  goals retracted: {len(ev['retract'])}")
    if ev["liiga_missing"]:
        out.append(f"  Liiga: announced goals missing from the feed (a dip, or a goal taken away): {len(ev['liiga_missing'])} change(s)")
        for e in ev["liiga_missing"]:
            out.append(f"    {e['ts']:%H:%M:%S} {e['team']}: {e['before']} -> {e['after']}")
    for e in ev["retract"]:
        out.append(f"    {e['ts']:%H:%M:%S} {e['label']} by {e['name']} in {e['channel']}")
    out.append(f"  FINAL score source: {len(ev['disagree'])} header/endpoint disagreement(s), "
               f"{len(ev['no_endpoint'])} without an endpoint result")
    for e in ev["disagree"]:
        out.append(f"    {e['ts']:%H:%M:%S} header {e['header']} -> endpoint {e['endpoint']}")
    for e in ev["summary"]:
        out.append(f"  end-of-slate results list {e['ts']:%H:%M:%S} in {e['channel']}: {e['games']} games, "
                   f"{e['with_figure']} with a figure")
    if ev["results_reply"]:
        out.append(f"  replies to `!nhl results`: {len(ev['results_reply'])}")
    if ev["board"]:
        statuses = list(dict.fromkeys(t for e in ev["board"] if e["text"].startswith("Live: ")
                                      for t in BOARD_STATUS.findall(e["text"].split(" || ")[0])))
        out.append(f"  `!nhl now` replies: {len(ev['board'])}" + (f"; live statuses seen: {', '.join(statuses)}" if statuses else ""))
    for e in ev["finished"]:
        out.append(f"  '{e['sport']} games finished' {e['ts']:%H:%M:%S} in {e['channel']}")
    out.append("")
    out.append(f"Anomalies ({len(report['anomalies'])})")
    out.extend(f"  - {a}" for a in report["anomalies"] or ["none"])
    return "\n".join(out) + "\n"


def main(argv):
    year = datetime.now().year
    args = [a for a in argv if a != "-"]
    if "--year" in args:
        i = args.index("--year")
        year = int(args[i + 1])
        del args[i:i + 2]
    if args and args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    lines = open(args[0], encoding="utf-8", errors="replace").read().splitlines() if args else sys.stdin.read().splitlines()
    sys.stdout.write(render(build_report(parse_journal(lines, year))))


if __name__ == "__main__":
    main(sys.argv[1:])
