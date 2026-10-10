"""tools/journal_report.py: the journal parser. Synthetic journal lines for each case, plus
round trips through the bot's own message builders so a changed wording breaks a test here."""
import os
import sys
from unittest.mock import MagicMock

import pytest

TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import journal_report as jr  # noqa: E402
from irc_format import BOLD, GREEN, ORANGE, RED, RESET  # noqa: E402

YEAR = 2026


def line(time, msg, day="07", host="host-a", proc="python3[100]"):
    return f"Oct {day} {time} {host} {proc}: {msg}"


def sent(time, channel, text, **kw):
    return line(time, f"> PRIVMSG {channel} :{text}", **kw)


def goal_text(home, h, a, away, clock, period, team, rest):
    return f"{BOLD}{GREEN}GOAL:{RESET} {BOLD}{home} {h}-{a} {away}{RESET} {clock} {period} | {team} — {rest}"


def nogoal_text(home, h, a, away, clock, period, scorer, team, reason=None):
    why = f" ({reason})" if reason else ""
    return (f"{BOLD}{RED}NO GOAL:{RESET} {BOLD}{home} {h}-{a} {away}{RESET} {clock} {period} | "
            f"{scorer} ({team}) was disallowed{why}")


def final_text(home, h, a, away, ot=None, att=None):
    return (f"{BOLD}{ORANGE}FINAL:{RESET} {home} {h}-{a} {away}" + (f" ({ot})" if ot else "")
            + (f" | Yleisöä: {att}" if att else ""))


def report(*lines):
    return jr.build_report(jr.parse_journal(list(lines), YEAR))


CH = "#chan"
GOAL1 = sent("01:00:00", CH, goal_text("Reds", 1, 0, "Blues", "05:00", "1st", "Reds", "Alice Ann (assists: Bo Bee)"))


class TestStripAndScrub:
    def test_bold_colour_and_reset_codes_are_removed(self):
        assert jr.strip_irc(f"{BOLD}{ORANGE}FINAL:{RESET} A 1-0 B") == "FINAL: A 1-0 B"

    def test_a_colour_with_a_background_is_removed_whole(self):
        assert jr.strip_irc("\x0304,01red\x0f on black") == "red on black"

    def test_home_paths_addresses_and_user_at_host_are_scrubbed(self):
        text = jr.scrub('File "/home/someone/git/x.py" from 192.0.2.40 by nick!uid@host.example.net')
        assert "someone" not in text and "192.0.2.40" not in text and "host.example.net" not in text
        assert "/home/<user>" in text and "<ip>" in text and "<user@host>" in text


