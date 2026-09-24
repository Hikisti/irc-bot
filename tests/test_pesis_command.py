import threading
import time
from unittest.mock import MagicMock, patch

from pesis_command import SuperpesisCommand, YkkospesisCommand
from tests.conftest import join_channel_thread
from tests.pesis_test_helpers import make_match, match_event, run_sub_event, run_sub_event_by_number, sc


class TestLeagueSubclasses:
    """PesisCommand itself is never registered as a command - only its
    subclasses are. This covers just the per-subclass configuration
    wiring (DISPLAY_NAME/SERIES_LEVEL_NAME/CACHE_SLUG/COMMAND_NAME); the
    shared business logic is exercised everywhere else in this file via
    the `sc` (SuperpesisCommand) fixture and applies identically to every
    subclass since none of it references these constants' concrete
    values except through `self`."""

    def test_superpesis_is_configured_for_the_mens_superpesis_league(self):
        sp = SuperpesisCommand()
        assert sp.SERIES_LEVEL_NAME == "Superpesis"
        assert sp.SERIES_NAME == "Miehet"
        assert sp.DISPLAY_NAME == "Superpesis"
        assert sp.COMMAND_NAME == "!superpesis"

    def test_ykkospesis_is_configured_for_the_mens_ykkospesis_league(self):
        yp = YkkospesisCommand()
        assert yp.SERIES_LEVEL_NAME == "Ykköspesis"
        assert yp.SERIES_NAME == "Miehet"
        assert yp.DISPLAY_NAME == "Ykköspesis"
        assert yp.COMMAND_NAME == "!ykkospesis"

    def test_each_subclass_gets_its_own_series_cache_file(self):
        sp = SuperpesisCommand()
        yp = YkkospesisCommand()
        assert sp.SERIES_CACHE_FILE != yp.SERIES_CACHE_FILE
        assert "superpesis" in sp.SERIES_CACHE_FILE
        assert "ykkospesis" in yp.SERIES_CACHE_FILE

    def test_superpesis_cache_file_path_is_unchanged_from_before_multi_league_support(self):
        # Regression test: SERIES_CACHE_FILE moved from a hardcoded class
        # constant to one derived from CACHE_SLUG in __init__ - the
        # already-deployed !superpesis cache file must resolve to the
        # exact same path either way, or a bot restart would silently
        # start paying series-list's full request cost again.
        sp = SuperpesisCommand()
        assert sp.SERIES_CACHE_FILE.endswith(".superpesis_series_cache.json")

    def test_display_name_flows_into_user_facing_messages(self):
        yp = YkkospesisCommand()
        assert "!ykkospesis" in yp.execute("bogus", irc_bot=MagicMock(), channel="#pesis.fi")
        assert "Ykköspesis" in yp._stop("#pesis.fi")

    def test_two_leagues_track_independently_even_in_the_same_channel(self):
        # Instance-level state (_channels, _player_cache, _series_cache)
        # is never shared between subclass instances, so running both
        # trackers in the same IRC channel can't cross-contaminate.
        sp = SuperpesisCommand()
        yp = YkkospesisCommand()
        bot = MagicMock()
        with patch.object(sp, "_resolve_series_id", return_value=None):
            sp._start(bot, "#pesis.fi")
            join_channel_thread(sp, "#pesis.fi")
        assert "#pesis.fi" not in sp._channels  # dropped after the error
        assert yp._channels == {}  # never touched at all


class TestUsage:
    def test_no_args_shows_usage(self, sc):
        assert "Usage:" in sc.execute("", irc_bot=MagicMock(), channel="#pesis.fi")

    def test_unknown_arg_shows_usage(self, sc):
        assert "Usage:" in sc.execute("bogus", irc_bot=MagicMock(), channel="#pesis.fi")

    def test_start_without_context_errors(self, sc):
        assert "Error" in sc.execute("start")


class TestStartDoesNotBlock:
    def test_start_returns_before_series_lookup_completes(self, sc):
        release = threading.Event()

        def slow_resolve():
            release.wait(timeout=2)
            return 2945

        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", side_effect=slow_resolve), \
             patch.object(sc, "_fetch_today_matches", return_value={}):
            start = time.time()
            result = sc.execute("start", irc_bot=bot, channel="#pesis.fi")
            elapsed = time.time() - start

            assert elapsed < 1, "execute() blocked on the network"
            assert "Checking" in result

            release.set()
            join_channel_thread(sc, "#pesis.fi")

        bot.send_message.assert_called_once()

    def test_start_reserves_slot_against_races(self, sc):
        release = threading.Event()

        def slow_resolve():
            release.wait(timeout=2)
            return None

        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", side_effect=slow_resolve):
            sc.execute("start", irc_bot=bot, channel="#pesis.fi")
            second = sc.execute("start", irc_bot=bot, channel="#pesis.fi")
            release.set()
            join_channel_thread(sc, "#pesis.fi")

        assert "Already tracking" in second


