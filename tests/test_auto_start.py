import datetime
import threading

import pytest

from auto_start import AutoStarter, Slate, due

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 14, 15, 0, tzinfo=UTC)  # the first game's scheduled start


def at(minutes):
    return T0 + datetime.timedelta(minutes=minutes)


class TestDue:
    SLATE = Slate(first_start=T0, seen_at=at(-600), read_at=at(-5))

    def test_not_before_the_first_start(self):
        assert not due(at(-1), self.SLATE, tracking=False, started_for=None, stopped_at=None)

    def test_at_the_first_start(self):
        assert due(T0, self.SLATE, tracking=False, started_for=None, stopped_at=None)

    def test_later_too_when_nobody_started_it(self):
        assert due(at(90), self.SLATE, tracking=False, started_for=None, stopped_at=None)

    def test_not_when_already_tracking(self):
        assert not due(T0, self.SLATE, tracking=True, started_for=None, stopped_at=None)

    def test_not_twice_for_the_same_first_start(self):
        assert not due(at(5), self.SLATE, tracking=False, started_for=T0, stopped_at=None)

    def test_again_for_another_days_first_start(self):
        assert due(T0, self.SLATE, tracking=False, started_for=at(-1440), stopped_at=None)

    def test_not_after_a_stop_on_the_same_day(self):
        assert not due(T0, self.SLATE, tracking=False, started_for=None, stopped_at=at(-30))

    def test_a_stop_from_before_the_day_was_seen_does_not_count(self):
        assert due(T0, self.SLATE, tracking=False, started_for=None, stopped_at=at(-700))

    def test_not_when_there_are_no_games(self):
        assert not due(T0, Slate(None, at(-600), at(-5)), tracking=False, started_for=None, stopped_at=None)

    def test_not_before_anything_was_read(self):
        assert not due(T0, None, tracking=False, started_for=None, stopped_at=None)


class FakeTracker:
    DISPLAY_NAME = "Test"
    HELSINKI_TZ = datetime.timezone(datetime.timedelta(hours=2))

    def __init__(self, first_start=T0):
        self.schedule = first_start  # what the feed says: a first start, None (no games) or "fail"
        self.reads = 0
        self.tracking = set()
        self.stopped = {}
        self.started = []

    def _fetch_today_items(self, context):
        self.reads += 1
        if self.schedule == "fail":
            return None
        return {} if self.schedule is None else {1: {"start": self.schedule}}

    def first_start(self, items):
        return min(item["start"] for item in items.values())

    def is_tracking(self, channel):
        return channel in self.tracking

    def last_stopped(self, channel):
        return self.stopped.get(channel)

    def start_automatically(self, irc_bot, channel):
        self.started.append(channel)
        self.tracking.add(channel)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def setup():
    tracker = FakeTracker()
    clock = Clock(at(-120))
    starter = AutoStarter(irc_bot=object(), targets=[(tracker, ["#a", "#b"])], clock=clock)
    return tracker, clock, starter


def tick_at(starter, clock, minutes):
    clock.now = at(minutes)
    starter.tick()