class TestParseLine:
    def test_colour_digits_left_over_from_a_lossy_copy_are_dropped(self):
        for label, text in (("final", "07FINAL: Reds 5-3 Blues"), ("nogoal", "04NO GOAL: Reds 4-1 Blues 06:08 2nd | A B (Reds) was disallowed"),
                            ("goal", "03GOAL: Reds 1-0 Blues 05:00 1st | Reds \u2014 A B")):
            assert jr.parse_line(sent("01:00:00", CH, text), YEAR)["kind"] == label

    def test_the_bots_own_goal_line_is_parsed(self):
        e = jr.parse_line(sent("01:02:03", CH, goal_text("Reds", 2, 1, "Blues", "07:12", "2nd", "Reds",
                                                          "Alice Ann (PP) (assists: Bo Bee, Cy Cee)")), YEAR)
        assert (e["kind"], e["channel"], e["home"], e["away"], e["h"], e["a"]) == ("goal", CH, "Reds", "Blues", 2, 1)
        assert (e["clock"], e["period"], e["scorer"], e["assists"], e["tags"]) == ("07:12", "2nd", "Alice Ann", True, ["PP"])
        assert (e["ts"].year, e["ts"].month, e["ts"].day, e["ts"].hour) == (YEAR, 10, 7, 1)

    def test_a_shootout_goal_with_its_tally_instead_of_a_clock(self):
        text = f"{BOLD}{GREEN}GOAL:{RESET} {BOLD}Reds 2-2 Blues Penguins{RESET} SO 1-0 | Reds \u2014 Alice Ann"
        e = jr.parse_line(sent("01:00:00", CH, text), YEAR)
        assert (e["kind"], e["home"], e["away"], e["h"], e["a"]) == ("goal", "Reds", "Blues Penguins", 2, 2)
        assert (e["period"], e["clock"], e["shootout"], e["scorer"]) == ("SO", None, (1, 0), "Alice Ann")

    def test_an_ordinary_goal_has_no_shootout_tally(self):
        assert jr.parse_line(GOAL1, YEAR)["shootout"] is None

    def test_a_goal_without_assists_or_tags(self):
        e = jr.parse_line(sent("01:00:00", CH, goal_text("Reds", 1, 0, "Blues", "04:38", "OT", "Reds", "Alice Ann")), YEAR)
        assert (e["scorer"], e["assists"], e["tags"], e["period"]) == ("Alice Ann", False, [], "OT")

    def test_team_names_with_hyphens_and_accents(self):
        e = jr.parse_line(sent("01:00:00", CH, goal_text("Kärpät", 6, 2, "K-Espoo", "17:25", "2nd", "Kärpät",
                                                          "Rasmus Rissanen")), YEAR)
        assert (e["home"], e["away"], e["h"], e["a"]) == ("Kärpät", "K-Espoo", 6, 2)

    def test_no_goal_in_the_current_wording(self):
        e = jr.parse_line(sent("01:00:00", CH, nogoal_text("Reds", 4, 4, "Blues", "11:47", "3rd", "Alice Ann", "Reds",
                                                            "offside challenge")), YEAR)
        assert (e["kind"], e["clock"], e["period"], e["scorer"], e["reason"]) == \
            ("nogoal", "11:47", "3rd", "Alice Ann", "offside challenge")
        assert (e["h"], e["a"]) == (4, 4)

    def test_no_goal_without_a_reason(self):
        e = jr.parse_line(sent("01:00:00", CH, nogoal_text("Reds", 4, 1, "Blues", "06:08", "2nd", "Alice Ann", "Reds")), YEAR)
        assert e["reason"] is None

    def test_no_goal_in_the_old_wording_still_parses(self):
        text = (f"{BOLD}{RED}NO GOAL:{RESET} {BOLD}Reds 4-1 Blues{RESET} | the 06:08 2nd goal by Alice Ann (Reds) "
                "was disallowed (offside challenge)")
        e = jr.parse_line(sent("01:00:00", CH, text), YEAR)
        assert (e["kind"], e["clock"], e["period"], e["scorer"], e["reason"]) == \
            ("nogoal", "06:08", "2nd", "Alice Ann", "offside challenge")

    @pytest.mark.parametrize("text, ot, att", [
        (final_text("Reds", 5, 3, "Blues"), None, None),
        (final_text("Reds", 3, 2, "Blues", ot="OT", att=18792), "OT", 18792),
        (final_text("Reds", 3, 2, "Blues", ot="SO"), "SO", None),
    ])
    def test_final_variants(self, text, ot, att):
        e = jr.parse_line(sent("01:00:00", CH, text), YEAR)
        assert (e["kind"], e["ot"], e["att"]) == ("final", ot, att)

    def test_the_results_list_counts_its_games_and_figures(self):
        text = "NHL results 7.10.: TOR 5-4 NSH (OT) (18792), MTL 4-6 CAR (20962), NJD 3-5 UTA, BUF 3-2 MIN (OT) (19070)"
        e = jr.parse_line(sent("01:00:00", CH, text), YEAR)
        assert (e["kind"], e["games"], e["with_figure"]) == ("results", 4, 3)

    def test_the_all_finished_message(self):
        e = jr.parse_line(sent("01:00:00", CH, "All of today's Liiga games have finished. Live tracking stopped."), YEAR)
        assert (e["kind"], e["sport"]) == ("finished", "Liiga")

    @pytest.mark.parametrize("msg, kind", [
        ("NHL: retracting goal 06:08 2nd by Alice Ann in #chan", "retract"),
        ("NHL: game 45: goal (team 12, 3-5) is back after 2 missed poll(s), not announced again", "back"),
        ("NHL: game 45: goal (team 12, 3-5) listed again as event [7], not announced again", "listed_again"),
        ("NHL: game 45: goal (team 12, 3-5) is now (team 12, 2-5), not announced again", "renumbered"),
        ("NHL: game 45: the feed shows no goal for 2 announced one(s), not counting this poll as a miss", "guard"),
        ("NHL: game 45: attendance 17583 found 151 s after the FINAL line", "found"),
        ("NHL: game 45: the play-by-play header says 2-2 REG at FINAL, the score endpoint says 3-2 OT; using the endpoint's",
         "disagree"),
        ("NHL: game 45: no result from the score endpoint for the FINAL line, using the play-by-play header", "no_endpoint"),
        ("Liiga: game 7: Jokerit: announced goals missing from the feed 0 -> 1", "liiga_missing"),
        ("==== BOT STARTED pid=123 ====", "start"),
        ("DISCONNECTED: server closed the connection (EOF)", "disconnect"),
    ])
    def test_the_bots_own_prints(self, msg, kind):
        assert jr.parse_line(line("01:00:00", msg), YEAR)["kind"] == kind

    def test_an_error_print_is_kept_but_scrubbed(self):
        e = jr.parse_line(line("01:00:00", 'NHL API score request failed for date=2026-10-07: 192.0.2.3 /home/svc/x'), YEAR)
        assert e["kind"] == "error" and "192.0.2.3" not in e["text"] and "/home/svc" not in e["text"]

    def test_incoming_chat_is_ignored_even_when_it_looks_like_a_bot_line_or_an_error(self):
        chat = line("01:00:00", f"< :nick!uid@host.example PRIVMSG {CH} :" + goal_text("A", 1, 0, "B", "01:00", "1st", "A", "X Y"))
        error = line("01:00:00", f"< :nick!uid@host.example PRIVMSG {CH} :this is an error")
        assert jr.parse_line(chat, YEAR) is None and jr.parse_line(error, YEAR) is None

    def test_other_commands_replies_and_other_noise_are_ignored(self):
        assert jr.parse_line(sent("01:00:00", CH, "Current weather in Austin: Sunny, 29 C"), YEAR) is None
        assert jr.parse_line(line("01:00:00", "Responding to PING from irc.example.org"), YEAR) is None
        assert jr.parse_line("not a journal line", YEAR) is None
        assert jr.parse_line(line("01:00:00", "> PONG irc.example.org"), YEAR) is None

    def test_host_and_process_id_never_reach_the_entry(self):
        e = jr.parse_line(GOAL1, YEAR)
        assert "host-a" not in repr(e) and "python3" not in repr(e)


