"""Tests of `!liiga next <team> [count]` and `!nhl next <team> [count]` (src/team_next.py and the two
sports' schedule hooks). Start times are far in the past or the future so no test depends on today's date."""
from unittest.mock import MagicMock, patch

import pytest
import requests

import team_next
from liiga_command import LiigaCommand
from tests.conftest import make_json_response


# ---- the shared pieces ---------------------------------------------------------------------------------


class TestNormalise:
    @pytest.mark.parametrize("text, expected", [
        ("Kärpät", "karpat"), ("KARPAT", "karpat"), ("K-Espoo", "kespoo"), ("Ässät", "assat"),
        ("Montréal Canadiens", "montrealcanadiens"), ("  ", ""), (None, ""),
    ])
    def test_it_drops_case_diacritics_and_punctuation(self, text, expected):
        assert team_next.normalise(text) == expected


class TestParseNextArgs:
    def test_a_team_alone_gets_the_default_count(self):
        assert team_next.parse_next_args("jyp") == ("jyp", 3, False)

    def test_the_last_number_is_the_count(self):
        assert team_next.parse_next_args("jyp 5") == ("jyp", 5, False)
        assert team_next.parse_next_args("new york 1") == ("new york", 1, False)

    def test_a_count_above_the_maximum_is_clamped_and_says_so(self):
        assert team_next.parse_next_args("jyp 25") == ("jyp", 10, True)
        assert team_next.parse_next_args("jyp 11") == ("jyp", 10, True)
        assert team_next.parse_next_args("jyp 10") == ("jyp", 10, False)

    @pytest.mark.parametrize("rest", ["jyp 0", "5", "0"])
    def test_a_zero_count_or_a_number_without_a_team_is_a_usage_error(self, rest):
        assert team_next.parse_next_args(rest) is None

    def test_a_negative_number_is_read_as_part_of_the_team(self):
        assert team_next.parse_next_args("jyp -2") == ("jyp -2", 3, False)


TEAMS = {"KalPa": ["KalPa"], "KooKoo": ["KooKoo"], "K-Espoo": ["K-Espoo", "k-espoo"], "Kärpät": ["Kärpät"],
         "JYP": ["JYP", "jyp"], "Ässät": ["Ässät"], "Pelicans": ["Pelicans"], "Lukko": ["Lukko"]}


class TestMatchTeam:
    @pytest.mark.parametrize("query, label", [
        ("jyp", "JYP"), ("JYP", "JYP"), ("karpat", "Kärpät"), ("kärpät", "Kärpät"), ("assat", "Ässät"),
        ("k-espoo", "K-Espoo"), ("kespoo", "K-Espoo"), ("espoo", "K-Espoo"), ("pel", "Pelicans"), ("uk", "Lukko"),
    ])
    def test_one_team_is_found_by_name_prefix_or_part(self, query, label):
        assert team_next.match_team(query, TEAMS) == ("ok", [label])

    def test_an_exact_name_wins_over_longer_names_that_contain_it(self):
        teams = {"TOR": ["TOR", "Maple Leafs"], "TORX": ["TORX"]}
        assert team_next.match_team("tor", teams) == ("ok", ["TOR"])

    def test_several_teams_are_listed_not_guessed(self):
        status, hits = team_next.match_team("k", TEAMS)
        assert status == "ambiguous" and hits == ["KalPa", "KooKoo", "K-Espoo", "Kärpät"]

    def test_an_unknown_name_or_an_empty_one(self):
        assert team_next.match_team("xyz", TEAMS) == ("unknown", [])
        assert team_next.match_team("---", TEAMS) == ("unknown", [])


class TestPack:
    def test_a_short_reply_is_one_line(self):
        assert team_next.pack("Next X games: ", ["a", "b"]) == ["Next X games: a | b"]

    def test_a_long_reply_is_split_only_between_games(self):
        parts = [f"game-{i:02d} " + "x" * 40 for i in range(12)]
        lines = team_next.pack("Next X games: ", parts)
        assert len(lines) > 1
        assert all(len(line.encode()) <= team_next.MAX_LINE_BYTES for line in lines)
        assert " | ".join(lines).count("game-") == 12  # nothing lost, nothing cut in the middle
        assert all(part in " | ".join(lines) for part in parts)