class TestAutoStarter:
    def test_starts_every_channel_at_the_first_start_and_only_then(self, setup):
        tracker, clock, starter = setup
        tick_at(starter, clock, -1)
        assert tracker.started == []
        tick_at(starter, clock, 0)
        assert tracker.started == ["#a", "#b"]

    def test_a_channel_already_tracking_is_left_alone(self, setup):
        tracker, clock, starter = setup
        tracker.tracking.add("#a")
        tick_at(starter, clock, 0)
        assert tracker.started == ["#b"]

    def test_never_twice_on_one_day_even_after_the_tracker_has_ended(self, setup):
        tracker, clock, starter = setup
        tick_at(starter, clock, 0)
        tracker.tracking.clear()  # every game over, the tracker stopped by itself
        tick_at(starter, clock, 300)
        assert tracker.started == ["#a", "#b"]

    def test_a_tracker_started_by_hand_that_ends_is_not_started_again(self, setup):
        tracker, clock, starter = setup
        tracker.tracking.add("#a")
        tick_at(starter, clock, 0)
        tracker.tracking.discard("#a")
        tick_at(starter, clock, 200)
        assert tracker.started == ["#b"]

    def test_a_stop_on_the_day_keeps_it_off(self, setup):
        tracker, clock, starter = setup
        tick_at(starter, clock, -60)  # the day's schedule is read
        tracker.stopped["#a"] = at(-10)
        tick_at(starter, clock, 0)
        assert tracker.started == ["#b"]

    def test_a_stop_after_the_automatic_start_keeps_it_off(self, setup):
        tracker, clock, starter = setup
        tick_at(starter, clock, 0)
        tracker.tracking.discard("#a")
        tracker.stopped["#a"] = at(5)
        tick_at(starter, clock, 6)
        assert tracker.started == ["#a", "#b"]

    def test_a_stop_from_the_day_before_does_not_count(self, setup):
        tracker, clock, starter = setup
        tracker.stopped["#a"] = at(-1000)
        tick_at(starter, clock, -60)
        tick_at(starter, clock, 0)
        assert tracker.started == ["#a", "#b"]

    def test_the_next_days_first_start_starts_it_again(self, setup):
        tracker, clock, starter = setup
        tick_at(starter, clock, 0)
        tracker.tracking.clear()
        tracker.schedule = at(1440)
        tick_at(starter, clock, 1430)
        assert tracker.started == ["#a", "#b"]
        tick_at(starter, clock, 1440)
        assert tracker.started == ["#a", "#b", "#a", "#b"]

    def test_no_games_no_start(self, setup):
        tracker, clock, starter = setup
        tracker.schedule = None
        for minutes in range(0, 600, 60):
            tick_at(starter, clock, minutes)
        assert tracker.started == []

    def test_after_a_restart_mid_slate_it_starts_at_once(self, setup):
        tracker, clock, starter = setup
        tick_at(starter, clock, 75)  # the first read of a new process, games under way
        assert tracker.started == ["#a", "#b"]

    def test_the_schedule_is_read_every_ten_minutes_not_every_minute(self, setup):
        tracker, clock, starter = setup
        for minutes in range(-30, 0):
            tick_at(starter, clock, minutes)
        assert tracker.reads == 3

    def test_a_later_start_time_is_noticed_at_the_next_read(self, setup):
        tracker, clock, starter = setup
        tick_at(starter, clock, -20)
        tracker.schedule = at(30)  # the first game moved back half an hour
        tick_at(starter, clock, -5)
        tick_at(starter, clock, 0)
        tick_at(starter, clock, 29)
        assert tracker.started == []
        tick_at(starter, clock, 30)
        assert tracker.started == ["#a", "#b"]

    def test_a_failed_read_keeps_what_was_known(self, setup, capsys):
        tracker, clock, starter = setup
        tick_at(starter, clock, -20)
        tracker.schedule = "fail"
        tick_at(starter, clock, 0)
        assert tracker.started == ["#a", "#b"]

    def test_a_read_that_raises_counts_as_failed(self, setup, capsys):
        tracker, clock, starter = setup
        tick_at(starter, clock, -20)
        tracker._fetch_today_items = lambda context: 1 / 0
        tick_at(starter, clock, 0)
        assert tracker.started == ["#a", "#b"]
        assert "Test: automatic start: schedule read failed: division by zero" in capsys.readouterr().out

    def test_a_failed_first_read_starts_nothing_and_is_retried_later(self, setup):
        tracker, clock, starter = setup
        tracker.schedule = "fail"
        tick_at(starter, clock, 0)
        tick_at(starter, clock, 5)
        assert (tracker.started, tracker.reads) == ([], 1)
        tracker.schedule = T0
        tick_at(starter, clock, 10)
        assert tracker.started == ["#a", "#b"]

    def test_one_tracker_failing_does_not_stop_the_others(self, capsys):
        broken, working = FakeTracker(), FakeTracker()
        broken.is_tracking = lambda channel: 1 / 0
        clock = Clock(T0)
        AutoStarter(object(), [(broken, ["#a"]), (working, ["#b"])], clock=clock).tick()
        assert working.started == ["#b"]
        assert "Test: automatic start check failed: division by zero" in capsys.readouterr().out

    def test_the_journal_says_why_it_started(self, setup, capsys):
        tracker, clock, starter = setup
        tick_at(starter, clock, 0)
        assert "Test: #a: nobody started the tracker by the first game (17:00), starting it" in capsys.readouterr().out

    def test_run_checks_once_a_minute_until_stopped(self, setup):
        tracker, clock, starter = setup
        stop_event = threading.Event()
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) >= 3:
                stop_event.set()

        stop_event.wait = wait
        starter.run(stop_event)
        assert waits == [60, 60, 60]