class TestAnomalies:
    def test_a_duplicate_goal_line_is_flagged(self):
        r = report(GOAL1, sent("01:00:05", CH, goal_text("Reds", 1, 0, "Blues", "05:00", "1st", "Reds", "Alice Ann")))
        assert any("duplicate GOAL line" in a for a in r["anomalies"])

    def test_two_different_goals_are_not_duplicates(self):
        r = report(GOAL1, sent("01:10:00", CH, goal_text("Reds", 2, 0, "Blues", "09:00", "1st", "Reds", "Alice Ann")),
                   sent("01:50:00", CH, final_text("Reds", 2, 0, "Blues", att=1000)))
        assert r["anomalies"] == []

    def test_a_retracted_goal_that_returns_is_flagged_as_reinstated_with_the_gap(self):
        g = lambda t: sent(t, CH, goal_text("Reds", 4, 2, "Blues", "06:08", "2nd", "Blues", "Alice Ann (PP)"))
        r = report(g("01:13:43"), sent("01:27:13", CH, nogoal_text("Reds", 4, 1, "Blues", "06:08", "2nd", "Alice Ann", "Blues")),
                   g("02:16:54"))
        assert any("retracted and announced again 49 min 41 s later (reinstated)" in a for a in r["anomalies"])
        assert not any("duplicate" in a for a in r["anomalies"])

    def test_a_no_goal_without_a_reason_is_flagged_and_one_with_a_reason_is_not(self):
        first = sent("01:00:00", CH, goal_text("Reds", 1, 0, "Blues", "05:25", "3rd", "Reds", "Alice Ann"))
        no_reason = sent("01:02:00", CH, nogoal_text("Reds", 0, 0, "Blues", "05:25", "3rd", "Alice Ann", "Reds"))
        with_reason = sent("01:02:00", CH, nogoal_text("Reds", 0, 0, "Blues", "05:25", "3rd", "Alice Ann", "Reds", "offside challenge"))
        assert any("without a reason" in a for a in report(first, no_reason)["anomalies"])
        assert not any("without a reason" in a for a in report(first, with_reason)["anomalies"])

    def test_a_final_that_differs_from_the_last_goal_is_flagged(self):
        r = report(GOAL1, sent("01:50:00", CH, final_text("Reds", 2, 0, "Blues", att=1000)))
        assert any("FINAL 2-0 but the last GOAL line said 1-0" in a for a in r["anomalies"])

    def test_a_matching_final_is_fine_and_a_shootout_final_is_not_compared(self):
        assert report(GOAL1, sent("01:50:00", CH, final_text("Reds", 1, 0, "Blues", att=1000)))["anomalies"] == []
        assert report(GOAL1, sent("01:50:00", CH, final_text("Reds", 2, 1, "Blues", ot="SO", att=1000)))["anomalies"] == []

    def test_the_last_no_goal_scores_is_what_the_final_is_compared_with(self):
        r = report(GOAL1, sent("01:10:00", CH, nogoal_text("Reds", 0, 0, "Blues", "05:00", "1st", "Alice Ann", "Reds", "x")),
                   sent("01:50:00", CH, final_text("Reds", 0, 0, "Blues", att=1000)))
        assert r["anomalies"] == []

    def test_goals_without_a_final_are_flagged(self):
        assert any("no FINAL line" in a for a in report(GOAL1)["anomalies"])

    def test_errors_become_anomalies(self):
        r = report(GOAL1, line("01:05:00", "NHL API score request failed for date=2026-10-07: timeout"))
        assert any("score request failed" in a for a in r["anomalies"])


