"""Builders shared by the NHL tracker's test files (one file per source module, as for the Pesis tests)."""
from unittest.mock import MagicMock

import requests


def roster_spot(player_id, first="John", last="Doe"):
    return {
        "playerId": player_id,
        "firstName": {"default": first},
        "lastName": {"default": last},
    }


def goal_play(event_id=1, period_number=1, period_type="REG", time_in_period="04:31",
              scoring_player_id=100, event_owner_team_id=10, home_score=1, away_score=0,
              assist1=None, assist2=None, situation_code=None):
    details = {
        "scoringPlayerId": scoring_player_id,
        "eventOwnerTeamId": event_owner_team_id,
        "homeScore": home_score,
        "awayScore": away_score,
    }
    if assist1 is not None:
        details["assist1PlayerId"] = assist1
    if assist2 is not None:
        details["assist2PlayerId"] = assist2
    play = {
        "eventId": event_id,
        "typeDescKey": "goal",
        "periodDescriptor": {"number": period_number, "periodType": period_type},
        "timeInPeriod": time_in_period,
        "details": details,
    }
    if situation_code is not None:
        play["situationCode"] = situation_code  # a play-level field in the real feed, not in details
    return play


def make_pbp(game_id=2026010026, home_id=10, away_id=20, home_abbrev="CAR", away_abbrev="FLA",
             home_place="Carolina", home_common="Hurricanes", away_place="Florida",
             away_common="Panthers", home_score=0, away_score=0, game_state="LIVE",
             last_period_type=None, plays=None, roster=None, season=None):
    pbp = {
        "id": game_id,
        "gameState": game_state,
        "homeTeam": {
            "id": home_id, "abbrev": home_abbrev, "score": home_score,
            "placeName": {"default": home_place}, "commonName": {"default": home_common},
        },
        "awayTeam": {
            "id": away_id, "abbrev": away_abbrev, "score": away_score,
            "placeName": {"default": away_place}, "commonName": {"default": away_common},
        },
        "gameOutcome": {"lastPeriodType": last_period_type} if last_period_type else {},
        "plays": plays or [],
        "rosterSpots": roster or [],
    }
    if season is not None:
        pbp["season"] = season
    return pbp


def seeded(*ids):
    """The record of a goal that was already in the feed when tracking
    started: remembered, but nothing was posted for it."""
    return {"ids": list(ids), "posted": None, "missing": 0, "retracted": False}


def make_schedule_game(gid=2026010026, home_id=10, away_id=20, home_abbrev="CAR", away_abbrev="FLA",
                        start_time_utc="2026-09-29T21:00:00Z", game_state="FUT",
                        home_score=None, away_score=None):
    home = {"id": home_id, "abbrev": home_abbrev}
    away = {"id": away_id, "abbrev": away_abbrev}
    if home_score is not None:  # the real schedule payload only carries scores once a game has started
        home["score"] = home_score
    if away_score is not None:
        away["score"] = away_score
    return {
        "id": gid,
        "startTimeUTC": start_time_utc,
        "gameState": game_state,
        "homeTeam": home,
        "awayTeam": away,
    }


def make_schedule_payload(date_to_games, next_start_date=None):
    """date_to_games: {date_str: [game_dict, ...]}. Every date not
    present gets an empty (0-game) entry, matching the real API always
    returning a 7-day window regardless of which days have games."""
    return {
        "nextStartDate": next_start_date,
        "gameWeek": [
            {"date": date, "numberOfGames": len(games), "games": games}
            for date, games in date_to_games.items()
        ],
    }


def html_response(text, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


def stoppage_play(reason, time_in_period, period_number=1, secondary=None):
    return {"typeDescKey": "stoppage", "periodDescriptor": {"number": period_number, "periodType": "REG"},
            "timeInPeriod": time_in_period,
            "details": {"reason": reason, "secondaryReason": reason if secondary is None else secondary}}


def review_play(time_in_period, period_number=1, reason="referee-or-linesman"):
    """A referee stoppage for a video review: the real shape of 2026-10-14 (reason referee-or-linesman, secondary
    reason video-review)."""
    return {"typeDescKey": "stoppage", "periodDescriptor": {"number": period_number, "periodType": "REG"},
            "timeInPeriod": time_in_period, "details": {"reason": reason, "secondaryReason": "video-review"}}


def penalty_play(desc_key, time_in_period, period_number=1):
    return {"typeDescKey": "penalty", "periodDescriptor": {"number": period_number, "periodType": "REG"},
            "timeInPeriod": time_in_period, "details": {"descKey": desc_key, "duration": 2}}
