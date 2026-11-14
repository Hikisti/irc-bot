from unittest.mock import MagicMock, patch

import pytest

from liiga_command import LiigaCommand
from liiga_scoreboard import period_status, snapshot_rank
from tests.conftest import make_json_response
from tests.test_liiga_command import FIXED_NOW, make_game

TODAY = FIXED_NOW.strftime("%Y-%m-%d")  # 2026-09-05, 18:00 Helsinki
YESTERDAY = "2026-09-04"

# The periods list as the feed carries it (sampled 2026-11-13): all five, played or not.
PERIODS = [
    {"index": 1, "category": "NORMAL", "startTime": 0, "endTime": 1200},
    {"index": 2, "category": "NORMAL", "startTime": 1200, "endTime": 2400},
    {"index": 3, "category": "NORMAL", "startTime": 2400, "endTime": 3600},
    {"index": 4, "category": "OVERTIME", "startTime": 3600, "endTime": 3900},
    {"index": 5, "category": "WINNING_SHOT_COMPETITION", "startTime": 3900, "endTime": 3900},
]
PLAYOFF_PERIODS = PERIODS[:3] + [
    {"index": 4, "category": "OVERTIME", "startTime": 3600, "endTime": 4800},
    {"index": 5, "category": "OVERTIME", "startTime": 4800, "endTime": 6000},
]


def game(gid=1, home="HIFK", away="Ilves", score=(0, 0), started=True, ended=False, period=1, clock=0,
         finished_type="ACTIVE_OR_NOT_STARTED", start="2026-09-05T14:00:00Z", periods=None):
    g = make_game(gid, home, away, started=started, ended=ended, finished_type=finished_type, start=start)
    g["homeTeam"]["goals"], g["awayTeam"]["goals"] = score
    g["currentPeriod"], g["gameTime"] = period, clock
    g["periods"] = [dict(p) for p in (periods or PERIODS)]
    return g


def upcoming(gid, home, away, start="2026-09-05T16:30:00Z"):
    return game(gid, home, away, started=False, period=0, start=start)


def final(gid, home, away, score, finished_type="ENDED_DURING_REGULAR_GAME_TIME", start="2026-09-05T14:00:00Z"):
    return game(gid, home, away, score, ended=True, period=3, clock=3600, finished_type=finished_type, start=start)


@pytest.fixture
def liiga():
    command = LiigaCommand()
    command._now = lambda: FIXED_NOW
    return command


def feed(liiga, days):
    """Stands in for the five-tournament fetch: {date: (games list, previousGameDate)} or {date: None}."""
    calls = []

    def fetch(date_str, season):
        calls.append(date_str)
        day = days.get(date_str, ([], None))
        if day is None:
            return None, None, None
        games, previous = day
        return {g["id"]: g for g in games}, None, previous

    return patch.object(liiga, "_fetch_games_with_hints", side_effect=fetch), calls


class TestPeriodStatus:
    LABELS = LiigaCommand.PERIOD_LABELS

    @pytest.mark.parametrize("period, clock, expected", [
        (1, 0, "1st 0'"),
        (1, 59, "1st 0'"),
        (2, 1200 + 12 * 60 + 34, "2nd 12'"),
        (3, 3599, "3rd 19'"),
        (4, 3600 + 200, "OT 3'"),
    ])
    def test_minutes_played_in_the_period(self, period, clock, expected):
        assert period_status(game(period=period, clock=clock), self.LABELS) == expected

    @pytest.mark.parametrize("period, clock, expected", [
        (1, 1200, "1st int."),
        (2, 2400, "2nd int."),
        (3, 3600, "3rd end"),
        (4, 3900, "OT end"),
    ])
    def test_the_clock_stopped_at_the_end_of_a_period(self, period, clock, expected):
        assert period_status(game(period=period, clock=clock), self.LABELS) == expected

    def test_a_shootout(self):
        assert period_status(game(period=5, clock=3900), self.LABELS) == "SO"

    def test_a_shootout_without_categories_goes_by_the_period_number(self):
        g = game(period=5, clock=3900)
        for p in g["periods"]:
            p.pop("category")
        assert period_status(g, self.LABELS) == "SO"

    def test_playoff_overtimes_are_numbered(self):
        assert period_status(game(period=4, clock=3700, periods=PLAYOFF_PERIODS), self.LABELS) == "OT 1'"
        assert period_status(game(period=5, clock=5000, periods=PLAYOFF_PERIODS), self.LABELS) == "2OT 3'"

    @pytest.mark.parametrize("period, clock", [(0, 0), (None, 100), (1, None), ("1", 100)])
    def test_nothing_usable_gives_nothing(self, period, clock):
        assert period_status(game(period=period, clock=clock), self.LABELS) == ""

    def test_a_period_missing_from_the_list_shows_its_name(self):
        assert period_status(game(period=2, clock=1500, periods=PERIODS[:1]), self.LABELS) == "2nd"