class TestAttendance:
    FINAL_A = sent("01:42:26", "#a", final_text("Reds", 1, 0, "Blues"))
    FINAL_B = sent("01:42:27", "#b", final_text("Reds", 1, 0, "Blues"))
    GOAL_A = sent("01:40:00", "#a", goal_text("Reds", 1, 0, "Blues", "05:00", "1st", "Reds", "Alice Ann"))
    GOAL_B = sent("01:40:01", "#b", goal_text("Reds", 1, 0, "Blues", "05:00", "1st", "Reds", "Alice Ann"))

    def found(self, time, seconds):
        return line(time, f"NHL: game 45: attendance 17583 found {seconds} s after the FINAL line")

    def test_each_channels_final_gets_its_own_found_line(self):
        r = report(self.GOAL_A, self.GOAL_B, self.FINAL_A, self.FINAL_B, self.found("01:44:57", 151), self.found("01:44:58", 151))
        assert r["anomalies"] == []
        assert {k[0]: g["found"] for k, g in r["games"].items()} == {"#a": (17583, 151), "#b": (17583, 151)}

    def test_a_final_with_the_figure_on_the_line_needs_no_found_line(self):
        r = report(self.GOAL_A, sent("01:42:26", "#a", final_text("Reds", 1, 0, "Blues", att=17583)))
        assert r["anomalies"] == []

    def test_a_final_with_no_figure_ever_is_flagged(self):
        r = report(self.GOAL_A, self.FINAL_A)
        assert any("FINAL without attendance" in a for a in r["anomalies"])

    def test_a_found_line_for_another_time_does_not_match(self):
        r = report(self.GOAL_A, self.FINAL_A, self.found("01:44:57", 40))
        assert any("FINAL without attendance" in a for a in r["anomalies"])