# ---- Liiga -----------------------------------------------------------------------------------------------


def liiga_game(home, away, start, started=False, ended=False, game_id=1):
    return {"id": game_id, "start": start, "started": started, "ended": ended,
            "homeTeam": {"teamName": home, "teamId": f"1234:{home.casefold()}"},
            "awayTeam": {"teamName": away, "teamId": f"5678:{away.casefold()}"}}


LIIGA_SEASON = [
    liiga_game("JYP", "HPK", "2099-10-22T15:30:00Z", game_id=3),
    liiga_game("Ässät", "JYP", "2099-10-24T14:00:00Z", game_id=5),
    liiga_game("JYP", "Tappara", "2099-10-28T16:30:00Z", game_id=7),
    liiga_game("JYP", "Lukko", "2099-11-01T16:30:00Z", game_id=9),
    liiga_game("JYP", "Ilves", "2000-10-01T15:30:00Z", started=True, ended=True, game_id=1),   # played
    liiga_game("JYP", "KalPa", "2000-10-02T15:30:00Z", game_id=2),                              # postponed: never started
    liiga_game("Kärpät", "KooKoo", "2099-10-23T15:30:00Z", game_id=4),
]


@pytest.fixture
def liiga():
    command = LiigaCommand()
    command.session.get = MagicMock(return_value=make_json_response(LIIGA_SEASON))
    return command