class TestNewestCopy:
    def test_rank_orders_ended_then_goals_then_clock(self):
        assert snapshot_rank(game(score=(1, 0), clock=500)) > snapshot_rank(game(score=(0, 0), clock=900))
        assert snapshot_rank(game(clock=901)) > snapshot_rank(game(clock=900))
        assert snapshot_rank(final(1, "A", "B", (0, 0))) > snapshot_rank(game(score=(5, 5), clock=3500))

    def test_rank_survives_odd_values(self):
        g = game()
        g["homeTeam"]["goals"], g["gameTime"] = "?", None
        assert snapshot_rank(g) == (False, 0, 0)

    def test_an_older_copy_right_after_a_newer_one_is_not_shown(self, liiga):
        newer, older = game(score=(1, 0), clock=700), game(score=(0, 0), clock=690)
        with patch("liiga_scoreboard.time.monotonic", return_value=100.0):
            assert liiga._newest_copy(1, newer) is newer
        with patch("liiga_scoreboard.time.monotonic", return_value=130.0):
            assert liiga._newest_copy(1, older) is newer

    def test_after_the_window_the_feed_is_trusted_again(self, liiga):
        # A goal taken off the board must not stay on it.
        newer, lower = game(score=(1, 0), clock=700), game(score=(0, 0), clock=800)
        with patch("liiga_scoreboard.time.monotonic", return_value=100.0):
            liiga._newest_copy(1, newer)
        with patch("liiga_scoreboard.time.monotonic", return_value=100.0 + liiga.NEWEST_SNAPSHOT_SECONDS):
            assert liiga._newest_copy(1, lower) is lower

    def test_a_newer_copy_replaces_the_kept_one(self, liiga):
        first, second = game(clock=100), game(clock=160)
        with patch("liiga_scoreboard.time.monotonic", return_value=100.0):
            liiga._newest_copy(1, first)
            assert liiga._newest_copy(1, second) is second

    def test_a_copy_just_as_far_along_is_taken_fresh(self, liiga):
        # Same score and clock, but other fields (spectators) can still have changed.
        first, second = game(clock=100), dict(game(clock=100), spectators=4100)
        with patch("liiga_scoreboard.time.monotonic", return_value=100.0):
            liiga._newest_copy(1, first)
            assert liiga._newest_copy(1, second) is second

    def test_old_entries_are_dropped(self, liiga):
        with patch("liiga_scoreboard.time.monotonic", return_value=100.0):
            liiga._newest_copy(1, game(gid=1))
        with patch("liiga_scoreboard.time.monotonic", return_value=1000.0):
            liiga._newest_copy(2, game(gid=2))
        assert set(liiga._newest_seen) == {2}