class TestReport:
    def test_the_delay_after_the_last_goal_line(self):
        r = report(GOAL1, sent("01:01:30", CH, final_text("Reds", 1, 0, "Blues", att=1000)))
        assert next(iter(r["games"].values()))["delay"] == 90

    def test_journal_events_are_counted(self):
        r = report(GOAL1, line("01:00:10", "NHL: game 1: goal (team 1, 1-0) is back after 1 missed poll(s), not announced again"),
                   line("01:00:11", "NHL: retracting goal 05:00 1st by Alice Ann in #chan"),
                   line("00:59:00", "==== BOT STARTED pid=9 ===="))
        assert (len(r["events"]["back"]), len(r["events"]["retract"]), len(r["events"]["start"])) == (1, 1, 1)

    def test_entries_are_ordered_by_time_even_if_the_input_is_not(self):
        g = lambda t: sent(t, CH, goal_text("Reds", 4, 2, "Blues", "06:08", "2nd", "Blues", "Alice Ann"))
        nogoal = sent("01:27:13", CH, nogoal_text("Reds", 4, 1, "Blues", "06:08", "2nd", "Alice Ann", "Blues"))
        r = report(g("02:16:54"), nogoal, g("01:13:43"))  # a later GOAL first, the earlier one last
        assert any("announced again 49 min 41 s later" in a for a in r["anomalies"])

    def test_a_night_across_midnight_keeps_its_order(self):
        late = sent("23:59:00", CH, goal_text("Reds", 1, 0, "Blues", "05:00", "1st", "Reds", "Alice Ann"), day="07")
        after = sent("00:30:00", CH, final_text("Reds", 1, 0, "Blues", att=1), day="08")
        assert next(iter(report(late, after)["games"].values()))["delay"] == 31 * 60

    def test_empty_input(self):
        assert "No bot lines found" in jr.render(report())


class TestRender:
    def text(self, *lines):
        return jr.render(report(*lines))

    def test_the_report_shows_games_channels_and_anomalies(self):
        out = self.text(GOAL1, sent("01:50:00", CH, final_text("Reds", 1, 0, "Blues", ot="OT", att=1000)))
        assert "Reds - Blues: FINAL 1-0 (OT)" in out and "#chan: 1 GOAL" in out and "attendance 1000 on the line" in out
        assert "Anomalies (0)" in out and "none" in out

    def test_chat_host_names_and_process_ids_never_appear(self):
        out = self.text(GOAL1, line("01:00:30", f"< :nick!uid@host.example PRIVMSG {CH} :an error in the chat"),
                        sent("01:50:00", CH, final_text("Reds", 1, 0, "Blues", att=1000)))
        assert "nick" not in out and "host-a" not in out and "host.example" not in out and "python3" not in out
        assert "Anomalies (0)" in out

    def test_liiga_goals_missing_from_the_feed_are_listed(self):
        out = self.text(GOAL1, line("01:05:00", "Liiga: game 7: Jokerit: announced goals missing from the feed 0 -> 1"),
                        line("01:05:10", "Liiga: game 7: Jokerit: announced goals missing from the feed 1 -> 0"))
        assert "announced goals missing from the feed (a dip, or a goal taken away): 2 change(s)" in out
        assert "01:05:00 Jokerit: 0 -> 1" in out and "01:05:10 Jokerit: 1 -> 0" in out

    def test_nothing_is_said_about_missing_liiga_goals_when_there_are_none(self):
        assert "missing from the feed" not in self.text(GOAL1)

    def test_retracted_goals_are_listed_by_name(self):
        out = self.text(GOAL1, line("01:05:00", "NHL: retracting goal 06:08 2nd by Alice Ann in #chan"))
        assert "goals retracted: 1" in out and "01:05:00 06:08 2nd by Alice Ann in #chan" in out

    def test_a_missing_attendance_and_the_score_source_lines_are_shown(self):
        out = self.text(GOAL1, sent("01:50:00", CH, final_text("Reds", 1, 0, "Blues")),
                        line("01:50:01", "NHL: game 1: the play-by-play header says 2-2 REG at FINAL, the score endpoint says 3-2 OT; "
                                         "using the endpoint's"))
        assert "no attendance" in out and "header 2-2 REG -> endpoint 3-2 OT" in out


