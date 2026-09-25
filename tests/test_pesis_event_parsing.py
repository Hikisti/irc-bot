from tests.pesis_test_helpers import run_sub_event, run_sub_event_by_number, out_at_home_sub_event, match_event, sc


class TestRunDetection:
    """Built directly against real production data patterns (see the
    commit history/conversation this was developed against)."""

    def test_eteni_kotipesaan_is_a_run(self, sc):
        sub = run_sub_event(9986, 16803, pattern="eteni_koti")
        assert sc._is_run_sub_event(sub["texts"]) is True

    def test_juoksu_is_a_run(self, sc):
        sub = run_sub_event(7911, 16802, pattern="juoksu")
        assert sc._is_run_sub_event(sub["texts"]) is True

    def test_paloi_kotipesaan_is_not_a_run(self, sc):
        # Put out at home plate - same destination string, different verb.
        sub = out_at_home_sub_event(9986, 16803)
        assert sc._is_run_sub_event(sub["texts"]) is False

    def test_eteni_to_a_regular_base_is_not_a_run(self, sc):
        sub = {
            "texts": [
                {"team": 16803, "type": "player", "id": 9986},
                {"type": "event", "text": "eteni"},
                "ykköspesälle",
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is False

    def test_kunnari_is_a_run(self, sc):
        # Home run - confirmed live it doesn't pair with a "kotipesään"
        # destination string like regular advancement does.
        sub = {
            "texts": [
                {"team": 16804, "type": "player", "number": 1},
                {"type": "event", "text": "löi kunnarin!", "base": 2},
                {"type": "stat", "homerun": 2},
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is True

    def test_vapaataival_kotipesaan_is_a_run(self, sc):
        # Regression test built from real Ykköspesis match data (147197):
        # a bases-loaded walk forcing a run home - a new pattern this
        # vocabulary didn't recognize until it showed up in that match.
        sub = {
            "texts": [
                {"team": 16926, "type": "player", "id": 7479},
                {"type": "event", "text": "sai vapaataipaleen väärien syöttöjen johdosta"},
                "kotipesään",
                {"type": "stat", "walkscore": 3},
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is True

    def test_vapaataival_to_a_regular_base_is_not_a_run(self, sc):
        sub = {
            "texts": [
                {"team": 16926, "type": "player", "id": 10249},
                {"type": "event", "text": "sai vapaataipaleen väärien syöttöjen johdosta"},
                "ykköspesälle",
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is False

    def test_eteni_harhaheitolla_kotipesaan_is_a_run(self, sc):
        # A suffixed "eteni..." variant (wild throw) - was being missed
        # entirely by an exact "eteni" match before this was reported.
        sub = {
            "texts": [
                {"team": 16804, "type": "player", "number": 11},
                {"type": "event", "text": "eteni harhaheitolla", "base": 3},
                "kotipesään",
                {"type": "stat", "wtscore": 3},
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is True

    def test_eteni_harhaheitolla_to_a_regular_base_is_not_a_run(self, sc):
        sub = {
            "texts": [
                {"team": 16804, "type": "player", "number": 7},
                {"type": "event", "text": "eteni harhaheitolla", "base": 0},
                "ykköspesälle",
            ],
        }
        assert sc._is_run_sub_event(sub["texts"]) is False

    def _without_sub_event(self, runs):
        """_extract_runs() also yields the raw matched sub-event (used by
        _group_runs_and_period_ends() for its intra-poll duplicate check)
        - strip it for tests that only care about who/what scored."""
        return [r[:2] for r in runs]

    def test_extract_runs_yields_multiple_scores_in_one_event(self, sc):
        event = match_event(1, team_id=16803, sub_events=[
            run_sub_event(9986, 16803, pattern="eteni_koti"),
            {"texts": [{"type": "event", "text": "eteni"}, "kolmospesälle"]},  # not a run
            run_sub_event(9904, 16803, pattern="eteni_koti"),
        ])
        runs = self._without_sub_event(sc._extract_runs(event))
        assert runs == [({"id": 9986}, None), ({"id": 9904}, None)]

    def test_extract_runs_with_number_based_player_ref(self, sc):
        event = match_event(1, team_id=16798, sub_events=[
            run_sub_event_by_number(1, 16798),
        ])
        runs = self._without_sub_event(sc._extract_runs(event))
        assert runs == [({"number": 1}, None)]

    def test_extract_runs_no_player_ref_carries_batter_through(self, sc):
        event = match_event(1, team_id=16802, batter=555, sub_events=[
            {"texts": [{"type": "event", "text": "juoksu"}]},
        ])
        runs = self._without_sub_event(sc._extract_runs(event))
        assert runs == [(None, 555)]

    def test_extract_runs_empty_for_non_scoring_event(self, sc):
        event = match_event(1, team_id=16802, sub_events=[
            {"texts": ["1. lyönti", {"type": "hit", "hit": None}]},
        ])
        assert list(sc._extract_runs(event)) == []

    def test_extract_runs_uses_harhaheitto_label_not_the_parent_batter(self, sc):
        # Regression test for a real incident: the parent event's own
        # "batter" field pointed at a completely unrelated player for a
        # wild-throw-caused run - must be replaced with the literal
        # "Harhaheitto" label instead of misattributing it.
        event = match_event(1, team_id=16804, batter=7, sub_events=[
            {"texts": [
                {"team": 16804, "type": "player", "number": 11},
                {"type": "event", "text": "eteni harhaheitolla", "base": 3},
                "kotipesään",
                {"type": "stat", "wtscore": 3},
            ]},
        ])
        runs = self._without_sub_event(sc._extract_runs(event))
        assert runs == [({"number": 11}, "Harhaheitto")]

    def test_extract_runs_uses_vapaataival_label_not_the_parent_batter(self, sc):
        # Same idea as Harhaheitto, for a bases-loaded walk (built from
        # real Ykköspesis match 147197 data): pesistulokset.fi's own page
        # shows literally "Vapaataival" as the batter, not whoever really
        # drew the walk.
        event = match_event(1, team_id=16926, batter=5246, sub_events=[
            {"texts": [
                {"team": 16926, "type": "player", "id": 7479},
                {"type": "event", "text": "sai vapaataipaleen väärien syöttöjen johdosta"},
                "kotipesään",
                {"type": "stat", "walkscore": 3},
            ]},
        ])
        runs = self._without_sub_event(sc._extract_runs(event))
        assert runs == [({"id": 7479}, "Vapaataival")]

    def test_extract_runs_regular_run_still_uses_parent_batter(self, sc):
        event = match_event(1, team_id=16804, batter=10, sub_events=[
            run_sub_event_by_number(3, 16804),
        ])
        runs = self._without_sub_event(sc._extract_runs(event))
        assert runs == [({"number": 3}, 10)]

    def test_extract_runs_exposes_the_raw_sub_event(self, sc):
        sub_event = run_sub_event(9986, 16803, pattern="eteni_koti")
        event = match_event(1, team_id=16803, sub_events=[sub_event])
        [(_, _, exposed)] = list(sc._extract_runs(event))
        assert exposed == sub_event


class TestIsErrorDrivenRun:
    def test_true_for_harhaheitolla_suffix(self, sc):
        texts = [{"type": "event", "text": "eteni harhaheitolla"}]
        assert sc._is_error_driven_run(texts) is True

    def test_false_for_plain_eteni(self, sc):
        texts = [{"type": "event", "text": "eteni"}]
        assert sc._is_error_driven_run(texts) is False

    def test_false_for_kunnari(self, sc):
        texts = [{"type": "event", "text": "löi kunnarin!"}]
        assert sc._is_error_driven_run(texts) is False


class TestIsWalkForcedRun:
    def test_true_for_vapaataival_text(self, sc):
        texts = [{"type": "event", "text": "sai vapaataipaleen väärien syöttöjen johdosta"}]
        assert sc._is_walk_forced_run(texts) is True

    def test_false_for_plain_eteni(self, sc):
        texts = [{"type": "event", "text": "eteni"}]
        assert sc._is_walk_forced_run(texts) is False

    def test_false_for_harhaheitto(self, sc):
        texts = [{"type": "event", "text": "eteni harhaheitolla"}]
        assert sc._is_walk_forced_run(texts) is False


class TestLastPlayerRef:
    def test_prefers_id_when_present(self, sc):
        texts = [{"team": 1, "type": "player", "id": 42}]
        assert sc._last_player_ref(texts) == {"id": 42}

    def test_falls_back_to_number(self, sc):
        texts = [{"team": 1, "type": "player", "number": 7}]
        assert sc._last_player_ref(texts) == {"number": 7}

    def test_no_player_entry_returns_none(self, sc):
        assert sc._last_player_ref([{"type": "event", "text": "eteni"}]) is None

    def test_takes_the_last_player_entry(self, sc):
        texts = [
            {"team": 1, "type": "player", "id": 1},
            {"type": "event", "text": "eteni"},
            {"team": 1, "type": "player", "id": 2, "hide": True},
        ]
        assert sc._last_player_ref(texts) == {"id": 2}


class TestPeriodEndDetection:
    """periodend detection is built directly against real data - see the
    commit history this was developed against for exact payloads."""

    def _periodend_sub_event(self, text="Ensimmäinen jakso päättyi"):
        return {
            "texts": [{"type": "event", "text": text}, {"type": "stat", "periodend": 1}],
            "runnersAtBases": [None] * 5,
        }

    def test_extracts_the_period_end_text(self, sc):
        event = match_event(1, team_id=16798, sub_events=[self._periodend_sub_event()])
        assert sc._extract_period_end_text(event) == "Ensimmäinen jakso päättyi"

    def test_second_period_text(self, sc):
        event = match_event(1, team_id=16798, sub_events=[
            self._periodend_sub_event("Toinen jakso päättyi"),
        ])
        assert sc._extract_period_end_text(event) == "Toinen jakso päättyi"

    def test_supervuoro_text(self, sc):
        event = match_event(1, team_id=16798, sub_events=[
            self._periodend_sub_event("Supervuoro päättyi"),
        ])
        assert sc._extract_period_end_text(event) == "Supervuoro päättyi"

    def test_match_end_is_not_mistaken_for_period_end(self, sc):
        # "Ottelu päättyi" uses a different stat key ("match-ended"), not
        # "periodend" - confirmed live these never overlap.
        event = match_event(1, team_id=16798, sub_events=[
            {"texts": [{"type": "event", "text": "Ottelu päättyi"},
                       {"type": "stat", "match-ended": "2026-08-25T19:33:00+03:00"}]},
        ])
        assert sc._extract_period_end_text(event) is None

    def test_regular_run_is_not_mistaken_for_period_end(self, sc):
        event = match_event(1, team_id=16798, sub_events=[run_sub_event(1, 16798)])
        assert sc._extract_period_end_text(event) is None

    def test_no_events_returns_none(self, sc):
        event = match_event(1, team_id=16798, sub_events=[])
        assert sc._extract_period_end_text(event) is None

    def test_periodend_marker_with_no_text_falls_back(self, sc):
        # The "periodend" stat marker present but no accompanying "event"
        # text of its own - confirmed live this combination exists.
        sub_event = {"texts": [{"type": "stat", "periodend": 1}], "runnersAtBases": [None] * 5}
        event = match_event(1, team_id=16798, sub_events=[sub_event])
        assert sc._extract_period_end_text(event) == sc.PERIOD_END_FALLBACK_TEXT


class TestFormatPeriodEnd:
    def test_format(self, sc):
        msg = sc._format_period_end("Ensimmäinen jakso päättyi", "Home", "Away", 4, 2)
        assert msg.startswith(sc.PERIOD_END_PREFIX)
        assert "Ensimmäinen jakso päättyi" in msg
        assert "Home 4-2 Away" in msg


class TestFormatFinal:
    """Real pesäpallo results are headlined by jaksovoitot (periods won),
    not total runs - matches pesistulokset.fi's own result_string shape,
    e.g. "1-0k (0-0, 0-0, 0-0, 2-1)"."""

    def test_matches_the_requested_format(self, sc):
        live = {
            "periods": {"home": 1, "away": 0},
            "runs": [{"home": [4], "away": [2]}, {"home": [2], "away": [2]}],
        }
        result = sc._format_final(live, "Joensuun Maila", "Sotkamon Jymy")
        assert result == "Joensuun Maila - Sotkamon Jymy 1 - 0 (4 - 2, 2 - 2)"

    def test_unplayed_periods_are_omitted_from_the_breakdown(self, sc):
        live = {
            "periods": {"home": 1, "away": 0},
            "runs": [
                {"home": [4], "away": [2]},
                {"home": [2], "away": [2]},
                {"home": [None], "away": [None]},  # supervuoro - never needed
                {"home": [None], "away": [None]},  # kotiutuslyöntikilpailu - never needed
            ],
        }
        result = sc._format_final(live, "Home", "Away")
        assert result == "Home - Away 1 - 0 (4 - 2, 2 - 2)"

    def test_includes_a_played_tiebreak(self, sc):
        live = {
            "periods": {"home": 1, "away": 0},
            "runs": [
                {"home": [0], "away": [0]},
                {"home": [0], "away": [0]},
                {"home": [0], "away": [0]},
                {"home": [2], "away": [1]},  # kotiutuslyöntikilpailu decided it
            ],
        }
        result = sc._format_final(live, "Home", "Away")
        assert result == "Home - Away 1 - 0 (0 - 0, 0 - 0, 0 - 0, 2 - 1)"

    def test_missing_periods_field_falls_back_gracefully(self, sc):
        live = {"runs": [{"home": [4], "away": [2]}]}
        result = sc._format_final(live, "Home", "Away")
        assert result == "Home - Away ? - ? (4 - 2)"

    def test_no_runs_data_omits_the_breakdown_entirely(self, sc):
        live = {"periods": {"home": 1, "away": 0}}
        result = sc._format_final(live, "Home", "Away")
        assert result == "Home - Away 1 - 0"


class TestSumValues:
    def test_sums_numeric_entries(self, sc):
        assert sc._sum_values([2, 0, 3]) == 5

    def test_non_list_returns_none(self, sc):
        assert sc._sum_values("not-a-list") is None

    def test_non_numeric_entries_are_skipped_not_fatal(self, sc):
        # Real per-period arrays contain None for innings not yet played.
        assert sc._sum_values([None, 3]) == 3

    def test_all_non_numeric_returns_none(self, sc):
        assert sc._sum_values([None, None]) is None


class TestPeriodRuns:
    """Regression coverage: pesäpallo scores each jakso independently
    (confirmed live via the API's own runs_home_first_period/
    second_period/etc split), so RUN:/JAKSO: must use only the current
    period's own tally, not a naive match-wide cumulative sum across
    periods."""

    LIVE = {"runs": [
        {"home": [0, 0, 2, 0], "away": [0, 1, 1, 0]},   # jakso 1: 2-2
        {"home": [3, 0, 6, None], "away": [0, 0, None, None]},  # jakso 2 (in progress): 9-0
    ]}

    def test_scoped_to_a_single_period(self, sc):
        assert sc._period_runs(self.LIVE, "home", 0) == 2
        assert sc._period_runs(self.LIVE, "away", 0) == 2

    def test_different_period_gives_a_different_total(self, sc):
        # This is the actual bug: summing across periods would give 11
        # for home here (2+9), not jakso 2's own 9. jakso 2's arrays also
        # contain None for innings not yet played - ignored, not summed
        # as 0 contributions that would still be "found".
        assert sc._period_runs(self.LIVE, "home", 1) == 9
        assert sc._period_runs(self.LIVE, "away", 1) == 0

    def test_none_period_index_returns_none(self, sc):
        assert sc._period_runs(self.LIVE, "home", None) is None

    def test_out_of_range_period_index_returns_none(self, sc):
        assert sc._period_runs(self.LIVE, "home", 5) is None
        assert sc._period_runs(self.LIVE, "home", -1) is None

    def test_missing_runs_returns_none(self, sc):
        assert sc._period_runs({}, "home", 0) is None


class TestFormatRun:
    def test_includes_team_scorer_and_score(self, sc):
        # period=1 is the *second* period - the feed is 0-indexed
        # (confirmed live: "Ensimmäinen jakso päättyi" carries period=0).
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 3, 1, 16802, 16802, "Santtu Patova")
        assert msg.startswith(sc.RUN_PREFIX)
        assert "Home — Santtu Patova" in msg
        assert "Home 3-1 Away" in msg
        assert "(2. jakso)" in msg

    def test_away_team_scoring(self, sc):
        event = {"period": 2}
        msg = sc._format_run(event, "Home", "Away", 1, 2, 999, 16802, "Someone")
        assert "Away — Someone" in msg

    def test_unknown_scorer_name(self, sc):
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, None)
        assert "Unknown" in msg

    def test_period_zero_is_first_period_not_omitted(self, sc):
        # Regression test: period=0 is a real, meaningful value (1st
        # period) - must not be treated as "no period" just because it's
        # falsy in Python.
        event = {"period": 0}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(1. jakso)" in msg

    def test_period_two_is_supervuoro(self, sc):
        event = {"period": 2}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(supervuoro)" in msg

    def test_period_three_is_kotiutuslyontikilpailu(self, sc):
        event = {"period": 3}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(kotiutuslyöntikilpailu)" in msg

    def test_missing_period_has_no_label(self, sc):
        event = {}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "jakso" not in msg

    def test_includes_batter_when_different_from_scorer(self, sc):
        # Lyöjä (batter) before etenijä (scorer), matching pesistulokset.fi's
        # own column order.
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "Konsta Piironen", "Joosua Rättö")
        assert "Joosua Rättö → Konsta Piironen" in msg

    def test_omits_batter_when_same_as_scorer(self, sc):
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "Same Person", "Same Person")
        # No "batter → scorer" arrow when they're the same person (e.g. a
        # home run) - just the name once, not repeated on both sides.
        assert "→" not in msg
        assert msg.count("Same Person") == 1

    def test_omits_batter_when_unresolved(self, sc):
        event = {"period": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "Scorer", None)
        assert "→" not in msg
        assert "Scorer" in msg

    def test_includes_vuoropari_alongside_the_period(self, sc):
        # Regression coverage built directly from real matches (147202,
        # 147207): "inning" (0-indexed) + "batTurn" (0 = the side that
        # bats first in that inning - "aloittava"; 1 = the side that
        # bats second - "lopettava") together give pesistulokset.fi's own
        # "Vuoropari" column, e.g. "3. lopettava".
        event = {"period": 0, "inning": 2, "batTurn": 1}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(1. jakso, 3. lopettava)" in msg

    def test_vuoropari_aloittava_is_bat_turn_zero(self, sc):
        event = {"period": 0, "inning": 0, "batTurn": 0}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(1. jakso, 1. aloittava)" in msg

    def test_missing_inning_or_bat_turn_falls_back_to_period_only(self, sc):
        event = {"period": 0, "inning": None, "batTurn": None}
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(1. jakso)" in msg
        assert "aloittava" not in msg and "lopettava" not in msg

    def test_vuoropari_without_a_period_has_no_leading_comma(self, sc):
        event = {"inning": 0, "batTurn": 0}  # no "period" key at all
        msg = sc._format_run(event, "Home", "Away", 1, 0, 16802, 16802, "X")
        assert "(1. aloittava)" in msg