class TestFetchDay:
    def test_a_day_is_cached_briefly(self, liiga):
        patcher, calls = feed(liiga, {TODAY: ([game()], YESTERDAY)})
        with patcher, patch("liiga_scoreboard.time.monotonic", return_value=100.0):
            first = liiga._fetch_day(TODAY)
            assert liiga._fetch_day(TODAY) is first
        with patcher, patch("liiga_scoreboard.time.monotonic", return_value=100.0 + liiga.SCORE_CACHE_SECONDS):
            liiga._fetch_day(TODAY)
        assert calls == [TODAY, TODAY]
        assert first[1] == YESTERDAY

    def test_a_failure_is_not_cached(self, liiga):
        patcher, calls = feed(liiga, {TODAY: None})
        with patcher:
            assert liiga._fetch_day(TODAY) is None
            assert liiga._fetch_day(TODAY) is None
        assert calls == [TODAY, TODAY]

    def test_an_unreadable_date_is_none(self, liiga):
        assert liiga._fetch_day("soon") is None

    def test_the_season_follows_the_date_asked_for(self, liiga):
        patcher, _ = feed(liiga, {})
        with patcher as fetch:
            liiga._fetch_day("2026-06-30")
            liiga._fetch_day("2026-07-01")
        assert [c.args[1] for c in fetch.call_args_list] == [2026, 2027]


class TestPreviousGameDate:
    def _get(self, previous_by_tournament):
        def fake_get(url, params=None, timeout=None):
            return make_json_response({"games": [], "previousGameDate": previous_by_tournament.get(params["tournament"])})
        return fake_get

    def test_the_latest_earlier_date_across_tournaments(self, liiga):
        fake = self._get({"runkosarja": "2026-11-11", "valmistavat_ottelut": "2026-08-27"})
        with patch.object(liiga.session, "get", side_effect=fake):
            _, _, previous = liiga._fetch_games_with_hints("2026-11-13", 2027)
        assert previous == "2026-11-11"

    @pytest.mark.parametrize("value", ["2026-11-13", "2026-12-01", None, 20261111])
    def test_a_date_not_earlier_than_the_one_asked_or_not_a_date_is_ignored(self, liiga, value):
        with patch.object(liiga.session, "get", side_effect=self._get({"runkosarja": value})):
            _, _, previous = liiga._fetch_games_with_hints("2026-11-13", 2027)
        assert previous is None

    def test_the_old_two_value_fetch_is_unchanged(self, liiga):
        with patch.object(liiga.session, "get", side_effect=self._get({"runkosarja": "2026-11-11"})):
            assert liiga._fetch_games_and_next_date("2026-11-13", 2027) == ({}, None)


