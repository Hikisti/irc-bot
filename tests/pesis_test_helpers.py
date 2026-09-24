"""Shared test builders and fixtures for the split PesisCommand test
files (test_pesis_command.py, test_pesis_event_parsing.py,
test_pesis_player_names.py, test_pesis_data_fetching.py) - kept in one
place so each split file doesn't redefine its own slightly-different
copy of the same fake API payloads."""

import pytest

from pesis_command import SuperpesisCommand


def series_list_payload():
    """Trimmed real shape: multiple divisions under one season, plus an
    older season, to exercise both the level/series disambiguation and
    the "pick the latest season" logic."""
    return {
        "seasons": [
            {
                "season": {"id": 110, "season": 2026},
                "seasonSerieses": [
                    {"seasonSeries": {"id": 2945, "name": "Miesten Superpesis"},
                     "level": {"id": 1, "name": "Superpesis"}, "series": {"id": 1, "name": "Miehet"}},
                    {"seasonSeries": {"id": 2946, "name": "Naisten Superpesis"},
                     "level": {"id": 1, "name": "Superpesis"}, "series": {"id": 2, "name": "Naiset"}},
                    {"seasonSeries": {"id": 2954, "name": "Miesten Ykköspesis"},
                     "level": {"id": 3, "name": "Ykköspesis"}, "series": {"id": 1, "name": "Miehet"}},
                ],
            },
            {
                "season": {"id": 95, "season": 2025},
                "seasonSerieses": [
                    {"seasonSeries": {"id": 2810, "name": "Miesten Superpesis"},
                     "level": {"id": 1, "name": "Superpesis"}, "series": {"id": 1, "name": "Miehet"}},
                ],
            },
        ]
    }


def make_match(mid=146953, home="Manse PP", home_id=16802, away="Hyvinkään Tahko", away_id=16796,
                date="2026-08-24T14:00:00.000000Z", home_runs=None, away_runs=None, finished=False):
    live_result = {"finished": finished}
    if home_runs is not None:
        live_result["runs"] = [{"home": [home_runs], "away": [away_runs or 0]}]
    return {
        "id": mid,
        "home": {"id": home_id, "name": home},
        "away": {"id": away_id, "name": away},
        "date": date,
        "liveResult": live_result,
    }


def matches_list_payload(*matches):
    return [{"groups": [{"matches": list(matches)}]}]


def run_sub_event(player_id, team_id, pattern="eteni_koti"):
    """Builds a sub-event that _is_run_sub_event() recognizes."""
    if pattern == "eteni_koti":
        texts = [
            {"team": team_id, "type": "player", "id": player_id},
            {"type": "event", "text": "eteni"},
            "kotipesään",
            {"type": "stat", "score": 1},
        ]
    elif pattern == "juoksu":
        texts = [
            {"type": "event", "text": "juoksu"},
            {"team": team_id, "type": "player", "id": player_id, "hide": True},
        ]
    else:
        raise ValueError(pattern)
    return {"texts": texts, "runnersAtBases": [None] * 5}


def run_sub_event_by_number(jersey_number, team_id):
    """Same as run_sub_event(pattern="eteni_koti") but using a jersey
    "number" reference instead of a global "id" - the format confirmed
    live for some matches (see _last_player_ref())."""
    return {
        "texts": [
            {"team": team_id, "type": "player", "number": jersey_number},
            {"type": "event", "text": "eteni"},
            "kotipesään",
            {"type": "stat", "score": 1},
        ],
        "runnersAtBases": [None] * 5,
    }


def out_at_home_sub_event(player_id, team_id):
    """"paloi" (put out) at home - must NOT be counted as a run."""
    return {
        "texts": [
            {"team": team_id, "type": "player", "id": player_id},
            {"type": "event", "text": "paloi"},
            {"type": "stat", "out": 1},
            "kotipesään",
        ],
        "runnersAtBases": [None] * 5,
    }


def match_event(event_id, team_id, period=1, sub_events=None, batter=None, inning=0, bat_turn=None):
    return {
        "id": event_id,
        "period": period,
        "inning": inning,
        "batTurn": bat_turn,
        "team": team_id,
        "hTeam": team_id,
        "batter": batter,
        "events": sub_events or [],
    }


def join_channel_thread(sc, channel, timeout=2):
    entry = sc._channels.get(channel)
    if entry and entry.get("thread"):
        entry["thread"].join(timeout=timeout)


@pytest.fixture
def sc(tmp_path):
    # Isolate every test from the real on-disk series-id cache: without
    # this, a leftover cache file from a real bot run (or another test)
    # would make _resolve_series_id() skip the mocked session.get() call
    # entirely, breaking assertions that expect it to be called.
    # SERIES_CACHE_FILE is set per-instance in __init__ (see PesisCommand -
    # it's derived from CACHE_SLUG, so each subclass gets its own file),
    # so it has to be overridden after construction, not via
    # monkeypatch.setattr on the class.
    instance = SuperpesisCommand()
    instance.SERIES_CACHE_FILE = str(tmp_path / "series_cache.json")
    return instance
