"""The NHL tracker's end of a game, split out of nhl_command.py (issue #10): the FINAL line and the result it states,
the attendance figure that often arrives minutes later, and the end-of-slate results list."""
import re
import time

import requests

from irc_format import ORANGE, prefix as irc_prefix


class NHLFinalMixin:
    """What the NHL tracker does when a game ends.

    The FINAL line states a result that is never level (read from the score endpoint or, when that is stale, from the
    plays: issue #12), the attendance figure is looked up again every poll for
    ATTENDANCE_RETRY_SECONDS when the report has not got it yet (issue #8), and when the last game is over one
    results list with every figure follows. Mixed into NHLCommand, whose fetches, state and goal formatting it uses.
    """

    # Attendance isn't in any of api-web.nhle.com's JSON (checked landing,
    # boxscore, play-by-play, right-rail, game-story) - only in NHL's HTML
    # Game Summary report, as "Attendance 19,250&nbsp;at&nbsp;Lenovo Center".
    REPORTS_URL = "https://www.nhl.com/scores/htmlreports"

    ATTENDANCE_RE = re.compile(r"Attendance(?:\s|&nbsp;|<[^>]*>){0,40}?(\d[\d,]*)", re.I)

    # The report's attendance figure shows up 61-212 s after the final horn in
    # about two games of three (17 games on two nights, issue #8; one more took
    # longer than 240 s); a missing figure is retried every poll for this long.
    ATTENDANCE_RETRY_SECONDS = 420

    FINAL_SCORE_MAX_AGE_SECONDS = 3

    # A FINAL line is never posted for a level score (see _announce_end): it waits up to this many polls
    # for a result, then goes out as it is.
    FINAL_HOLD_POLLS = 6

    FINAL_PREFIX = irc_prefix("FINAL:", ORANGE)

    def _announce_end(self, irc_bot, channel, pbp, slate=None, hold_allowed=False):
        """Posts the FINAL: line and returns its record, or None when `hold_allowed` and the result is
        still level (no line is posted then; the caller tries again next poll)."""
        home = self._team_name(pbp.get("homeTeam") or {})
        away = self._team_name(pbp.get("awayTeam") or {})
        home_score = (pbp.get("homeTeam") or {}).get("score", "?")
        away_score = (pbp.get("awayTeam") or {}).get("score", "?")
        last_period_type = (pbp.get("gameOutcome") or {}).get("lastPeriodType") or ""

        # The play-by-play header can still hold the pre-overtime score and no
        # OT outcome for 30 s or more after the game turned FINAL (seen once:
        # 2-2 posted for a 3-2 overtime win); /score/{date} was already right
        # while the game was in OVER, in every case watched. See issue #12.
        official = self._official_result(pbp.get("id"), slate)
        if official:
            (official_home, official_away), official_type = official
            if official_type is None:
                official_type = last_period_type
            def shown(period_type):  # as the line shows it
                return period_type if period_type in ("OT", "SO") else "REG"

            if ((official_home, official_away), shown(official_type)) != ((home_score, away_score), shown(last_period_type)):
                print(f"NHL: game {pbp.get('id')}: the play-by-play header says "
                      f"{home_score}-{away_score} {shown(last_period_type)} at FINAL, the score endpoint says "
                      f"{official_home}-{official_away} {shown(official_type)}; using the endpoint's")
            home_score, away_score, last_period_type = official_home, official_away, official_type
        elif slate:
            print(f"NHL: game {pbp.get('id')}: no result from the score endpoint for the FINAL line, "
                  f"using the play-by-play header")

        # An NHL game never ends level, so a level score at FINAL is a stale result - in the shootout game
        # of 2026-10-13 the header and the score endpoint both still said 2-2 for about 20 s after the
        # state turned FINAL, and the line went out as "2-2" with no (SO). The plays already hold the
        # decision (the overtime goal's running score, the shootout goals), so the result is read from
        # them; if they do not decide it either, the line waits a few polls (issue #12).
        if isinstance(home_score, int) and isinstance(away_score, int) and home_score == away_score:
            from_plays = self._result_from_plays(pbp)
            if from_plays:
                print(f"NHL: game {pbp.get('id')}: level at {home_score}-{away_score} at FINAL, "
                      f"the plays say {from_plays[0]}-{from_plays[1]} {from_plays[2]}; using the plays'")
                home_score, away_score, last_period_type = from_plays
            elif hold_allowed:
                print(f"NHL: game {pbp.get('id')}: FINAL held back, level at {home_score}-{away_score} "
                      f"and the plays do not decide it")
                return None

        # The same lag can leave a decided score with no overtime mark: 2026-10-22, a 3-2 overtime win went
        # out without "(OT)" in one channel and with it 13 s later in the other, and neither the header nor
        # the score endpoint named the overtime at the first look. The last goal's own period says it.
        if last_period_type not in ("OT", "SO"):
            from_plays = self._result_from_plays(pbp)
            if from_plays and from_plays[2] in ("OT", "SO") and from_plays[:2] == (home_score, away_score):
                print(f"NHL: game {pbp.get('id')}: FINAL without an overtime mark, "
                      f"the plays say {home_score}-{away_score} {from_plays[2]}; using the plays' mark")
                last_period_type = from_plays[2]

        suffix = f" ({last_period_type})" if last_period_type in ("OT", "SO") else ""

        # Same "Yleisöä" wording as !liiga's FINAL line; left out whenever
        # the figure isn't available (see _fetch_attendance).
        attendance = self._fetch_attendance(pbp)
        attendance_str = f" | Yleisöä: {attendance}" if attendance else ""

        self._safe_send(
            irc_bot, channel,
            f"{self.FINAL_PREFIX} {home} {home_score}-{away_score} {away}{suffix}{attendance_str}",
        )
        return {
            "text": (
                f"{(pbp.get('homeTeam') or {}).get('abbrev') or '?'} {home_score}-{away_score} "
                f"{(pbp.get('awayTeam') or {}).get('abbrev') or '?'}{suffix}"
            ),
            "attendance": attendance,
            "late": False,  # True once the figure arrived after the FINAL: line
            "report": {"season": pbp.get("season"), "id": pbp.get("id")},
            "start": pbp.get("startTimeUTC"),
            "at": time.monotonic(),
        }

    def _result_from_plays(self, pbp):
        """(home, away, "REG"|"OT"|"SO") as the plays decide it, or None when they do not: the running
        score of the last goal, or - when the last goal is a shootout goal - the score before the shootout
        plus one for the side that scored more shootout goals (None while that is level)."""
        goals = self._all_real_goals(pbp)
        if not goals:
            return None

        def period_type(goal):
            return (goal.get("periodDescriptor") or {}).get("periodType")

        def running(goal):
            details = goal.get("details") or {}
            home, away = details.get("homeScore"), details.get("awayScore")
            return (home, away) if isinstance(home, int) and isinstance(away, int) else None

        last = goals[-1]
        if period_type(last) == "SO":
            before = [g for g in goals if period_type(g) != "SO"]
            score = running(before[-1]) if before else (0, 0)
            home_so, away_so = self._shootout_tally(pbp, last)
            if score is None or home_so == away_so:
                return None
            return (score[0] + 1, score[1], "SO") if home_so > away_so else (score[0], score[1] + 1, "SO")
        score = running(last)
        if score is None or score[0] == score[1]:
            return None
        return score[0], score[1], "OT" if period_type(last) == "OT" else "REG"

    def _retry_missing_attendance(self, state, poll_started):
        """Looks again for the attendance of every finished game whose FINAL:
        line went out without it, until ATTENDANCE_RETRY_SECONDS after that
        line. Updates the records in `state` in place. A FINAL: sent in this
        very poll (its record is newer than `poll_started`) was just looked up
        for, so it waits for the next one."""
        now = time.monotonic()
        waiting = [gid for gid, snap in state.items()
                   if self._awaiting_attendance(snap, now) and snap["final"]["at"] < poll_started]
        found = self._fetch_concurrently(waiting, lambda gid: self._fetch_attendance(state[gid]["final"]["report"]))
        for gid, attendance in found.items():
            if attendance:
                state[gid]["final"]["attendance"] = attendance
                state[gid]["final"]["late"] = True
                print(f"NHL: game {gid}: attendance {attendance} found "
                      f"{time.monotonic() - state[gid]['final']['at']:.0f} s after the FINAL line")

    def _awaiting_attendance(self, snap, now) -> bool:
        final = snap.get("final")
        return bool(final) and not final["attendance"] and now - final["at"] < self.ATTENDANCE_RETRY_SECONDS

    def _announce_slate_summary(self, irc_bot, channel, state):
        """One results list with every tracked game's attendance, posted when
        the slate is over - but only if some figure arrived after its FINAL:
        line, since otherwise those lines already said everything (issue #8).
        A figure that never arrived is simply left out."""
        finals = [(snap["slate"], snap["final"]) for snap in state.values() if snap.get("final")]
        if not any(final["late"] for _, final in finals):
            return
        finals.sort(key=lambda item: item[1]["start"] or "")
        label = self._result_date_label(
            {i: {"startTimeUTC": final["start"]} for i, (_, final) in enumerate(finals)}, finals[0][0],
        )
        items = [
            f"{final['text']} ({final['attendance']})" if final["attendance"] else final["text"]
            for _, final in finals
        ]
        for message in self._chunk(f"NHL results {label}: ", items, ", "):
            self._safe_send(irc_bot, channel, message)

    def _official_result(self, game_id, slate):
        """((home, away), lastPeriodType) from /score/{slate} for one game,
        or None when it can't be had (no slate, request failed, game not
        listed, no numeric score) - the caller then keeps the play-by-play
        header's values. lastPeriodType is None when that game carries no
        gameOutcome, which also leaves the header's value alone. Never a
        copy older than FINAL_SCORE_MAX_AGE_SECONDS: this is the one moment
        a 15 s old copy (say, from a `!nhl now`) could still be the stale
        one, while games ending in the same poll can share one request."""
        if not slate or game_id is None:
            return None
        game = (self._fetch_scores(slate, max_age=self.FINAL_SCORE_MAX_AGE_SECONDS) or {}).get(game_id)
        score = self._score_pair(game) if game else None
        if score is None:
            return None
        return score, (game.get("gameOutcome") or {}).get("lastPeriodType")

    def _fetch_attendance(self, pbp):
        """The game's attendance as an int, or None if it can't be had -
        the report may not exist yet right when a game ends (404), NHL may
        change the page, or the request may fail; none of those may stop
        the FINAL: line, which just goes out without it. One request per
        finished game."""
        season, game_id = pbp.get("season"), pbp.get("id")
        if not season or not game_id:
            return None
        url = f"{self.REPORTS_URL}/{season}/GS{str(game_id)[4:]}.HTM"
        try:
            resp = self.session.get(url, timeout=self.REQUEST_TIMEOUT_SECONDS, headers={"Accept": "text/html"})
            resp.raise_for_status()
            match = self.ATTENDANCE_RE.search(resp.text)
        except requests.exceptions.RequestException as e:
            print(f"NHL game report unavailable for game {game_id} (no attendance): {e}")
            return None
        if not match:
            return None
        digits = match.group(1).replace(",", "")
        return int(digits) if digits.isdigit() and int(digits) > 0 else None