class TestBoard:
    def board(self, liiga, games, **more_days):
        patcher, _ = feed(liiga, {TODAY: (games, YESTERDAY), **more_days})
        with patcher:
            return liiga._board_messages()

    def test_live_final_and_upcoming_in_that_order(self, liiga):
        games = [
            upcoming(3, "JYP", "Lukko"),
            final(2, "Tappara", "Kärpät", (3, 2), finished_type="ENDED_DURING_EXTENDED_GAME_TIME"),
            game(1, "HIFK", "Ilves", (2, 1), period=2, clock=1200 + 754),
        ]
        assert self.board(liiga, games) == [
            "Live: HIFK 2-1 Ilves 2nd 12' || Final: Tappara 3-2 Kärpät (OT) || Upcoming: 19:30 JYP-Lukko"]

    def test_empty_groups_are_left_out(self, liiga):
        assert self.board(liiga, [upcoming(1, "A", "B"), upcoming(2, "C", "D", "2026-09-05T17:00:00Z")]) == [
            "Upcoming: 19:30 A-B | 20:00 C-D"]

    def test_a_game_that_was_not_played_is_said_to_be(self, liiga):
        postponed = upcoming(1, "SaiPa", "Ässät", start="2026-09-05T10:00:00Z")  # its end was 16:00 Helsinki
        assert self.board(liiga, [postponed]) == ["Upcoming: 13:00 SaiPa-Ässät (not played)"]

    def test_a_game_in_its_last_minutes_before_the_feed_ends_it(self, liiga):
        assert self.board(liiga, [game(1, "A", "B", (4, 1), period=3, clock=3600)]) == ["Live: A 4-1 B 3rd end"]

    def test_a_shootout_final(self, liiga):
        g = final(1, "A", "B", (3, 2), finished_type="ENDED_DURING_WINNING_SHOT_COMPETITION")
        assert self.board(liiga, [g]) == ["Final: A 3-2 B (SO)"]

    def test_live_games_are_in_start_order(self, liiga):
        late = game(1, "Late", "X", start="2026-09-05T15:30:00Z", period=1, clock=60)
        early = game(2, "Early", "Y", start="2026-09-05T14:00:00Z", period=2, clock=1300)
        assert self.board(liiga, [late, early]) == ["Live: Early 0-0 Y 2nd 1', Late 0-0 X 1st 1'"]

    def test_a_long_board_is_split_within_the_line_limit(self, liiga):
        games = [final(i, f"Team{i:02d}", f"Other{i:02d}", (i, 0)) for i in range(30)]
        messages = self.board(liiga, games)
        assert len(messages) > 1
        assert all(len(m.encode()) <= liiga.MAX_LINE_BYTES and m.startswith("Final: ") for m in messages)

    def test_no_games_today_points_at_the_next_gameday(self, liiga):
        # The date label reads the real clock (LiveTrackerCommand._format_date_label), so it is fixed here.
        with patch.object(liiga, "_fetch_next_gameday", return_value=("2026-09-06", {9: upcoming(9, "A", "B")})), \
                patch.object(liiga, "_format_date_label", return_value="Sun 06/09"):
            assert self.board(liiga, []) == ["No Liiga games today. Next Liiga gameday (Sun 06/09): 19:30 A-B"]

    def test_no_games_and_no_next_gameday(self, liiga):
        with patch.object(liiga, "_fetch_next_gameday", return_value=(None, None)):
            assert self.board(liiga, []) == ["No Liiga games today."]

    def test_api_unreachable_is_none(self, liiga):
        patcher, _ = feed(liiga, {TODAY: None})
        with patcher:
            assert liiga._board_messages() is None


