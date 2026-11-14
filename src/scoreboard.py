import threading


class ScoreboardMixin:
    """`<command> now` and `<command> results`: one-shot looks at the
    scoreboard, as opposed to the start/stop tracker, shared by the sports
    that have them. Both run on a background thread, never touch tracker
    state, and work while a tracker is running.

    This is the sport-neutral part: dispatch, the background thread, the
    Live / Final / Upcoming board, the results line and the line packing.
    A sport mixes it in ahead of LiveTrackerCommand and supplies
    _board_messages() and _results_messages() (each a list of messages, or
    None when the API can't be reached), the classifiers _is_live(),
    _is_ended() and _is_upcoming(), and the labels _live_label() and
    _final_label().
    """

    # The feed is cheap, but anyone can spam the command - a short cache
    # keeps that from becoming a request per message.
    SCORE_CACHE_SECONDS = 15
    # Well under IRC's 512-byte line limit, which the server's own framing
    # also eats into.
    MAX_LINE_BYTES = 380
    SECTION_SEPARATOR = " || "

    def execute(self, args=None, irc_bot=None, channel=None, **kwargs):
        arg = (args or "").strip().lower()
        if arg in ("now", "results"):
            return self._scoreboard(irc_bot, channel, arg)
        return super().execute(args, irc_bot=irc_bot, channel=channel, **kwargs)

    # ---- entry point --------------------------------------------------------

    def _scoreboard(self, irc_bot, channel, kind):
        """Starts the lookup on a background thread (a slow API can't stall
        the bot - same reasoning as `next`) and returns at once. The
        result is sent as one or more channel messages, not returned."""
        if irc_bot is None or channel is None:
            return "Error: this command needs channel context."
        threading.Thread(target=self._run_scoreboard, args=(irc_bot, channel, kind), daemon=True).start()
        return None

    def _run_scoreboard(self, irc_bot, channel, kind):
        try:
            messages = self._board_messages() if kind == "now" else self._results_messages()
        except Exception as e:
            print(f"{self.DISPLAY_NAME} {kind} lookup error: {e}")
            messages = None
        if not messages:
            messages = [f"Error: could not reach the {self.DISPLAY_NAME} API."]
        for message in messages:
            self._safe_send(irc_bot, channel, message)

    def _by_start(self, games):
        return sorted(games, key=lambda g: g.get(self.START_TIME_KEY) or "")

    # ---- `now` --------------------------------------------------------------

    def _board_from(self, games):
        """{id: game} as Live / Final / Upcoming messages (empty groups left
        out), or the no-games message when there are none."""
        if not games:
            return [self._no_games_message()]

        live = self._by_start(g for g in games.values() if self._is_live(g))
        ended = self._by_start(g for g in games.values() if self._is_ended(g))
        upcoming = self._by_start(
            g for g in games.values() if self._is_upcoming(g) and not self._is_ended(g)
        )

        sections = []
        if live:
            sections += self._chunk("Live: ", [self._live_label(g) for g in live], ", ")
        if ended:
            sections += self._chunk("Final: ", [self._final_label(g) for g in ended], ", ")
        if upcoming:
            summary = self._format_games_summary(upcoming)
            sections += self._chunk("Upcoming: ", summary.split(" | "), " | ")
        return self._pack(sections)

    def _no_games_message(self) -> str:
        name = self.DISPLAY_NAME
        try:
            date_str, games = self._fetch_next_gameday()
        except Exception as e:
            print(f"{name} next-gameday lookup error: {e}")
            date_str, games = None, None
        if not games:
            return f"No {name} games today."
        label = self._format_date_label(date_str)
        return f"No {name} games today. Next {name} gameday ({label}): {self._format_games_summary(games.values())}"

    # ---- `results` ----------------------------------------------------------

    def _results_from(self, date_label, finished, still_on):
        """The finished games as '<name> results D.M.: ...' messages, with a
        pointer to `now` on the last one when `still_on` games are being
        played."""
        header = f"{self.DISPLAY_NAME} results {date_label}: "
        messages = self._pack(self._chunk(header, [self._final_label(g) for g in finished], ", "))
        if still_on:
            messages[-1] += f" ({still_on} game(s) still on: {self.COMMAND_NAME} now)"
        return messages

    # ---- message packing ----------------------------------------------------

    def _chunk(self, prefix, items, sep):
        """Splits `items` into strings that each start with `prefix` and
        stay within MAX_LINE_BYTES, breaking only between items."""
        chunks, current = [], []
        for item in items:
            candidate = prefix + sep.join(current + [item])
            if current and len(candidate.encode("utf-8")) > self.MAX_LINE_BYTES:
                chunks.append(prefix + sep.join(current))
                current = []
            current.append(item)
        if current:
            chunks.append(prefix + sep.join(current))
        return chunks

    def _pack(self, parts):
        """Joins as many parts as fit into each message (never splitting a
        part), so a short board is one line and a long one a few."""
        messages = []
        for part in parts:
            if messages and len((messages[-1] + self.SECTION_SEPARATOR + part).encode("utf-8")) <= self.MAX_LINE_BYTES:
                messages[-1] += self.SECTION_SEPARATOR + part
            else:
                messages.append(part)
        return messages