class TestRenderSummaryLines:
    def test_a_late_attendance_the_results_list_and_the_all_finished_line_are_shown(self):
        out = jr.render(report(
            sent("01:40:00", CH, goal_text("Reds", 1, 0, "Blues", "05:00", "1st", "Reds", "Alice Ann")),
            sent("01:42:26", CH, final_text("Reds", 1, 0, "Blues")),
            line("01:44:57", "NHL: game 45: attendance 17583 found 151 s after the FINAL line"),
            sent("01:50:00", CH, "NHL results 7.10.: RED 1-0 BLU (17583)"),
            sent("01:50:01", CH, "All of today's NHL games have finished. Live tracking stopped.")))
        assert "attendance 17583 found 151 s later" in out
        assert "end-of-slate results list 01:50:00 in #chan: 1 games, 1 with a figure" in out
        assert "'NHL games finished' 01:50:01 in #chan" in out


class TestBoardsAndResultsReplies:
    """The slate's results list versus someone's `!nhl results`, and the `!nhl now` boards."""

    def test_a_results_line_followed_at_once_by_the_finished_message_is_the_summary(self):
        r = report(sent("01:50:00", CH, "NHL results 7.10.: RED 1-0 BLU (17583)"),
                   sent("01:50:01", CH, "All of today's NHL games have finished. Live tracking stopped."))
        assert len(r["events"]["summary"]) == 1 and r["events"]["results_reply"] == []

    def test_a_results_line_with_no_finished_message_after_it_is_a_reply_to_the_command(self):
        r = report(sent("01:50:00", CH, "NHL results 7.10.: RED 1-0 BLU (1 game(s) still on: !nhl now)"))
        assert r["events"]["summary"] == [] and len(r["events"]["results_reply"]) == 1
        assert "replies to `!nhl results`: 1" in jr.render(r)

    def test_a_finished_message_in_another_channel_does_not_make_it_a_summary(self):
        r = report(sent("01:50:00", "#a", "NHL results 7.10.: RED 1-0 BLU"),
                   sent("01:50:01", "#b", "All of today's NHL games have finished. Live tracking stopped."))
        assert r["events"]["summary"] == [] and len(r["events"]["results_reply"]) == 1

    def test_board_replies_are_counted_and_their_live_statuses_listed(self):
        out = jr.render(report(
            sent("01:00:00", CH, "Live: ANA 1-0 EDM 1st 13:14 left || Final: WSH 5-3 PIT"),
            sent("01:30:00", CH, "Live: ANA 2-0 EDM 1st int. || Final: WSH 5-3 PIT"),
            sent("02:00:00", CH, "Upcoming: 05:00 ANA-EDM")))
        assert "`!nhl now` replies: 3; live statuses seen: 1st 13:14 left, 1st int." in out

    def test_a_board_showing_zero_zero_left_is_flagged_as_issue_11(self):
        r = report(sent("01:00:00", CH, "Live: ANA 1-0 EDM 1st 00:00 left"))
        assert any("'00:00 left'" in a and "#11" in a for a in r["anomalies"])

    def test_a_board_with_an_intermission_or_end_status_is_not_flagged(self):
        r = report(sent("01:00:00", CH, "Live: ANA 1-0 EDM 1st end || Final: A 1-0 B"),
                   sent("01:01:00", CH, "Live: ANA 1-0 EDM 1st int."))
        assert r["anomalies"] == []


class TestMain:
    def test_main_reads_a_file_and_prints_the_report(self, tmp_path, capsys):
        path = tmp_path / "journal.txt"
        path.write_text("\n".join([GOAL1, sent("01:50:00", CH, final_text("Reds", 1, 0, "Blues", att=1))]), encoding="utf-8")
        jr.main([str(path), "--year", "2026"])
        assert "Reds - Blues: FINAL 1-0" in capsys.readouterr().out

    def test_main_reads_standard_input(self, monkeypatch, capsys):
        import io
        monkeypatch.setattr(sys, "stdin", io.StringIO(GOAL1 + "\n"))
        jr.main([])
        assert "1 GOAL" in capsys.readouterr().out

    def test_help_exits_with_the_usage(self):
        with pytest.raises(SystemExit):
            jr.main(["--help"])