class TestResults:
    def results(self, liiga, days):
        patcher, calls = feed(liiga, days)
        with patcher:
            return liiga._results_messages(), calls

    def test_todays_finished_games(self, liiga):
        days = {TODAY: ([final(1, "HPK", "Jukurit", (3, 2)), upcoming(2, "A", "B")], YESTERDAY)}
        assert self.results(liiga, days) == (["Liiga results 5.9.: HPK 3-2 Jukurit"], [TODAY])

    def test_the_previous_gameday_when_today_has_none_finished(self, liiga):
        days = {TODAY: ([upcoming(1, "A", "B")], "2026-09-02"),
                "2026-09-02": ([final(7, "TPS", "Jokerit", (4, 3), finished_type="ENDED_DURING_EXTENDED_GAME_TIME")],
                               "2026-08-30")}
        assert self.results(liiga, days) == (["Liiga results 2.9.: TPS 4-3 Jokerit (OT)"], [TODAY, "2026-09-02"])

    def test_games_still_on_are_counted_not_listed(self, liiga):
        days = {TODAY: ([final(1, "A", "B", (1, 0)), game(2, "C", "D", (2, 2), period=3, clock=3000)], YESTERDAY)}
        messages, _ = self.results(liiga, days)
        assert messages == ["Liiga results 5.9.: A 1-0 B (1 game(s) still on: !liiga now)"]

    def test_games_still_on_today_point_at_now_from_the_previous_gameday(self, liiga):
        days = {TODAY: ([game(1, "C", "D", (0, 0), period=1, clock=100)], YESTERDAY),
                YESTERDAY: ([final(2, "A", "B", (2, 1))], None)}
        messages, _ = self.results(liiga, days)
        assert messages == ["Liiga results 4.9.: A 2-1 B (1 game(s) still on: !liiga now)"]

    def test_finished_games_are_in_start_order(self, liiga):
        days = {TODAY: ([final(1, "Late", "X", (1, 0), start="2026-09-05T15:30:00Z"),
                         final(2, "Early", "Y", (0, 1), start="2026-09-05T14:00:00Z")], YESTERDAY)}
        messages, _ = self.results(liiga, days)
        assert messages == ["Liiga results 5.9.: Early 0-1 Y, Late 1-0 X"]

    def test_no_previous_gameday(self, liiga):
        assert self.results(liiga, {TODAY: ([upcoming(1, "A", "B")], None)})[0] == ["No recent Liiga results found."]

    def test_a_previous_gameday_with_nothing_finished(self, liiga):
        days = {TODAY: ([], YESTERDAY), YESTERDAY: ([upcoming(1, "A", "B")], None)}
        assert self.results(liiga, days)[0] == ["No recent Liiga results found."]

    def test_api_unreachable_today_is_none(self, liiga):
        assert self.results(liiga, {TODAY: None})[0] is None

    def test_api_unreachable_for_the_previous_gameday_is_none(self, liiga):
        assert self.results(liiga, {TODAY: ([], YESTERDAY), YESTERDAY: None})[0] is None

    def test_a_long_list_keeps_the_pointer_on_its_last_line(self, liiga):
        games = [final(i, f"Team{i:02d}", f"Other{i:02d}", (i, 0)) for i in range(30)]
        games.append(game(99, "C", "D", period=1, clock=60))
        messages, _ = self.results(liiga, {TODAY: (games, YESTERDAY)})
        assert len(messages) > 1
        assert messages[-1].endswith("(1 game(s) still on: !liiga now)")
        assert all(m.startswith("Liiga results 5.9.: ") for m in messages)

    def test_an_unreadable_date_label_is_shown_as_it_came(self, liiga):
        assert liiga._day_label("someday") == "someday"


class _InlineThread:
    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class TestExecute:
    def run(self, liiga, arg, days):
        bot = MagicMock()
        patcher, _ = feed(liiga, days)
        with patcher, patch("scoreboard.threading.Thread", _InlineThread):
            result = liiga.execute(arg, irc_bot=bot, channel="#liiga")
        return result, bot

    def test_now_sends_the_board(self, liiga):
        result, bot = self.run(liiga, " NOW ", {TODAY: ([upcoming(1, "A", "B")], None)})
        assert result is None
        assert bot.send_message.call_args[0] == ("#liiga", "Upcoming: 19:30 A-B")

    def test_results_sends_the_results(self, liiga):
        _, bot = self.run(liiga, "results", {TODAY: ([final(1, "A", "B", (1, 0))], None)})
        assert bot.send_message.call_args[0] == ("#liiga", "Liiga results 5.9.: A 1-0 B")

    @pytest.mark.parametrize("arg", ["now", "results"])
    def test_api_failure_replies_with_an_error_string(self, liiga, arg):
        _, bot = self.run(liiga, arg, {TODAY: None})
        assert bot.send_message.call_args[0][1] == "Error: could not reach the Liiga API."

    def test_the_tracker_state_is_not_touched(self, liiga):
        liiga._channels["#liiga"] = {"stop_event": MagicMock(), "thread": None, "games": {"kept": True}}
        self.run(liiga, "now", {TODAY: ([game(1, "A", "B", (1, 0), period=1, clock=60)], None)})
        assert liiga._channels["#liiga"]["games"] == {"kept": True}

    @pytest.mark.parametrize("arg", ["now", "results"])
    def test_without_channel_context_it_says_so(self, liiga, arg):
        assert "channel context" in liiga.execute(arg, irc_bot=None, channel=None)

    def test_usage_lists_the_new_subcommands(self, liiga):
        assert liiga.execute("bogus", irc_bot=MagicMock(), channel="#liiga") == (
            "Usage: !liiga start | !liiga stop | !liiga next [team [count]] | !liiga now | !liiga results")
