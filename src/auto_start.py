"""Starts a live tracker by itself at the scheduled start of the day's first game, in every channel that has
the tracker (channels.toml) and where nobody has started it by then (#57). It goes through the tracker's own
`start`, so every guard there (already finished, postponed games, one game day) applies unchanged."""
import datetime
import threading
import traceback
from dataclasses import dataclass


@dataclass(frozen=True)
class Slate:
    """What the feed said about today's games when last read."""
    first_start: datetime.datetime | None  # the earliest scheduled start; None when there are no games
    seen_at: datetime.datetime  # when this first start was first read: a stop before it was for another day
    read_at: datetime.datetime


def due(now, slate, tracking, started_for, stopped_at) -> bool:
    """Whether to start a channel's tracker now. `started_for` is the first start this channel was last
    started (or found already tracking) for; `stopped_at` is its last manual stop, or None."""
    if slate is None or slate.first_start is None or now < slate.first_start:
        return False
    if tracking or started_for == slate.first_start:
        return False
    return stopped_at is None or stopped_at < slate.seen_at


class AutoStarter:
    CHECK_SECONDS = 60
    # The schedule is read again this often, so a changed start time is noticed; the start itself is
    # checked every minute against the last read.
    SLATE_REFRESH_SECONDS = 600

    def __init__(self, irc_bot, targets, clock=None):
        self.irc_bot = irc_bot
        self.targets = targets  # [(tracker, [channel, ...])]
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._slates = {}  # tracker -> Slate
        self._started_for = {}  # (tracker, channel) -> the first start it was started or found tracking for

    def start(self):
        threading.Thread(target=self.run, args=(threading.Event(),), daemon=True).start()

    def run(self, stop_event):
        while not stop_event.is_set():
            self.tick()
            stop_event.wait(self.CHECK_SECONDS)

    def tick(self):
        now = self.clock()
        for tracker, channels in self.targets:
            try:
                self._check(tracker, channels, now)
            except Exception as e:
                print(f"{tracker.DISPLAY_NAME}: automatic start check failed: {e}")
                traceback.print_exc()

    def _check(self, tracker, channels, now):
        slate = self._slate(tracker, now)
        for channel in channels:
            key = (tracker, channel)
            tracking = tracker.is_tracking(channel)
            if tracking and slate.first_start is not None and now >= slate.first_start:
                self._started_for[key] = slate.first_start
            if due(now, slate, tracking, self._started_for.get(key), tracker.last_stopped(channel)):
                self._started_for[key] = slate.first_start
                print(f"{tracker.DISPLAY_NAME}: {channel}: nobody started the tracker by the first game "
                      f"({slate.first_start.astimezone(tracker.HELSINKI_TZ):%H:%M}), starting it")
                tracker.start_automatically(self.irc_bot, channel)

    def _slate(self, tracker, now):
        slate = self._slates.get(tracker)
        if slate is not None and (now - slate.read_at).total_seconds() < self.SLATE_REFRESH_SECONDS:
            return slate
        try:
            items = tracker._fetch_today_items(None)
        except Exception as e:
            print(f"{tracker.DISPLAY_NAME}: automatic start: schedule read failed: {e}")
            items = None
        if items is None:
            # Keep what was known; try again after the refresh interval, not every minute.
            first_start = slate.first_start if slate else None
        else:
            first_start = tracker.first_start(items) if items else None
        seen_at = slate.seen_at if slate is not None and slate.first_start == first_start else now
        slate = Slate(first_start, seen_at, now)
        self._slates[tracker] = slate
        return slate