class TestLiigaTeamNext:
    def test_it_lists_the_teams_next_three_games_in_order_in_helsinki_time(self, liiga):
        (line,) = liiga._team_next_lines("jyp", 3, False)
        assert line == ("Next JYP games: Thu 22.10. 18:30 JYP-HPK | Sat 24.10. 17:00 Ässät-JYP"
                        " | Wed 28.10. 18:30 JYP-Tappara")

    def test_played_and_postponed_games_are_left_out(self, liiga):
        (line,) = liiga._team_next_lines("jyp", 10, False)
        assert "Ilves" not in line and "KalPa" not in line

    def test_the_count_is_named_when_it_is_not_the_default(self, liiga):
        (line,) = liiga._team_next_lines("jyp", 4, False)
        assert line.startswith("Next 4 JYP games: ") and line.count("JYP-") + line.count("-JYP") == 4

    def test_fewer_games_left_than_asked_says_how_many(self, liiga):
        (line,) = liiga._team_next_lines("karpat", 3, False)
        assert line.startswith("Next 1 Kärpät games: ") and "Kärpät-KooKoo" in line

    def test_the_maximum_is_named_when_a_larger_count_was_clamped(self, liiga):
        (line,) = liiga._team_next_lines("jyp", 10, True)
        assert line.startswith("Next 4 JYP games (the maximum): ")

    def test_a_team_with_nothing_left(self, liiga):
        liiga.session.get.return_value = make_json_response([liiga_game("JYP", "HPK", "2000-01-01T15:30:00Z")])
        assert liiga._team_next_lines("jyp", 3, False) == ["No upcoming JYP games found."]

    def test_an_ambiguous_name_lists_the_candidates(self, liiga):
        assert liiga._team_next_lines("k", 3, False) == ["Which team? KalPa, Kärpät, KooKoo"]

    def test_an_unknown_name_lists_the_teams_read_from_the_schedule(self, liiga):
        (line,) = liiga._team_next_lines("xyz", 3, False)
        assert line == "Unknown team 'xyz'. Teams: HPK, Ilves, JYP, KalPa, KooKoo, Kärpät, Lukko, Tappara, Ässät"

    @pytest.mark.parametrize("failure", [requests.exceptions.ConnectionError("down"), ValueError("not json")])
    def test_an_unreachable_api_gives_the_error_line(self, liiga, failure):
        liiga.session.get.side_effect = failure
        assert liiga._team_next_lines("jyp", 3, False) == ["Error: could not reach the Liiga API."]

    def test_a_game_already_under_way_is_not_an_upcoming_one(self, liiga):
        live = liiga_game("JYP", "HPK", "2099-10-22T15:30:00Z", started=True)
        liiga.session.get.return_value = make_json_response([live])
        assert liiga._team_next_lines("jyp", 3, False) == ["No upcoming JYP games found."]

    def test_a_schedule_that_fails_after_the_team_list_was_read_gives_the_error_line(self, liiga):
        with patch.object(liiga, "_team_directory", return_value={"JYP": ["JYP"]}):
            liiga.session.get.side_effect = requests.exceptions.Timeout("slow")
            assert liiga._team_next_lines("jyp", 3, False) == ["Error: could not reach the Liiga API."]

    def test_an_unexpected_error_in_either_lookup_gives_the_error_line_and_is_journaled(self, liiga, capsys):
        with patch.object(liiga, "_team_directory", side_effect=RuntimeError("boom")):
            assert liiga._team_next_lines("jyp", 3, False) == ["Error: could not reach the Liiga API."]
        with patch.object(liiga, "_upcoming_games", side_effect=RuntimeError("boom")):
            assert liiga._team_next_lines("jyp", 3, False) == ["Error: could not reach the Liiga API."]
        out = capsys.readouterr().out
        assert "Liiga team list error: boom" in out and "Liiga schedule error for JYP: boom" in out

    def test_a_payload_of_the_wrong_shape_gives_the_error_line(self, liiga):
        liiga.session.get.return_value = make_json_response({"games": []})
        assert liiga._team_next_lines("jyp", 3, False) == ["Error: could not reach the Liiga API."]

    def test_the_season_is_fetched_once_for_repeated_questions(self, liiga):
        liiga._team_next_lines("jyp", 3, False)
        liiga._team_next_lines("karpat", 3, False)
        assert liiga.session.get.call_count == 1

    def test_a_failed_fetch_is_not_remembered(self, liiga):
        liiga.session.get.side_effect = [requests.exceptions.ConnectionError("down"),
                                         make_json_response(LIIGA_SEASON)]
        assert liiga._team_next_lines("jyp", 3, False) == ["Error: could not reach the Liiga API."]
        assert liiga._team_next_lines("jyp", 3, False)[0].startswith("Next JYP games")

    def test_it_asks_for_the_regular_season_without_a_date(self, liiga):
        liiga._team_next_lines("jyp", 3, False)
        params = liiga.session.get.call_args.kwargs["params"]
        assert params["tournament"] == "runkosarja" and "date" not in params

    def test_ten_games_of_the_longest_names_stay_in_one_line(self, liiga):
        names = ["Pelicans", "Jukurit", "Tappara", "K-Espoo", "Kärpät", "Jokerit", "KooKoo", "Ässät", "Sport", "Ilves"]
        season = [liiga_game("Pelicans" if i % 2 else names[i], names[(i + 1) % 10] if i % 2 else "Pelicans",
                             f"2099-12-{i + 1:02d}T17:30:00Z", game_id=i) for i in range(10)]
        liiga.session.get.return_value = make_json_response(season)
        lines = liiga._team_next_lines("pelicans", 10, False)
        assert len(lines) == 1 and len(lines[0].encode()) <= team_next.MAX_LINE_BYTES


