from unittest.mock import MagicMock

import pytest

from live_tracker_command import LiveTrackerCommand


class MinimalTracker(LiveTrackerCommand):
    """Smallest possible concrete subclass, to exercise the shared
    lifecycle directly rather than only indirectly via LiigaCommand/
    PesisCommand's own test suites."""

    DISPLAY_NAME = "Test"
    COMMAND_NAME = "!test"
    CACHE_SLUG = "test"
    TRACKED_NOUN = "items"
    PERIOD_NOUN = "round"
    STATE_KEY = "items"

    def _run(self, irc_bot, channel, stop_event):
        # Tests drive _poll_loop/_poll_once directly rather than through
        # _start's real background thread, so this just has to not blow up
        # if that thread happens to run before a test finishes with it.
        pass

    def _fetch_next_period(self, context):
        raise NotImplementedError

    def _format_period_summary(self, items):
        return ", ".join(str(i) for i in items)


@pytest.fixture
def tracker():
    return MinimalTracker()


class TestLifecycle:
    def test_start_without_context_errors(self, tracker):
        assert "Error" in tracker._start(None, None)

    def test_start_reserves_slot_and_reports_checking(self, tracker):
        bot = MagicMock()
        result = tracker._start(bot, "#chan")
        assert result == "Checking today's Test items..."
        assert "#chan" in tracker._channels
        assert tracker._channels["#chan"]["items"] == {}

    def test_start_twice_reports_already_tracking(self, tracker):
        bot = MagicMock()
        tracker._start(bot, "#chan")
        result = tracker._start(bot, "#chan")
        assert result == "Already tracking live Test items in this channel."

    def test_stop_without_tracking_reports_not_tracking(self, tracker):
        assert tracker._stop("#chan") == "Not currently tracking Test items in this channel."

    def test_stop_after_start_sets_stop_event(self, tracker):
        bot = MagicMock()
        tracker._start(bot, "#chan")
        stop_event = tracker._channels["#chan"]["stop_event"]
        result = tracker._stop("#chan")
        assert result == "Stopped live Test tracking."
        assert stop_event.is_set()
        assert "#chan" not in tracker._channels

    def test_next_without_context_errors(self, tracker):
        assert "Error" in tracker._next(None, None)

    def test_next_reports_checking(self, tracker):
        assert tracker._next(MagicMock(), "#chan") == "Checking the next Test round..."


class TestRunNext:
    def test_fetch_returning_none_reports_api_unreachable(self, tracker):
        bot = MagicMock()
        tracker._fetch_next_period = lambda context: (None, None)
        tracker._run_next(bot, "#chan")
        bot.send_message.assert_called_once_with("#chan", "Error: could not reach the Test API.")

    def test_fetch_returning_empty_uses_not_found_message(self, tracker):
        bot = MagicMock()
        tracker._fetch_next_period = lambda context: (None, {})
        tracker._run_next(bot, "#chan")
        bot.send_message.assert_called_once_with("#chan", "No upcoming Test items found.")

    def test_fetch_returning_items_formats_summary(self, tracker):
        bot = MagicMock()
        tracker._fetch_next_period = lambda context: ("2026-01-01", {1: "a", 2: "b"})
        tracker._run_next(bot, "#chan")
        args = bot.send_message.call_args[0]
        assert args[0] == "#chan"
        assert "Next Test round" in args[1]
        assert "a, b" in args[1]

    def test_requires_context_blocks_when_unresolved(self, tracker):
        tracker.REQUIRES_CONTEXT = True
        tracker._resolve_context = lambda: None
        bot = MagicMock()
        tracker._run_next(bot, "#chan")
        bot.send_message.assert_called_once_with("#chan", "Error: could not reach the Test API.")

    def test_fetch_exception_does_not_propagate(self, tracker):
        bot = MagicMock()
        def boom(context):
            raise RuntimeError("boom")
        tracker._fetch_next_period = boom
        tracker._run_next(bot, "#chan")
        bot.send_message.assert_called_once_with("#chan", "Error: could not reach the Test API.")


class TestPollLoop:
    def test_poll_loop_stops_and_announces_when_all_ended(self, tracker):
        bot = MagicMock()
        tracker._start(bot, "#chan")
        stop_event = tracker._channels["#chan"]["stop_event"]
        tracker._poll_once = lambda irc_bot, channel, *ctx: True
        tracker._poll_loop(bot, "#chan", stop_event)
        assert "#chan" not in tracker._channels
        bot.send_message.assert_called_with(
            "#chan", "All of today's Test items have finished. Live tracking stopped."
        )

    def test_poll_loop_passes_through_context_args(self, tracker):
        bot = MagicMock()
        tracker._start(bot, "#chan")
        stop_event = tracker._channels["#chan"]["stop_event"]
        seen = []

        def fake_poll_once(irc_bot, channel, *ctx):
            seen.append(ctx)
            return True

        tracker._poll_once = fake_poll_once
        tracker._poll_loop(bot, "#chan", stop_event, "extra-context")
        assert seen == [("extra-context",)]

    def test_poll_loop_survives_exception_and_keeps_polling(self, tracker):
        bot = MagicMock()
        tracker._start(bot, "#chan")
        stop_event = tracker._channels["#chan"]["stop_event"]
        calls = {"count": 0}

        def flaky_poll_once(irc_bot, channel, *ctx):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("boom")
            stop_event.set()
            return False

        tracker._poll_once = flaky_poll_once
        tracker.POLL_INTERVAL_SECONDS = 0
        tracker._poll_loop(bot, "#chan", stop_event)
        assert calls["count"] == 2


class TestSafeSendAndDateLabel:
    def test_safe_send_swallows_exceptions(self, tracker):
        bot = MagicMock()
        bot.send_message.side_effect = RuntimeError("disconnected")
        tracker._safe_send(bot, "#chan", "hi")  # must not raise

    def test_format_date_label_handles_bad_input(self, tracker):
        assert tracker._format_date_label(None) == "unknown date"
        assert tracker._format_date_label("not-a-date") == "not-a-date"