class TestRun:
    def test_series_resolution_fails(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}

        with patch.object(sc, "_resolve_series_id", return_value=None):
            sc._run(bot, "#pesis.fi", stop_event)

        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")
        assert "#pesis.fi" not in sc._channels

    def test_matches_fetch_fails(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}

        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_today_matches", return_value=None):
            sc._run(bot, "#pesis.fi", stop_event)

        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")

    def test_no_matches_today(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}

        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_today_matches", return_value={}):
            sc._run(bot, "#pesis.fi", stop_event)

        bot.send_message.assert_called_once_with("#pesis.fi", "No Superpesis matches scheduled today.")

    def test_unexpected_exception_does_not_propagate(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}

        with patch.object(sc, "_resolve_series_id", side_effect=RuntimeError("boom")):
            sc._run(bot, "#pesis.fi", stop_event)  # must not raise

        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")

    def test_seeds_event_baseline_and_reports_matches(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()
        sc._channels["#pesis.fi"] = {"stop_event": stop_event, "thread": None, "matches": {}}
        match = make_match()

        roster = {16802: {1: "A"}}
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_today_matches", return_value={146953: match}), \
             patch.object(sc, "_fetch_match_events", return_value=[{"id": 1}, {"id": 2}]), \
             patch.object(sc, "_fetch_match_roster", return_value=roster), \
             patch.object(sc, "_poll_loop"):
            sc._run(bot, "#pesis.fi", stop_event)

        assert sc._channels["#pesis.fi"]["matches"][146953]["event_count"] == 2
        assert sc._channels["#pesis.fi"]["matches"][146953]["roster"] == roster
        message = bot.send_message.call_args[0][1]
        assert "Tracking 1 Superpesis match" in message
        assert "Manse PP-Hyvinkään Tahko" in message


class TestSeedSnapshot:
    def test_seeds_from_the_current_period_only(self, sc):
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        match["liveResult"] = {
            "finished": False,
            "lastPeriod": 1,
            "runs": [{"home": [4], "away": [2]}, {"home": [0], "away": [1]}],
        }
        snapshot = sc._seed_snapshot(match)
        assert snapshot["period"] == 1
        assert snapshot["period_home_runs"] == 0
        assert snapshot["period_away_runs"] == 1

    def test_defaults_to_period_zero_with_no_live_result(self, sc):
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        snapshot = sc._seed_snapshot(match)
        assert snapshot["period"] == 0
        assert snapshot["period_home_runs"] == 0
        assert snapshot["period_away_runs"] == 0

    def test_seeds_empty_announced_and_ended_periods(self, sc):
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        snapshot = sc._seed_snapshot(match)
        assert snapshot["announced"] == {}
        assert snapshot["ended_periods"] == set()


class TestSeedMatchExtras:
    def test_fills_in_event_count_and_roster_for_every_match(self, sc):
        state = {
            1: {"event_count": 0, "roster": {}, "home_id": 10802, "away_id": 10803},
            2: {"event_count": 0, "roster": {}, "home_id": 10804, "away_id": 10805},
        }
        rosters = {1: {10802: {1: "A"}}, 2: {10804: {1: "B"}}}

        def fake_events(mid):
            return [{"id": i} for i in range(mid * 3)]

        def fake_roster(mid):
            return rosters[mid]

        with patch.object(sc, "_fetch_match_events", side_effect=fake_events), \
             patch.object(sc, "_fetch_match_roster", side_effect=fake_roster):
            sc._seed_match_extras(state)

        assert state[1]["event_count"] == 3
        assert state[2]["event_count"] == 6
        assert state[1]["roster"] == rosters[1]
        assert state[2]["roster"] == rosters[2]
        assert state[1]["announced"] == {}  # no run-shaped content in the fake events
        assert state[1]["ended_periods"] == set()

    def test_seeds_announced_and_ended_periods_from_real_history(self, sc):
        # Regression coverage for the actual purpose of this seeding:
        # a match already in progress when !superpesis start runs must
        # not replay its whole history as fresh RUN:/JAKSO: lines.
        state = {1: {"event_count": 0, "roster": {}, "home_id": 16802, "away_id": 16796}}
        events = [
            match_event(1, team_id=16802, period=0, sub_events=[run_sub_event(1, 16802)]),
            match_event(2, team_id=16802, period=0, sub_events=[run_sub_event(2, 16802)]),
            match_event(3, team_id=16796, period=0, sub_events=[run_sub_event(3, 16796)]),
            match_event(4, team_id=16802, period=0, sub_events=[
                {"texts": [{"type": "event", "text": "Ensimmäinen jakso päättyi"},
                           {"type": "stat", "periodend": 1}]},
            ]),
        ]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_fetch_match_roster", return_value={}):
            sc._seed_match_extras(state)

        assert state[1]["announced"] == {(0, "home"): 2, (0, "away"): 1}
        assert state[1]["ended_periods"] == {0}

    def test_matches_are_fetched_concurrently_not_sequentially(self, sc):
        # If this ran sequentially, 3 matches x 0.1s each would take
        # >=0.3s; concurrently it should take roughly one slot's worth.
        state = {i: {"event_count": 0, "roster": {}, "home_id": 1, "away_id": 2} for i in (1, 2, 3)}

        def slow_events(mid):
            time.sleep(0.1)
            return []

        with patch.object(sc, "_fetch_match_events", side_effect=slow_events), \
             patch.object(sc, "_fetch_match_roster", return_value={}):
            start = time.time()
            sc._seed_match_extras(state)
            elapsed = time.time() - start

        assert elapsed < 0.25

    def test_event_fetch_failure_leaves_event_count_untouched(self, sc):
        state = {1: {"event_count": 5, "roster": {}, "home_id": 1, "away_id": 2}}

        with patch.object(sc, "_fetch_match_events", return_value=None), \
             patch.object(sc, "_fetch_match_roster", return_value={}):
            sc._seed_match_extras(state)

        assert state[1]["event_count"] == 5  # unchanged, not reset to 0

    def test_empty_state_does_nothing(self, sc):
        sc._seed_match_extras({})  # must not raise


class TestStop:
    def test_stop_without_active_tracking(self, sc):
        assert "Not currently tracking" in sc.execute("stop", irc_bot=MagicMock(), channel="#pesis.fi")

    def test_stop_signals_thread_and_clears_state(self, sc):
        release = threading.Event()

        def slow_resolve():
            release.wait(timeout=2)
            return None

        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", side_effect=slow_resolve):
            sc.execute("start", irc_bot=bot, channel="#pesis.fi")
            stop_event = sc._channels["#pesis.fi"]["stop_event"]

            result = sc.execute("stop", irc_bot=bot, channel="#pesis.fi")

            assert "Stopped" in result
            assert stop_event.is_set()
            assert "#pesis.fi" not in sc._channels
            release.set()


class TestNext:
    def test_next_returns_immediately_without_blocking(self, sc):
        release = threading.Event()

        def slow_next():
            release.wait(timeout=2)
            return "found", "2026-08-28", {1: make_match(mid=1)}

        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", side_effect=slow_next):
            start = time.time()
            result = sc.execute("next", irc_bot=bot, channel="#pesis.fi")
            elapsed = time.time() - start

            assert elapsed < 1, "execute() blocked on the network"
            assert "Checking" in result

            release.set()
            time.sleep(0.2)  # let the one-shot background thread finish

        bot.send_message.assert_called_once()

    def test_next_without_context_errors(self, sc):
        assert "Error" in sc.execute("next")

    def test_run_next_reports_matches_and_date_label(self, sc):
        bot = MagicMock()
        matches = {1: make_match(mid=1, home="Sotkamon Jymy", away="Joensuun Maila")}
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", return_value=("found", "2026-08-28", matches)), \
             patch.object(sc, "_format_date_label", return_value="tomorrow"):
            sc._run_next(bot, "#pesis.fi")

        message = bot.send_message.call_args[0][1]
        assert "Next Superpesis matchday (tomorrow)" in message
        assert "Sotkamon Jymy-Joensuun Maila" in message

    def test_run_next_series_lookup_fails(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=None):
            sc._run_next(bot, "#pesis.fi")
        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")

    def test_run_next_series_lookup_raises_does_not_propagate(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", side_effect=RuntimeError("boom")):
            sc._run_next(bot, "#pesis.fi")  # must not raise
        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")

    def test_run_next_api_error(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", return_value=("error", None, None)):
            sc._run_next(bot, "#pesis.fi")
        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")

    def test_run_next_nothing_found(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", return_value=("not_found", None, None)):
            sc._run_next(bot, "#pesis.fi")
        message = bot.send_message.call_args[0][1]
        assert "No upcoming Superpesis matches found" in message
        assert str(sc.NEXT_SEARCH_MAX_DAYS) in message

    def test_run_next_unexpected_exception_does_not_propagate(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_resolve_series_id", return_value=2945), \
             patch.object(sc, "_fetch_next_matchday", side_effect=RuntimeError("boom")):
            sc._run_next(bot, "#pesis.fi")  # must not raise
        bot.send_message.assert_called_once_with("#pesis.fi", "Error: could not reach the Superpesis API.")


class TestDateLabel:
    def test_today(self, sc):
        import datetime
        today = datetime.datetime.now(sc.HELSINKI_TZ).strftime("%Y-%m-%d")
        assert sc._format_date_label(today) == "today"

    def test_tomorrow(self, sc):
        import datetime
        tomorrow = (datetime.datetime.now(sc.HELSINKI_TZ) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        assert sc._format_date_label(tomorrow) == "tomorrow"

    def test_other_date_shows_weekday_and_date(self, sc):
        import datetime
        far_future = datetime.datetime.now(sc.HELSINKI_TZ) + datetime.timedelta(days=10)
        label = sc._format_date_label(far_future.strftime("%Y-%m-%d"))
        assert far_future.strftime("%d/%m") in label

    def test_malformed_date_falls_back_to_raw_string(self, sc):
        assert sc._format_date_label("not-a-date") == "not-a-date"


class TestMatchesSummary:
    def test_groups_by_start_time(self, sc):
        matches = [
            make_match(mid=1, home="A", away="B", date="2026-08-24T14:00:00.000000Z"),
            make_match(mid=2, home="C", away="D", date="2026-08-24T14:00:00.000000Z"),
            make_match(mid=3, home="E", away="F", date="2026-08-24T15:30:00.000000Z"),
        ]
        summary = sc._format_matches_summary(matches)
        assert summary == "17:00 A-B, C-D | 18:30 E-F"

    def test_missing_date_sorts_last(self, sc):
        matches = [
            make_match(mid=1, home="A", away="B", date=None),
            make_match(mid=2, home="C", away="D", date="2026-08-24T14:00:00.000000Z"),
        ]
        summary = sc._format_matches_summary(matches)
        assert summary == "17:00 C-D | ??:?? A-B"


class TestAnnouncePeriodEnds:
    def test_already_ended_period_is_not_reannounced(self, sc):
        bot = MagicMock()
        ended_periods = {0}
        sc._announce_period_ends(
            bot, "#pesis.fi", "A", "B",
            period_end_by_period={0: "Ensimmäinen jakso päättyi"},
            ended_periods=ended_periods,
            announced={},
        )
        bot.send_message.assert_not_called()
        assert ended_periods == {0}  # unchanged, not re-added

    def test_new_period_end_is_announced_and_recorded(self, sc):
        bot = MagicMock()
        ended_periods = set()
        sc._announce_period_ends(
            bot, "#pesis.fi", "A", "B",
            period_end_by_period={0: "Ensimmäinen jakso päättyi"},
            ended_periods=ended_periods,
            announced={(0, "home"): 2, (0, "away"): 1},
        )
        bot.send_message.assert_called_once()
        message = bot.send_message.call_args[0][1]
        assert "Ensimmäinen jakso päättyi" in message
        assert "A 2-1 B" in message
        assert ended_periods == {0}


class TestProcessMatch:
    def _prev(self, **overrides):
        prev = {
            "match_id": 146953,
            "home_id": 16802,
            "away_id": 16796,
            "home_name": "Manse PP",
            "away_name": "Hyvinkään Tahko",
            "period": 0,
            "period_home_runs": 0,
            "period_away_runs": 0,
            "event_count": 0,
            "finished": False,
            "roster": {},
        }
        prev.update(overrides)
        return prev

    def test_number_based_scorer_is_resolved_via_roster(self, sc):
        # Regression test for the real bug: a jersey-number-only player
        # ref must resolve through the match's roster, not get treated
        # as a global player id (which resolved to a real but completely
        # unrelated person in production).
        bot = MagicMock()
        roster = {16798: {1: "Konsta Piironen"}}
        prev = self._prev(match_id=146949, home_id=16798, away_id=16804, roster=roster)
        match = make_match(mid=146949, home_id=16798, away_id=16804)
        events = [match_event(1, team_id=16798, sub_events=[run_sub_event_by_number(1, 16798)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name") as mock_global_lookup:
            sc._process_match(bot, "#pesis.fi", match, prev)

        mock_global_lookup.assert_not_called()  # must never treat "1" as a global id
        message = bot.send_message.call_args[0][1]
        assert "Konsta Piironen" in message

    def test_batter_and_scorer_both_shown_when_different(self, sc):
        bot = MagicMock()
        roster = {16798: {1: "Konsta Piironen", 4: "Joosua Rättö"}}
        prev = self._prev(match_id=146949, home_id=16798, away_id=16804, roster=roster)
        match = make_match(mid=146949, home_id=16798, away_id=16804)
        events = [match_event(
            1, team_id=16798, batter=4,
            sub_events=[run_sub_event_by_number(1, 16798)],
        )]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            sc._process_match(bot, "#pesis.fi", match, prev)

        message = bot.send_message.call_args[0][1]
        assert "Joosua Rättö → Konsta Piironen" in message

    def test_real_match_146950_reproduces_all_three_reported_runs(self, sc):
        # End-to-end regression test built directly from the real incident
        # report: three runs from pesistulokset.fi's own match page
        # (kunnari, regular hit, wild throw), only the middle one detected
        # (and with the wrong score) before this fix.
        bot = MagicMock()
        roster = {16804: {1: "Iivari Vihanto", 3: "Kalle Kuosmanen", 10: "Roope Korhonen", 11: "Elmeri Purmonen"}}
        prev = self._prev(match_id=146950, home_id=16804, away_id=16798,
                           home_name="Sotkamon Jymy", away_name="Joensuun Maila", roster=roster)
        match = make_match(mid=146950, home_id=16804, away_id=16798,
                            home="Sotkamon Jymy", away="Joensuun Maila")
        events = [
            match_event(1, team_id=16804, batter=1, sub_events=[
                {"texts": [{"team": 16804, "type": "player", "number": 1}, "jätettiin välistä"]},
                {"texts": [{"team": 16804, "type": "player", "number": 1},
                           {"type": "event", "text": "eteni", "base": 0}, "ykköspesälle"]},
                {"texts": [{"team": 16804, "type": "player", "number": 1},
                           {"type": "event", "text": "eteni", "base": 1}, "kakkospesälle"]},
                {"texts": [{"team": 16804, "type": "player", "number": 1},
                           {"type": "event", "text": "löi kunnarin!", "base": 2},
                           {"type": "stat", "homerun": 2}]},
            ]),
            match_event(2, team_id=16804, batter=10, sub_events=[
                run_sub_event_by_number(3, 16804),
            ]),
            match_event(3, team_id=16804, batter=7, sub_events=[
                {"texts": [{"team": 16804, "type": "player", "number": 11},
                           {"type": "event", "text": "eteni harhaheitolla", "base": 3},
                           "kotipesään", {"type": "stat", "wtscore": 3}]},
            ]),
        ]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            sc._process_match(bot, "#pesis.fi", match, prev)

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        assert len(messages) == 3
        assert "Iivari Vihanto | Sotkamon Jymy 1-0 Joensuun Maila" in messages[0]
        assert "lyöjä" not in messages[0]  # batter == scorer for the kunnari
        assert "Roope Korhonen → Kalle Kuosmanen | Sotkamon Jymy 2-0 Joensuun Maila" in messages[1]
        assert "Harhaheitto → Elmeri Purmonen | Sotkamon Jymy 3-0 Joensuun Maila" in messages[2]

    def test_real_match_147206_duplicated_segment_is_not_double_counted(self, sc):
        # Regression test for a real incident, confirmed via a diagnostic
        # log line ("home score snap ... 4 -> 3") and independently by
        # re-fetching the match's actual event feed: a whole segment of
        # already-counted plays was re-appended later in the events array
        # at different positions - _group_runs_and_period_ends()'s
        # intra-poll duplicate check must collapse the byte-identical
        # re-appended sub-event back down to a single real play.
        bot = MagicMock()
        roster = {16804: {1: "Iivari Vihanto", 4: "Hannes Pekkinen"}}
        prev = self._prev(match_id=147206, home_id=16804, away_id=16801,
                           home_name="Sotkamon Jymy", away_name="Kouvolan Pallonlyöjät", roster=roster)
        match = make_match(mid=147206, home_id=16804, away_id=16801,
                            home="Sotkamon Jymy", away="Kouvolan Pallonlyöjät")

        # The real run: Hannes Pekkinen (batter) -> Iivari Vihanto (scorer).
        real_run_sub_event = {
            "texts": [{"team": 16804, "type": "player", "number": 1},
                      {"type": "event", "text": "eteni", "base": 3}, "kotipesään",
                      {"type": "stat", "score": 3}],
            "runnersAtBases": [None, None, None, None, 1],
        }
        # Same events array the whole match sees in one poll: the real
        # play, then (much later, different array position, but byte-
        # identical content) the server re-appending that same play.
        events = [
            match_event(1, team_id=16804, batter=4, period=0, sub_events=[real_run_sub_event]),
            match_event(2, team_id=16804, batter=99, period=0, sub_events=[  # unrelated in between
                {"texts": ["1. lyönti", {"type": "hit", "hit": None}]},
            ]),
            match_event(3, team_id=16804, batter=4, period=0, sub_events=[real_run_sub_event]),  # re-appended
        ]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        assert len(messages) == 1  # not 2
        assert "Sotkamon Jymy 1-0 Kouvolan Pallonlyöjät" in messages[0]  # not 2-0
        assert new_state["period_home_runs"] == 1

    def test_real_match_147206_runs_appended_to_an_already_seen_event_are_not_dropped(self, sc):
        # Regression test for a real incident (pesistulokset.fi match
        # 147206, "2. jakso"): a batter's whole turn is ONE outer event
        # whose own "events" sub-array grows in place over several polls
        # as the play develops (confirmed live: the same object's
        # "updated" timestamp kept changing while its array index
        # didn't). Under the old position-based design this could
        # permanently drop runs appended after their event was already
        # marked "seen"; under the current count-based design
        # (_group_runs_and_period_ends()) every poll fully re-derives how
        # many run-shaped sub-events exist right now, so growth is always
        # picked up regardless of when it happened - this also verifies
        # the "already announced" count from an earlier poll (when the
        # event had grown zero runs yet) correctly carries forward
        # without re-announcing anything already sent.
        bot = MagicMock()
        roster = {16804: {1: "Iivari Vihanto", 2: "Jere Vikström", 3: "Kalle Kuosmanen", 4: "Hannes Pekkinen",
                           10: "Roope Korhonen", 11: "Elmeri Purmonen"}}
        # One real batter's turn (id=14 in the actual feed): three
        # separate runners reach "kotipesään" inside it - #2, #3, then
        # #4 (the batter himself) via "löi kunnarin!" - interleaved with
        # non-scoring "eteni ... kolmospesälle/kakkospesälle" advances,
        # exactly as pesistulokset.fi's own play-by-play recorded it.
        # An unrelated real run that already happened (and was already
        # announced) earlier in the same period, present in every fetch
        # from here on exactly as it would really be.
        earlier_run = match_event(13, team_id=16804, batter=1, period=1, sub_events=[
            run_sub_event_by_number(1, 16804),
        ])
        growing_event = match_event(14, team_id=16804, batter=4, period=1, sub_events=[
            {"texts": ["3. lyönti", {"type": "hit", "hit": {"out": False}}]},
        ])
        match = make_match(mid=147206, home_id=16804, away_id=16801,
                            home="Sotkamon Jymy", away="Kouvolan Pallonlyöjät")
        prev = self._prev(match_id=147206, home_id=16804, away_id=16801,
                           home_name="Sotkamon Jymy", away_name="Kouvolan Pallonlyöjät",
                           roster=roster, period=1, announced={(1, "home"): 1})
        with patch.object(sc, "_fetch_match_events", return_value=[earlier_run, growing_event]):
            prev = sc._process_match(bot, "#pesis.fi", match, prev)
        assert bot.send_message.call_count == 0  # no new runs yet, nothing to announce

        # Second poll: the SAME event (same array position) has grown to
        # include all three runs, and a genuinely new event (Roope
        # Korhonen driving in Elmeri Purmonen) has also appeared.
        grown_event = match_event(14, team_id=16804, batter=4, period=1, sub_events=[
            {"texts": ["3. lyönti", {"type": "hit", "hit": {"out": False}}]},
            {"texts": [{"team": 16804, "type": "player", "number": 2},
                       {"type": "event", "text": "eteni", "base": 2}, "kolmospesälle"]},
            {"texts": [{"team": 16804, "type": "player", "number": 2},
                       {"type": "event", "text": "eteni", "base": 3}, "kotipesään",
                       {"type": "stat", "score": 3}]},
            {"texts": [{"team": 16804, "type": "player", "number": 3},
                       {"type": "event", "text": "eteni", "base": 2}, "kolmospesälle"]},
            {"texts": [{"team": 16804, "type": "player", "number": 3},
                       {"type": "event", "text": "eteni", "base": 3}, "kotipesään",
                       {"type": "stat", "score": 3}]},
            {"texts": [{"team": 16804, "type": "player", "number": 4},
                       {"type": "event", "text": "löi kunnarin!", "base": 2},
                       {"type": "stat", "homerun": 2}]},
        ])
        roope_event = match_event(15, team_id=16804, batter=10, period=1, sub_events=[
            run_sub_event_by_number(11, 16804),
        ])

        with patch.object(sc, "_fetch_match_events", return_value=[earlier_run, grown_event, roope_event]):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        assert len(messages) == 4  # Jere Vikström, Kalle Kuosmanen, Hannes Pekkinen's kunnari, Roope->Elmeri
        assert "Hannes Pekkinen → Jere Vikström | Sotkamon Jymy 2-0" in messages[0]
        assert "Hannes Pekkinen → Kalle Kuosmanen | Sotkamon Jymy 3-0" in messages[1]
        assert "Hannes Pekkinen | Sotkamon Jymy 4-0" in messages[2]  # kunnari: batter == scorer
        # The real incident report: this line showed "6-0" before this
        # fix (double-counted on top of a snap that had silently absorbed
        # the three dropped runs above) instead of the real "5-0".
        assert "Sotkamon Jymy 5-0 Kouvolan Pallonlyöjät" in messages[3]
        assert new_state["period_home_runs"] == 5

    def test_real_match_147201_a_run_that_would_exceed_the_authoritative_total_is_held_back(self, sc):
        # Regression test for a real incident (pesistulokset.fi match
        # 147201, "1. jakso" and "2. jakso" both affected): pesistulokset.fi
        # apparently retracts and reissues a play mid-game (confirmed live:
        # the real "4. lopettava" run ended up announced twice, first as
        # "Perttu Ruuska -> Aapo Komulainen" then, minutes later, as the
        # actually-correct "Jukka-Pekka Vainionpää -> Aapo Komulainen" -
        # same real point, two different batters). Without a cap, that
        # risks an impossible score (higher than pesistulokset.fi's own
        # authoritative period total - the real period ended 6-5, but the
        # bot announced "6-6"). The authoritative total is always
        # eventually correct (confirmed against this same match's real
        # final result: "6 - 5, 6 - 5"), so a detected run beyond what it
        # confirms must be held back rather than shown.
        bot = MagicMock()
        roster = {16802: {1: "Jukka-Pekka Vainionpää", 5: "Aapo Komulainen"}}
        prev = self._prev(match_id=147201, home_id=16805, away_id=16802,
                           home_name="Vimpelin Veto", away_name="Manse PP, Tampere",
                           roster=roster, period=0, announced={(0, "away"): 1})
        match = make_match(mid=147201, home_id=16805, away_id=16802,
                            home="Vimpelin Veto", away="Manse PP, Tampere")
        match["liveResult"]["runs"] = [{"home": [0], "away": [1]}]  # authoritative: still just 1

        already_counted_run = match_event(49, team_id=16802, batter=1, period=0, sub_events=[
            run_sub_event_by_number(5, 16802),
        ])
        # A second, distinct-content sub-event for the same (period, side)
        # - simulating the "retracted and reissued" real play, which the
        # authoritative total hasn't caught up to yet.
        ghost_run = match_event(50, team_id=16802, batter=10, period=0, sub_events=[
            {"texts": [{"team": 16802, "type": "player", "number": 5},
                       {"type": "event", "text": "eteni", "base": 3}, "kotipesään",
                       {"type": "stat", "score": 99}]},
        ])

        with patch.object(sc, "_fetch_match_events", return_value=[already_counted_run, ghost_run]), \
             patch("builtins.print") as mock_print:
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()  # no RUN: line for the ghost
        assert new_state["period_away_runs"] == 1  # not bumped to 2
        assert any("holding back" in str(c.args[0]) for c in mock_print.call_args_list)

    def test_held_back_run_is_announced_once_authoritative_catches_up(self, sc):
        # A run held back for exceeding the authoritative total isn't
        # counted as "announced" - if the hold-back was instead just the
        # authoritative source lagging a poll behind a genuinely new run,
        # the next poll (once authoritative rises to match) must still
        # count and announce it, not lose it forever.
        bot = MagicMock()
        roster = {16802: {5: "Aapo Komulainen"}}
        run_event = match_event(50, team_id=16802, batter=10, period=0, sub_events=[
            run_sub_event_by_number(5, 16802),
        ])

        # First poll: authoritative hasn't caught up yet - held back.
        prev = self._prev(match_id=147201, home_id=16805, away_id=16802, roster=roster, period=0)
        match = make_match(mid=147201, home_id=16805, away_id=16802)
        match["liveResult"]["runs"] = [{"home": [0], "away": [0]}]
        with patch.object(sc, "_fetch_match_events", return_value=[run_event]):
            prev = sc._process_match(bot, "#pesis.fi", match, prev)
        assert bot.send_message.call_count == 0
        assert prev["period_away_runs"] == 0

        # Second poll: authoritative has now risen to 1 - the exact same
        # event (still present in the events array) must be picked up.
        match["liveResult"]["runs"] = [{"home": [0], "away": [1]}]
        with patch.object(sc, "_fetch_match_events", return_value=[run_event]):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_called_once()
        assert new_state["period_away_runs"] == 1

    def test_real_match_147207_a_run_for_an_earlier_period_is_tracked_independently(self, sc):
        # Regression test for a real incident (pesistulokset.fi match
        # 147207): a run event tagged period=0 (jakso 1) showed up in the
        # feed well after jakso 1's own JAKSO: end had already been
        # announced and jakso 2 was already in progress - a correction
        # appended out of period order (confirmed live: the settled
        # events array itself is cleanly period-ordered, so this only
        # shows up mid-poll, not in a post-game fetch). Every (period,
        # side) is tracked as its own independent bucket (see
        # _group_runs_and_period_ends()), so a late run for an earlier
        # period is simply counted into that period's own bucket without
        # touching jakso 2's real, active running score at all.
        bot = MagicMock()
        roster = {16804: {2: "Aapo Hiltunen", 3: "Iivari Vihanto"}}
        prev = self._prev(match_id=147207, home_id=16801, away_id=16804,
                           home_name="Kouvolan Pallonlyöjät", away_name="Sotkamon Jymy",
                           roster=roster, period=1, announced={(1, "home"): 1, (1, "away"): 1})
        match = make_match(mid=147207, home_id=16801, away_id=16804,
                            home="Kouvolan Pallonlyöjät", away="Sotkamon Jymy")
        match["liveResult"]["runs"] = [{"home": [2], "away": [2]}, {"home": [1], "away": [1]}]

        late_jakso1_run = match_event(99, team_id=16804, batter=2, period=0, sub_events=[
            run_sub_event_by_number(3, 16804),
        ])

        with patch.object(sc, "_fetch_match_events", return_value=[late_jakso1_run]):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        message = bot.send_message.call_args[0][1]
        assert "(1. jakso)" in message  # labeled with its own real period
        # Scoped to jakso 1's own (previously-untouched) bucket, not
        # jakso 2's running score.
        assert "Kouvolan Pallonlyöjät 0-1 Sotkamon Jymy" in message
        # jakso 2 (the active period) is completely untouched.
        assert new_state["period"] == 1
        assert new_state["period_home_runs"] == 1
        assert new_state["period_away_runs"] == 1
        assert new_state["announced"][(0, "away")] == 1

    def test_roster_is_carried_forward_unchanged(self, sc):
        bot = MagicMock()
        roster = {16802: {1: "A"}, 16796: {1: "B"}}
        prev = self._prev(roster=roster)
        match = make_match(mid=146953, home_id=16802, away_id=16796)

        with patch.object(sc, "_fetch_match_events", return_value=None):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["roster"] == roster

    def test_period_end_is_announced(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=16802, period=0, sub_events=[
            {"texts": [{"type": "event", "text": "Ensimmäinen jakso päättyi"},
                       {"type": "stat", "periodend": 1}]},
        ])]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            sc._process_match(bot, "#pesis.fi", match, prev)

        message = bot.send_message.call_args[0][1]
        assert message.startswith(sc.PERIOD_END_PREFIX)
        assert "Ensimmäinen jakso päättyi" in message

    def test_new_run_is_announced_and_scored(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=16802, sub_events=[run_sub_event(111, 16802)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["event_count"] == 1
        message = bot.send_message.call_args[0][1]
        assert "RUN:" in message
        assert "Test Player" in message

    def test_score_is_independent_across_a_period_boundary(self, sc):
        # The actual reported bug: a run in the 2nd period was showing
        # the match-wide cumulative score (e.g. 4-3, continuing from
        # jakso 1's 4-2) instead of restarting at 0 for the new period -
        # every (period, side) is its own independent bucket (see
        # _group_runs_and_period_ends()), so jakso 1's 4-2 has no bearing
        # on jakso 2's own count at all.
        bot = MagicMock()
        prev = self._prev(event_count=0, period=0, announced={(0, "home"): 4, (0, "away"): 2})
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=16796, period=1, sub_events=[run_sub_event(111, 16796)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            sc._process_match(bot, "#pesis.fi", match, prev)

        message = bot.send_message.call_args[0][1]
        assert "Manse PP 0-1 Hyvinkään Tahko" in message  # not 4-3
        assert "(2. jakso)" in message

    def test_period_field_follows_the_authoritative_lastPeriod(self, sc):
        # "period" (used for RUN:/JAKSO: display continuity) is driven
        # purely by pesistulokset.fi's own authoritative "lastPeriod" -
        # not by which events happen to get scanned, which would make it
        # vulnerable to the same out-of-order-array wrinkle as everything
        # else (see match 147207 in the class docstring).
        bot = MagicMock()
        prev = self._prev(event_count=0, period=0, announced={(0, "home"): 4, (0, "away"): 2})
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        match["liveResult"]["lastPeriod"] = 1
        events = [match_event(1, team_id=16796, period=1, sub_events=[run_sub_event(111, 16796)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["period"] == 1
        assert new_state["period_home_runs"] == 0
        assert new_state["period_away_runs"] == 1

    def test_period_field_falls_back_to_prev_without_lastPeriod(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0, period=2)
        match = make_match(mid=146953, home_id=16802, away_id=16796)  # no liveResult.lastPeriod

        with patch.object(sc, "_fetch_match_events", return_value=[]):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["period"] == 2

    def test_period_end_uses_the_ending_periods_own_score_not_reset(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0, period=0, period_home_runs=0, period_away_runs=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [
            match_event(1, team_id=16802, period=0, sub_events=[run_sub_event(111, 16802)]),
            match_event(2, team_id=16802, period=0, sub_events=[
                {"texts": [{"type": "event", "text": "Ensimmäinen jakso päättyi"},
                           {"type": "stat", "periodend": 1}]},
            ]),
        ]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            sc._process_match(bot, "#pesis.fi", match, prev)

        messages = [c[0][1] for c in bot.send_message.call_args_list]
        period_end_msg = next(m for m in messages if m.startswith(sc.PERIOD_END_PREFIX))
        assert "Manse PP 1-0 Hyvinkään Tahko" in period_end_msg

    def test_score_is_never_inflated_past_what_was_actually_announced(self, sc):
        # The authoritative total (3) is a ceiling, never a floor to snap
        # up to: with only 1 run actually detectable in the raw feed (a
        # disclosed possibility - see the class docstring's "Only runs
        # are surfaced" note), the displayed count must stay at 1, not
        # get inflated to a number with no announced RUN: line behind it.
        bot = MagicMock()
        prev = self._prev(event_count=0, period=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=3, away_runs=0)
        events = [match_event(1, team_id=16802, period=0, sub_events=[run_sub_event(111, 16802)])]

        with patch.object(sc, "_fetch_match_events", return_value=events), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert len(bot.send_message.call_args_list) == 1  # only the one real detected run
        assert new_state["period_home_runs"] == 1

    def test_no_new_events_sends_nothing(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=5)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=0, away_runs=0)

        with patch.object(sc, "_fetch_match_events", return_value=[{"id": i} for i in range(5)]):
            sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()

    def test_finish_transition_sends_final(self, sc):
        bot = MagicMock()
        # FINAL is headlined by jaksovoitot (periods won) with a
        # per-period run breakdown, unlike RUN/JAKSO which are scoped to
        # the current period - see TestFormatFinal for the format itself.
        prev = self._prev(event_count=0, finished=False)
        match = make_match(mid=146953, home_id=16802, away_id=16796, finished=True)
        match["liveResult"]["runs"] = [{"home": [2], "away": [1]}, {"home": [0], "away": [0]}]
        match["liveResult"]["periods"] = {"home": 1, "away": 0}

        with patch.object(sc, "_fetch_match_events", return_value=None):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["finished"] is True
        message = bot.send_message.call_args[0][1]
        assert message.startswith(sc.FINAL_PREFIX)
        assert "Manse PP - Hyvinkään Tahko 1 - 0 (2 - 1, 0 - 0)" in message

    def test_already_finished_does_not_resend_final(self, sc):
        bot = MagicMock()
        prev = self._prev(finished=True, period_home_runs=2, period_away_runs=1)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=2, away_runs=1, finished=True)

        with patch.object(sc, "_fetch_match_events", return_value=None):
            sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()

    def test_already_finished_match_is_never_touched_again(self, sc):
        # Regression test for a real incident: the event feed kept
        # appending events (apparent corrections) well after "Ottelu
        # päättyi" had already fired and FINAL: had already been sent,
        # producing contradictory RUN:/JAKSO: lines for a finished match.
        # Once finished, a match must be skipped entirely - not just have
        # its messages suppressed - so no amount of new "corrected" data
        # can produce any announcement for it again.
        bot = MagicMock()
        prev = self._prev(finished=True, period_home_runs=2, period_away_runs=1)
        match = make_match(mid=146953, home_id=16802, away_id=16796, home_runs=2, away_runs=1, finished=True)

        with patch.object(sc, "_fetch_match_events") as mock_fetch_events:
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        mock_fetch_events.assert_not_called()
        bot.send_message.assert_not_called()
        assert new_state is prev

    def test_events_fetch_failure_does_not_crash(self, sc):
        bot = MagicMock()
        prev = self._prev()
        match = make_match(mid=146953, home_id=16802, away_id=16796)

        with patch.object(sc, "_fetch_match_events", return_value=None):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)  # must not raise

        assert new_state["event_count"] == prev["event_count"]

    def test_event_count_baseline_never_regresses(self, sc):
        # Regression test for a real incident: a transiently shorter
        # events array on one poll (e.g. a flaky/incomplete API response)
        # must not lower the stored baseline - otherwise a later poll,
        # once the array recovers, re-slices already-announced events as
        # "new" and re-sends them. Observed live as an exact duplicate
        # RUN: message with the score inflated for every play after it,
        # until the next period-end self-correction.
        bot = MagicMock()
        prev = self._prev(event_count=10)
        match = make_match(mid=146953, home_id=16802, away_id=16796)

        # A poll that (for whatever reason) sees fewer events than the
        # stored baseline - must not shrink event_count below 10.
        with patch.object(sc, "_fetch_match_events", return_value=[{"id": i} for i in range(3)]):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        assert new_state["event_count"] == 10
        bot.send_message.assert_not_called()

    def test_run_not_reannounced_after_a_transient_array_shrink(self, sc):
        # Full reproduction of the reported sequence: poll 1 processes a
        # run; poll 2 (transiently) sees a shorter array than poll 1 did;
        # poll 3 sees the array back to its full (or longer) length.
        # Without the monotonic guard, poll 3 would re-slice and
        # re-announce the run poll 1 already sent.
        bot = MagicMock()
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        run_event = match_event(1, team_id=16802, period=0, sub_events=[run_sub_event(111, 16802)])

        prev = self._prev(event_count=0, period=0)
        with patch.object(sc, "_fetch_match_events", return_value=[run_event]), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            prev = sc._process_match(bot, "#pesis.fi", match, prev)
        assert prev["event_count"] == 1
        assert bot.send_message.call_count == 1

        # Poll 2: transiently shorter (e.g. flaky response) - must not
        # crash or lower the baseline, and must not re-announce anything.
        with patch.object(sc, "_fetch_match_events", return_value=[]):
            prev = sc._process_match(bot, "#pesis.fi", match, prev)
        assert prev["event_count"] == 1
        assert bot.send_message.call_count == 1  # unchanged

        # Poll 3: array "recovers" to the same single event again - must
        # not be re-sliced as new since the baseline never regressed.
        with patch.object(sc, "_fetch_match_events", return_value=[run_event]), \
             patch.object(sc, "_resolve_player_name", return_value="Test Player"):
            prev = sc._process_match(bot, "#pesis.fi", match, prev)

        assert bot.send_message.call_count == 1  # still just the one real send

    def test_run_by_scoring_team_not_home_or_away_is_ignored(self, sc):
        bot = MagicMock()
        prev = self._prev(event_count=0)
        match = make_match(mid=146953, home_id=16802, away_id=16796)
        events = [match_event(1, team_id=99999, sub_events=[run_sub_event(1, 99999)])]

        with patch.object(sc, "_fetch_match_events", return_value=events):
            new_state = sc._process_match(bot, "#pesis.fi", match, prev)

        bot.send_message.assert_not_called()
        assert new_state["period_home_runs"] == 0
        assert new_state["period_away_runs"] == 0


class TestPollLoop:
    def test_unexpected_poll_once_failure_logs_a_traceback_and_continues(self, sc):
        bot = MagicMock()
        stop_event = threading.Event()

        def fail_once(*args, **kwargs):
            stop_event.set()  # let the loop exit after this one iteration
            raise RuntimeError("boom")

        with patch.object(sc, "_poll_once", side_effect=fail_once), \
             patch("traceback.print_exc") as mock_print_exc:
            sc._poll_loop(bot, "#pesis.fi", stop_event, 2945)  # must not raise

        mock_print_exc.assert_called_once()


class TestPollOnce:
    def _seed(self, sc, channel, matches_state):
        sc._channels[channel] = {"stop_event": MagicMock(), "thread": None, "matches": matches_state}

    def test_unexpected_process_match_failure_logs_a_traceback(self, sc):
        # The catch-all around _process_match is the last line of defense
        # against anything not anticipated by a narrower handler - it
        # must print a full traceback, not just str(e), since that's
        # often the only way to pinpoint where a genuinely new bug broke.
        bot = MagicMock()
        self._seed(sc, "#pesis.fi", {
            146953: {"match_id": 146953, "home_id": 1, "away_id": 2, "home_name": "A", "away_name": "B",
                     "period": 0, "period_home_runs": 0, "period_away_runs": 0, "event_count": 0, "finished": False},
        })
        match = make_match(mid=146953, home_id=1, away_id=2, finished=False)

        with patch.object(sc, "_fetch_today_matches", return_value={146953: match}), \
             patch.object(sc, "_process_match", side_effect=KeyError("boom")), \
             patch("traceback.print_exc") as mock_print_exc:
            result = sc._poll_once(bot, "#pesis.fi", 2945)  # must not raise

        assert result is False
        mock_print_exc.assert_called_once()

    def test_all_finished_returns_true(self, sc):
        bot = MagicMock()
        self._seed(sc, "#pesis.fi", {
            146953: {"match_id": 146953, "home_id": 1, "away_id": 2, "home_name": "A", "away_name": "B",
                     "period": 0, "period_home_runs": 1, "period_away_runs": 0, "event_count": 0, "finished": True},
        })
        match = make_match(mid=146953, home_id=1, away_id=2, finished=True)

        with patch.object(sc, "_fetch_today_matches", return_value={146953: match}), \
             patch.object(sc, "_fetch_match_events", return_value=None):
            result = sc._poll_once(bot, "#pesis.fi", 2945)

        assert result is True

    def test_not_all_finished_returns_false(self, sc):
        bot = MagicMock()
        self._seed(sc, "#pesis.fi", {
            146953: {"match_id": 146953, "home_id": 1, "away_id": 2, "home_name": "A", "away_name": "B",
                     "period": 0, "period_home_runs": 0, "period_away_runs": 0, "event_count": 0, "finished": False},
        })
        match = make_match(mid=146953, home_id=1, away_id=2, finished=False)

        with patch.object(sc, "_fetch_today_matches", return_value={146953: match}), \
             patch.object(sc, "_fetch_match_events", return_value=None):
            result = sc._poll_once(bot, "#pesis.fi", 2945)

        assert result is False

    def test_fetch_failure_returns_false_without_crashing(self, sc):
        bot = MagicMock()
        self._seed(sc, "#pesis.fi", {146953: {"finished": False}})

        with patch.object(sc, "_fetch_today_matches", return_value=None):
            result = sc._poll_once(bot, "#pesis.fi", 2945)

        assert result is False

    def test_match_missing_from_todays_list_is_left_alone(self, sc):
        bot = MagicMock()
        prev_snapshot = {"match_id": 146953, "home_id": 1, "away_id": 2, "home_name": "A", "away_name": "B",
                          "period": 0, "period_home_runs": 0, "period_away_runs": 0, "event_count": 0, "finished": False}
        self._seed(sc, "#pesis.fi", {146953: prev_snapshot})

        with patch.object(sc, "_fetch_today_matches", return_value={}):
            result = sc._poll_once(bot, "#pesis.fi", 2945)

        assert result is False
        assert sc._channels["#pesis.fi"]["matches"][146953] == prev_snapshot

    def test_untracked_channel_stops_polling(self, sc):
        bot = MagicMock()
        with patch.object(sc, "_fetch_today_matches", return_value={}):
            result = sc._poll_once(bot, "#never-started", 2945)
        assert result is True