class TestExecuteRouting:
    """What the command line does with `next ...`; the lookup itself runs on a thread, which these tests
    run in place."""

    @staticmethod
    def _inline_threads():
        class InlineThread:
            def __init__(self, target, args=(), daemon=None):
                self._target, self._args = target, args

            def start(self):
                self._target(*self._args)

        return patch("team_next.threading.Thread", InlineThread)

    def test_next_with_a_team_replies_at_once_and_sends_the_answer(self, liiga):
        bot = MagicMock()
        with self._inline_threads():
            reply = liiga.execute("next jyp 2", irc_bot=bot, channel="#smliiga")
        assert reply == "Checking the next Liiga games of jyp..."
        sent = bot.send_message.call_args.args
        assert sent[0] == "#smliiga" and "Next 2 JYP games: " in sent[1]

    def test_the_argument_may_be_typed_in_any_case(self, liiga):
        bot = MagicMock()
        with self._inline_threads():
            liiga.execute("NEXT JyP", irc_bot=bot, channel="#smliiga")
        assert "Next JYP games: " in bot.send_message.call_args.args[1]

    @pytest.mark.parametrize("args", ["next 0", "next 5", "next jyp 0"])
    def test_a_bad_count_gives_the_usage_text(self, liiga, args):
        reply = liiga.execute(args, irc_bot=MagicMock(), channel="#smliiga")
        assert reply.startswith("Usage: !liiga next [team [count]]") and "(e.g., !liiga next jyp 5)" in reply

    def test_without_channel_context_it_says_so(self, liiga):
        assert liiga.execute("next jyp").startswith("Error")

    def test_next_alone_and_start_and_stop_are_as_before(self, liiga):
        with patch.object(liiga, "_next", return_value="plain next") as plain:
            assert liiga.execute("next", irc_bot=MagicMock(), channel="#c") == "plain next"
        plain.assert_called_once()
        assert liiga.execute("start now please", irc_bot=MagicMock(), channel="#c").startswith("Usage: ")
        assert liiga.execute("stop it", irc_bot=MagicMock(), channel="#c").startswith("Usage: ")

    def test_the_usage_and_help_show_the_new_form(self, liiga):
        assert liiga.HELP == "!liiga start|stop|next [team [count]]"
        assert liiga.execute("nonsense").endswith("!liiga next [team [count]]")


# ---- NHL -------------------------------------------------------------------------------------------------


def standings_row(abbrev, place, common, full=None):
    return {"teamAbbrev": {"default": abbrev}, "placeName": {"default": place},
            "teamCommonName": {"default": common}, "teamName": {"default": full or f"{place} {common}"}}


STANDINGS = {"standings": [
    standings_row("TOR", "Toronto", "Maple Leafs"), standings_row("MTL", "Montréal", "Canadiens"),
    standings_row("NYR", "NY Rangers", "Rangers", "New York Rangers"),
    standings_row("NYI", "NY Islanders", "Islanders", "New York Islanders"),
    standings_row("LAK", "Los Angeles", "Kings"), standings_row("UTA", "Utah", "Mammoth", "Utah Mammoth"),
]}


def nhl_game(home, away, start, state="FUT", game_type=2, schedule_state="OK"):
    return {"id": 1, "startTimeUTC": start, "gameState": state, "gameType": game_type,
            "gameScheduleState": schedule_state, "homeTeam": {"abbrev": home}, "awayTeam": {"abbrev": away}}


TOR_SEASON = {"games": [
    nhl_game("TOR", "BUF", "2099-10-20T23:30:00Z"),
    nhl_game("NYR", "TOR", "2099-10-22T23:00:00Z"),
    nhl_game("TOR", "BOS", "2099-10-18T23:30:00Z", game_type=1),                       # preseason
    nhl_game("TOR", "EDM", "2099-10-30T23:30:00Z", schedule_state="PPD"),              # postponed
    nhl_game("TOR", "MTL", "2000-10-10T23:30:00Z", state="OFF"),                       # played
    nhl_game("TOR", "OTT", "2099-11-02T23:30:00Z", state="PRE"),
    nhl_game("CGY", "TOR", "2099-04-20T23:30:00Z", game_type=3),                       # a playoff game
]}


@pytest.fixture
def nhl(nhl_command):
    def get(url, **kwargs):
        if url.endswith("/standings/now"):
            return make_json_response(STANDINGS)
        if "/club-schedule-season/TOR/" in url:
            return make_json_response(TOR_SEASON)
        return make_json_response({"games": []})

    nhl_command.session.get = MagicMock(side_effect=get)
    return nhl_command