class TestRoundTrip:
    """The bot's own message builders feed the parser, so a reworded message fails here."""

    def test_nhl_goal_no_goal_and_final(self):
        from nhl_command import NHLCommand
        from tests.test_nhl_command import goal_play, make_pbp, roster_spot
        nhl = NHLCommand()
        nhl.session.get = MagicMock()
        goal = goal_play(event_id=7, event_owner_team_id=10, scoring_player_id=100, period_number=3, time_in_period="11:47",
                         home_score=5, away_score=4, assist1=101)
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, home_score=4, away_score=4, plays=[goal],
                       roster=[roster_spot(100, "Wil", "Nyl"), roster_spot(101, "Aus", "Mat")])
        e = jr.parse_line(sent("01:00:00", CH, nhl._format_goal(pbp, goal)), YEAR)
        assert (e["kind"], e["home"], e["away"], e["h"], e["a"], e["clock"], e["period"], e["scorer"], e["assists"]) == \
            ("goal", "Carolina Hurricanes", "Florida Panthers", 5, 4, "11:47", "3rd", "Wil Nyl", True)
        posted = nhl._posted_info(pbp, goal)
        e = jr.parse_line(sent("01:03:00", CH, nhl._format_retraction(pbp, posted)), YEAR)
        assert (e["kind"], e["clock"], e["period"], e["scorer"]) == ("nogoal", "11:47", "3rd", "Wil Nyl")
        bot = MagicMock()
        final_pbp = make_pbp(game_id=1, home_id=10, away_id=20, home_score=5, away_score=4, last_period_type="OT")
        nhl._announce_end(bot, CH, final_pbp)
        e = jr.parse_line(sent("01:20:00", CH, bot.send_message.call_args[0][1]), YEAR)
        assert (e["kind"], e["h"], e["a"], e["ot"]) == ("final", 5, 4, "OT")

    def test_nhl_shootout_goal_round_trip(self):
        from nhl_command import NHLCommand
        from tests.test_nhl_command import goal_play, make_pbp, roster_spot
        nhl = NHLCommand()
        nhl.session.get = MagicMock()
        goal = goal_play(event_id=7, event_owner_team_id=10, scoring_player_id=100, period_number=5, period_type="SO",
                         time_in_period="00:00", home_score=2, away_score=2)
        pbp = make_pbp(game_id=1, home_id=10, away_id=20, plays=[goal], roster=[roster_spot(100, "Kent", "Johnson")])
        e = jr.parse_line(sent("01:00:00", CH, nhl._format_goal(pbp, goal)), YEAR)
        assert (e["home"], e["away"], e["h"], e["a"], e["period"], e["shootout"], e["scorer"]) == \
            ("Carolina Hurricanes", "Florida Panthers", 2, 2, "SO", (1, 0), "Kent Johnson")

    def test_liiga_goal_and_final(self):
        from liiga_command import LiigaCommand
        from tests.test_liiga_command import goal_event, make_game
        liiga = LiigaCommand()
        game = make_game(home_goals=[goal_event(home_score=1, away_score=0, assists=[{"firstName": "Bo", "lastName": "Bee"}])],
                         ended=True)
        game["spectators"] = 4366
        e = jr.parse_line(sent("01:00:00", CH, liiga._format_goal(game, "homeTeam", game["homeTeam"]["goalEvents"][0])), YEAR)
        assert (e["kind"], e["h"], e["a"], e["assists"]) == ("goal", 1, 0, True)
        bot = MagicMock()
        liiga._announce_end(bot, CH, game)
        e = jr.parse_line(sent("01:20:00", CH, bot.send_message.call_args[0][1]), YEAR)
        assert (e["kind"], e["h"], e["a"], e["att"]) == ("final", 1, 0, 4366)
