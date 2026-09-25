import datetime
import traceback

import requests

from irc_format import BOLD, RESET, GREEN, ORANGE, prefix as irc_prefix
from live_tracker_command import LiveTrackerCommand


class LiigaCommand(LiveTrackerCommand):
    """
    Live-tracks today's Finnish Liiga (ice hockey) games in a channel,
    announcing goals and final scores as they happen. Uses the unofficial
    liiga.fi JSON API (the same one that powers the liiga.fi site).

    Usage:
      !liiga start  -> start polling today's games in this channel
      !liiga stop   -> stop polling in this channel
      !liiga next   -> show the next upcoming gameday's games and times

    All network I/O (the initial lookup and every later poll) happens on a
    background thread, never on the caller's thread, so a slow or hanging
    liiga.fi response can't stall the bot's main IRC loop.

    See live_tracker_command.py for the shared start/stop/next lifecycle
    this inherits; only the actual fetching/announcing (_run, _poll_once,
    _fetch_next_period, ...) is Liiga-specific.
    """

    ALIASES = ("!liiga",)
    CHANNELS = ("#smliiga",)

    DISPLAY_NAME = "Liiga"
    COMMAND_NAME = "!liiga"
    CACHE_SLUG = "Liiga"
    TRACKED_NOUN = "games"
    TRACKED_NOUN_COUNTED = "game(s)"
    PERIOD_NOUN = "gameday"
    STATE_KEY = "games"

    BASE_URL = "https://www.liiga.fi/api/v2/games"
    TOURNAMENTS = ["runkosarja", "playoffs", "playout", "qualifications", "valmistavat_ottelut"]

    PERIOD_LABELS = {1: "1st", 2: "2nd", 3: "3rd", 4: "OT", 5: "SO"}

    GOAL_PREFIX = irc_prefix("GOAL:", GREEN)
    FINAL_PREFIX = irc_prefix("FINAL:", ORANGE)

    # ---- "next"/"run" lookup hooks (see LiveTrackerCommand._run_next / _run) --

    def _fetch_next_period(self, context):
        return self._fetch_next_gameday()

    def _format_period_summary(self, items):
        return self._format_games_summary(items)

    def _fetch_today_items(self, context):
        return self._fetch_today_games()

    def _build_initial_state(self, items) -> dict:
        return {gid: self._snapshot(g) for gid, g in items.items()}

    # ---- polling ---------------------------------------------------------

    def _poll_once(self, irc_bot, channel, *context_args) -> bool:
        """Fetch current game states and announce diffs since the last poll.

        Returns True once every tracked game has ended (nothing left to watch).
        """
        games = self._fetch_today_games()
        if games is None:
            return False

        prev_state = self._get_state(channel)
        if prev_state is None:
            return True

        all_ended = bool(games)
        new_state = {}

        for gid, game in games.items():
            try:
                prev = prev_state.get(gid)
                if prev is None:
                    # A game we weren't tracking yet (e.g. added after start).
                    # Seed a baseline silently instead of replaying old goals.
                    new_state[gid] = self._snapshot(game)
                    if not game.get("ended"):
                        all_ended = False
                    continue

                self._announce_new_goals(irc_bot, channel, game, prev, "homeTeam")
                self._announce_new_goals(irc_bot, channel, game, prev, "awayTeam")

                if game.get("ended") and not prev["ended"]:
                    self._announce_end(irc_bot, channel, game)

                new_state[gid] = self._snapshot(game)
                if not game.get("ended"):
                    all_ended = False
            except Exception as e:
                # Don't let one malformed game entry take down the whole poll
                # cycle (or the ones after it) - keep the previous state for
                # this game and try again next cycle. Traceback included
                # since str(e) alone often isn't enough to pinpoint where
                # in the processing this actually broke.
                print(f"Liiga: failed to process game {gid} in {channel}: {e}")
                traceback.print_exc()
                new_state[gid] = prev_state.get(gid) or self._snapshot(game)
                all_ended = False

        self._set_state(channel, new_state)
        return all_ended

    # ---- start-time summary -------------------------------------------

    def _format_games_summary(self, games) -> str:
        """Groups games by scheduled start time, e.g.
        '17:00 HIFK-Ilves, Tappara-Kärpät | 18:30 JYP-Lukko'."""
        return self._format_start_time_summary(
            games, "start",
            lambda g: f"{self._team_name(g, 'homeTeam')}-{self._team_name(g, 'awayTeam')}",
        )

    # ---- announcements --------------------------------------------------

    def _team_name(self, game, side) -> str:
        return (game.get(side) or {}).get("teamName") or "Unknown"

    def _team_goals(self, game, side):
        return (game.get(side) or {}).get("goals", "?")

    def _announce_new_goals(self, irc_bot, channel, game, prev, side):
        events = (game.get(side) or {}).get("goalEvents") or []
        key = "home_goals" if side == "homeTeam" else "away_goals"
        for event in events[prev[key]:]:
            self._safe_send(irc_bot, channel, self._format_goal(game, side, event))

    def _format_goal(self, game, side, event) -> str:
        home = self._team_name(game, "homeTeam")
        away = self._team_name(game, "awayTeam")
        scoring_team = home if side == "homeTeam" else away
        home_score = event.get("homeTeamScore", "?")
        away_score = event.get("awayTeamScore", "?")

        scorer = event.get("scorerPlayer") or {}
        scorer_name = f"{scorer.get('firstName', '')} {scorer.get('lastName', '')}".strip() or "Unknown"

        assists = event.get("assistantPlayers") or []
        assist_names = ", ".join(
            f"{a.get('firstName', '')} {a.get('lastName', '')}".strip() for a in assists
        )
        assist_str = f" (assists: {assist_names})" if assist_names else ""

        tags = event.get("goalTypes") or []
        tag_str = f" ({'/'.join(tags)})" if tags else ""

        period_label = self.PERIOD_LABELS.get(event.get("period"), "")
        clock = self._format_clock(game, event)
        time_str = f" {clock} {period_label}".rstrip() if clock else f" {period_label}".rstrip()

        # Score (bolded) and time/period lead the line, GOAL: only - the
        # number everyone actually wants is the first thing read; scorer
        # and assist detail trail after the pipe.
        return (
            f"{self.GOAL_PREFIX} {BOLD}{home} {home_score}-{away_score} {away}{RESET}"
            f"{time_str} | {scoring_team} — {scorer_name}{tag_str}{assist_str}"
        )

    def _format_clock(self, game, event) -> str:
        game_time = event.get("gameTime")
        period = event.get("period")
        if game_time is None or period is None:
            return ""
        period_start = 0
        for p in game.get("periods") or []:
            if p.get("index") == period:
                period_start = p.get("startTime", 0)
                break
        elapsed = max(0, game_time - period_start)
        minutes, seconds = divmod(elapsed, 60)
        return f"{minutes:02d}:{seconds:02d}"

    def _announce_end(self, irc_bot, channel, game):
        home = self._team_name(game, "homeTeam")
        away = self._team_name(game, "awayTeam")
        home_goals = self._team_goals(game, "homeTeam")
        away_goals = self._team_goals(game, "awayTeam")

        finished = game.get("finishedType") or ""
        if "SHOOTOUT" in finished or "WINNING_SHOT" in finished:
            suffix_from_type = " (SO)"
        elif "OVERTIME" in finished:
            suffix_from_type = " (OT)"
        else:
            suffix_from_type = ""

        # Corroborating source, independent of finishedType's exact enum
        # wording: the periods list itself carries a "category" per
        # period, confirmed live (2701280, Sport-Jokerit,
        # 2026-09-01) to include "OVERTIME"/"WINNING_SHOT_COMPETITION"
        # entries. Checked in addition to finishedType (not instead of)
        # since a decided-but-scoreless overtime period can still appear
        # in the list even when nothing happened in it.
        suffix_from_periods = ""
        for period in game.get("periods") or []:
            category = period.get("category") or ""
            goals = (period.get("homeTeamGoals") or 0) + (period.get("awayTeamGoals") or 0)
            if category == "WINNING_SHOT_COMPETITION" and goals:
                suffix_from_periods = " (SO)"
                break
            if category == "OVERTIME" and goals:
                suffix_from_periods = " (OT)"

        suffix = suffix_from_type or suffix_from_periods
        if suffix_from_type != suffix_from_periods:
            # Diagnostic for a reported case where a real shootout final
            # was announced with no suffix at all - couldn't reproduce
            # the exact stale snapshot after the fact (by the time this
            # was investigated, a fresh fetch of that same game already
            # had a correct/consistent finishedType), so this is here to
            # capture hard evidence if it recurs rather than guessing.
            print(
                f"Liiga: end-suffix mismatch for game {game.get('id')} in {channel}: "
                f"finishedType={finished!r} (-> {suffix_from_type!r}) vs periods (-> {suffix_from_periods!r})"
            )

        spectators = game.get("spectators")
        # Confirmed live (game 2701291, HPK-Ilves, 2026-09-08): matches
        # liiga.fi's own "Yleisöä: N" figure exactly. Omitted when absent
        # (e.g. some preseason/training games don't carry an attendance
        # figure at all) rather than printing a misleading "0".
        attendance = f" | Yleisöä: {spectators}" if isinstance(spectators, (int, float)) else ""

        self._safe_send(
            irc_bot, channel, f"{self.FINAL_PREFIX} {home} {home_goals}-{away_goals} {away}{suffix}{attendance}"
        )

    # ---- data fetching --------------------------------------------------

    def _current_season(self, now_helsinki) -> int:
        # Liiga seasons are labelled by the year they run into (e.g. the
        # 2024-2025 season is season 2025). The July cutoff (not
        # September, when the regular season itself starts) accounts for
        # "valmistavat_ottelut" (preseason games), which already belong
        # to the upcoming season's numbering and start earlier.
        return now_helsinki.year + 1 if now_helsinki.month >= 7 else now_helsinki.year

    def _fetch_today_games(self):
        """Returns {game_id: game_dict} for today, merged across tournaments.
        None only if every tournament request failed."""
        now = datetime.datetime.now(self.HELSINKI_TZ)
        games, _next_date = self._fetch_games_and_next_date(
            now.strftime("%Y-%m-%d"), self._current_season(now)
        )
        return games

    def _fetch_games_and_next_date(self, date_str, season):
        """Fetch every tournament's games for one date, merged together.

        Each tournament is fetched and error-handled independently, so a
        single failing/slow endpoint doesn't discard data already fetched
        from the others. Returns (games_dict, next_game_date):
          - games_dict is None only if every tournament request failed
            (i.e. we have no idea what's happening that day) - callers
            treat that as "API unreachable".
          - next_game_date is the earliest "nextGameDate" reported by any
            tournament for this date (liiga.fi returns this even when a
            date has no games, pointing at the next date that does), or
            None if none of them reported one.

        Confirmed live: once a tournament's own schedule is exhausted
        (e.g. valmistavat_ottelut/preseason ends and runkosarja takes
        over), its "nextGameDate" doesn't go null - it wraps back to
        that tournament's very first date, which can be weeks in the
        past relative to what was actually queried. A hint that isn't
        later than the queried date is exactly that wraparound, not a
        real "next date", so it's discarded rather than fed into
        min(next_dates) alongside a different tournament's real one.
        """
        games = {}
        next_dates = []
        any_success = False

        for tournament in self.TOURNAMENTS:
            try:
                resp = self.session.get(
                    self.BASE_URL,
                    params={"tournament": tournament, "season": season, "date": date_str},
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                data = resp.json()
                for g in (data.get("games") or []):
                    gid = g.get("id")
                    if gid is not None:
                        games[gid] = g
                next_game_date = data.get("nextGameDate")
                if next_game_date and next_game_date > date_str:
                    next_dates.append(next_game_date)
                any_success = True
            except requests.exceptions.RequestException as e:
                print(f"Liiga API request failed for tournament={tournament}: {e}")
            except (ValueError, AttributeError, TypeError) as e:
                print(f"Liiga API returned unexpected data for tournament={tournament}: {e}")

        if not any_success:
            return None, None
        return games, (min(next_dates) if next_dates else None)

    def _fetch_next_gameday(self):
        """Returns (date_str, games_dict) for the closest date (today or
        later) that has games *not all already finished*, or (None, None)
        if that can't be determined (API unreachable, or genuinely
        nothing scheduled)."""
        now = datetime.datetime.now(self.HELSINKI_TZ)
        today_str = now.strftime("%Y-%m-%d")

        games, next_date = self._fetch_games_and_next_date(today_str, self._current_season(now))
        if games is None:
            return None, None
        if games and not self._all_games_ended(games):
            return today_str, games

        # Today has no games, or only ones that have already finished
        # (e.g. checking !liiga next hours after today's games ended) -
        # look forward. Prefer the API's own "nextGameDate" hint when
        # available - it's only given when today's query had no games at
        # all, but can jump weeks ahead (e.g. across an off-season gap),
        # farther than the bounded day-by-day fallback below would reach.
        if next_date:
            try:
                next_dt = datetime.datetime.strptime(next_date, "%Y-%m-%d").replace(tzinfo=self.HELSINKI_TZ)
                hinted_games, _ = self._fetch_games_and_next_date(next_date, self._current_season(next_dt))
                if hinted_games:
                    return next_date, hinted_games
            except ValueError:
                pass

        # No hint (today did have games, just all finished already), or
        # the hinted date turned out empty - fall back to a bounded
        # day-by-day search.
        for offset in range(1, self.NEXT_SEARCH_MAX_DAYS + 1):
            date = now + datetime.timedelta(days=offset)
            date_str = date.strftime("%Y-%m-%d")
            candidate_games, _ = self._fetch_games_and_next_date(date_str, self._current_season(date))
            if candidate_games:
                return date_str, candidate_games

        return None, None

    def _all_games_ended(self, games) -> bool:
        return bool(games) and all(bool(g.get("ended")) for g in games.values())

    def _snapshot(self, game) -> dict:
        return {
            "home_goals": len((game.get("homeTeam") or {}).get("goalEvents") or []),
            "away_goals": len((game.get("awayTeam") or {}).get("goalEvents") or []),
            "ended": bool(game.get("ended")),
        }