class TestNhlTeamNext:
    def test_it_lists_the_clubs_next_games_in_order_in_helsinki_time(self, nhl):
        (line,) = nhl._team_next_lines("toronto", 3, False)
        assert line == "Next TOR games: Tue 21.04. 02:30 CGY-TOR | Wed 21.10. 02:30 TOR-BUF | Fri 23.10. 02:00 NYR-TOR"

    def test_preseason_postponed_and_played_games_are_left_out(self, nhl):
        (line,) = nhl._team_next_lines("tor", 10, False)
        assert "BOS" not in line and "EDM" not in line and "MTL" not in line
        assert "OTT" in line  # a game about to start (PRE) is still to come

    @pytest.mark.parametrize("query", ["toronto", "leafs", "maple leafs", "tor", "TOR", "Toronto Maple Leafs"])
    def test_a_club_is_found_by_any_of_its_names(self, nhl, query):
        assert nhl._team_next_lines(query, 3, False)[0].startswith("Next TOR games: ")

    def test_a_name_with_a_diacritic_is_found_without_it(self, nhl):
        assert nhl._team_next_lines("montreal", 3, False) == ["No upcoming MTL games found."]

    def test_a_nickname_is_found(self, nhl):
        assert nhl._team_next_lines("habs", 3, False) == ["No upcoming MTL games found."]

    def test_new_york_is_ambiguous_and_los_angeles_is_not(self, nhl):
        assert nhl._team_next_lines("new york", 3, False) == ["Which team? NYR, NYI"]
        assert nhl._team_next_lines("ny", 3, False) == ["Which team? NYR, NYI"]
        assert nhl._team_next_lines("los angeles", 3, False) == ["No upcoming LAK games found."]

    def test_an_unknown_team_lists_the_abbreviations(self, nhl):
        (line,) = nhl._team_next_lines("xyz", 3, False)
        assert line == "Unknown team 'xyz'. Teams: LAK, MTL, NYI, NYR, TOR, UTA"

    def test_an_unreachable_api_gives_the_error_line(self, nhl):
        nhl.session.get = MagicMock(side_effect=requests.exceptions.ConnectionError("down"))
        assert nhl._team_next_lines("tor", 3, False) == ["Error: could not reach the NHL API."]

    def test_standings_of_the_wrong_shape_give_the_error_line(self, nhl):
        nhl.session.get = MagicMock(return_value=make_json_response({"standings": None}))
        assert nhl._team_next_lines("tor", 3, False) == ["Error: could not reach the NHL API."]

    def test_a_failed_club_schedule_gives_the_error_line(self, nhl):
        real = nhl.session.get.side_effect

        def get(url, **kwargs):
            if "/club-schedule-season/" in url:
                raise requests.exceptions.Timeout("slow")
            return real(url, **kwargs)

        nhl.session.get = MagicMock(side_effect=get)
        assert nhl._team_next_lines("tor", 3, False) == ["Error: could not reach the NHL API."]

    def test_the_standings_are_fetched_once_for_repeated_questions(self, nhl):
        nhl._team_next_lines("tor", 3, False)
        nhl._team_next_lines("tor", 5, False)
        urls = [call.args[0] for call in nhl.session.get.call_args_list]
        assert sum(url.endswith("/standings/now") for url in urls) == 1
        assert sum("/club-schedule-season/TOR/" in url for url in urls) == 1

    def test_the_unchanged_subcommands_still_work(self, nhl):
        assert nhl.HELP == "!nhl start|stop|next [team [count]]|now|results"
        with patch.object(nhl, "_scoreboard", return_value="board") as board:
            assert nhl.execute("now", irc_bot=MagicMock(), channel="#nhl.fi") == "board"
        board.assert_called_once()

    def test_next_with_a_team_replies_at_once_and_sends_the_answer(self, nhl):
        bot = MagicMock()
        with TestExecuteRouting._inline_threads():
            reply = nhl.execute("next toronto 2", irc_bot=bot, channel="#nhl.fi")
        assert reply == "Checking the next NHL games of toronto..."
        assert "Next 2 TOR games: " in bot.send_message.call_args.args[1]

    def test_a_bad_count_gives_the_usage_text(self, nhl):
        reply = nhl.execute("next 5", irc_bot=MagicMock(), channel="#nhl.fi")
        assert reply.startswith("Usage: !nhl next [team [count]]") and "(e.g., !nhl next tor 5)" in reply

    def test_ten_games_stay_in_one_line(self, nhl):
        season = {"games": [nhl_game("TOR", "NYR", f"2099-12-{i + 1:02d}T23:30:00Z") for i in range(10)]}
        nhl.session.get = MagicMock(side_effect=lambda url, **kw: make_json_response(
            STANDINGS if url.endswith("/standings/now") else season))
        lines = nhl._team_next_lines("tor", 10, False)
        assert len(lines) == 1 and len(lines[0].encode()) <= team_next.MAX_LINE_BYTES
