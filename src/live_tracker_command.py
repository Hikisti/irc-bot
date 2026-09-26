import datetime
import threading
import traceback
from zoneinfo import ZoneInfo

from base_command import BaseCommand
from http_session import make_session


class LiveTrackerCommand(BaseCommand):
    """Shared engine behind LiigaCommand and PesisCommand: both live-track
    "today's games/matches" in a channel via a background polling thread,
    with an identical start/stop/next lifecycle, initial-lookup shape
    (_run) and next-period lookup (_run_next) around a sport-specific
    fetch+announce core. Only _poll_once stays entirely on each subclass
    - confirmed, by close side-by-side reading of both, to genuinely
    differ in shape (Pesis resolves+threads a series_id that Liiga has no
    equivalent of; they also differ in iteration direction and
    new/missing-item handling).

    A subclass must set: ALIASES/CHANNELS (BaseCommand's own contract -
    every subclass here restricts itself to specific channels, unlike
    most other commands), DISPLAY_NAME, COMMAND_NAME, CACHE_SLUG,
    TRACKED_NOUN (plural, e.g. "games"/"matches"), TRACKED_NOUN_COUNTED
    (the same, but with an irregular count suffix for _run's "Tracking N
    ..." message, e.g. "game(s)"/"match(es)"), PERIOD_NOUN (e.g.
    "gameday"/"matchday"), STATE_KEY (the per-channel dict key holding
    tracked-item state, e.g. "games"/"matches" - kept distinct rather
    than unified since existing tests key into it directly), and
    REQUIRES_CONTEXT (True if _resolve_context() must succeed before
    _run/_run_next can proceed - Pesis's series_id lookup; Liiga has no
    such step). It must implement _poll_once(irc_bot, channel,
    *context_args), the _run hooks _fetch_today_items(context) and
    _build_initial_state(items), and the _run_next hooks
    _fetch_next_period(context) and _format_period_summary(items) (both
    _run and _run_next also share _format_period_summary); only if the
    default doesn't fit, _format_not_found_message() and
    _resolve_context().
    """

    needs_irc_context = True

    HELSINKI_TZ = ZoneInfo("Europe/Helsinki")
    POLL_INTERVAL_SECONDS = 30
    REQUEST_TIMEOUT_SECONDS = 10
    # How long before the earliest today's-item's scheduled start
    # <command> start refuses to begin polling - confirmed live users
    # start tracking well over an hour before the first game, which just
    # burns API calls/poll cycles for nothing until something's actually
    # happening. A subclass sets START_TIME_KEY to the field name its own
    # items use for their ISO-8601 start timestamp (e.g. Liiga's "start",
    # Pesis's "date").
    EARLY_START_GUARD_MINUTES = 15
    START_TIME_KEY = None
    # Bound for "<command> next"'s day-by-day search when today's own
    # games/matches are all already finished (or there's an API-specific
    # hint that doesn't apply) - see each subclass's own next-period fetch
    # for the exact search it bounds.
    NEXT_SEARCH_MAX_DAYS = 21

    REQUIRES_CONTEXT = False

    def __init__(self):
        self.session = make_session(f"KukistiBot-{self.CACHE_SLUG}/1.0")
        self._lock = threading.Lock()
        self._channels = {}  # channel -> {"stop_event", "thread", STATE_KEY: ...}

    def execute(self, args=None, irc_bot=None, channel=None, **kwargs) -> str:
        arg = (args or "").strip().lower()

        if arg == "start":
            return self._start(irc_bot, channel)
        elif arg == "stop":
            return self._stop(channel)
        elif arg == "next":
            return self._next(irc_bot, channel)
        return f"Usage: {self.COMMAND_NAME} start | {self.COMMAND_NAME} stop | {self.COMMAND_NAME} next"

    # ---- start / stop -----------------------------------------------

    def _start(self, irc_bot, channel):
        if irc_bot is None or channel is None:
            return "Error: live tracking is unavailable without channel context."

        with self._lock:
            if channel in self._channels:
                return f"Already tracking live {self.DISPLAY_NAME} {self.TRACKED_NOUN} in this channel."
            # Reserve the slot up front (before any network I/O) so a second
            # start can't race in while the first lookup is in flight.
            stop_event = threading.Event()
            self._channels[channel] = {"stop_event": stop_event, "thread": None, self.STATE_KEY: {}}

        thread = threading.Thread(
            target=self._run,
            args=(irc_bot, channel, stop_event),
            daemon=True,
        )
        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None and entry["stop_event"] is stop_event:
                entry["thread"] = thread
        thread.start()

        return f"Checking today's {self.DISPLAY_NAME} {self.TRACKED_NOUN}..."

    def _stop(self, channel):
        with self._lock:
            entry = self._channels.pop(channel, None)
        if not entry:
            return f"Not currently tracking {self.DISPLAY_NAME} {self.TRACKED_NOUN} in this channel."
        entry["stop_event"].set()
        return f"Stopped live {self.DISPLAY_NAME} tracking."

    def _next(self, irc_bot, channel):
        if irc_bot is None or channel is None:
            return "Error: this command needs channel context."

        # A one-shot lookup, not persistent tracking - no need to reserve a
        # channel slot the way _start does. Still runs on a background
        # thread so a slow API can't stall the bot.
        threading.Thread(
            target=self._run_next,
            args=(irc_bot, channel),
            daemon=True,
        ).start()

        return f"Checking the next {self.DISPLAY_NAME} {self.PERIOD_NOUN}..."

    # ---- "next" lookup (hook-based: see _fetch_next_period et al.) ----

    def _resolve_context(self):
        """Returns whatever _fetch_next_period/_run need to do their
        lookup (e.g. Pesis's resolved series_id), or None if there's
        nothing to resolve (Liiga). Only checked against REQUIRES_CONTEXT
        when it's True."""
        return None

    def _format_not_found_message(self) -> str:
        return f"No upcoming {self.DISPLAY_NAME} {self.TRACKED_NOUN} found."

    def _run_next(self, irc_bot, channel):
        try:
            context = self._resolve_context()
        except Exception as e:
            print(f"{self.DISPLAY_NAME} context resolution error: {e}")
            context = None

        if self.REQUIRES_CONTEXT and context is None:
            self._safe_send(irc_bot, channel, f"Error: could not reach the {self.DISPLAY_NAME} API.")
            return

        try:
            date_str, items = self._fetch_next_period(context)
        except Exception as e:
            print(f"{self.DISPLAY_NAME} next-{self.PERIOD_NOUN} fetch error: {e}")
            date_str, items = None, None

        if items is None:
            self._safe_send(irc_bot, channel, f"Error: could not reach the {self.DISPLAY_NAME} API.")
            return
        if not items:
            self._safe_send(irc_bot, channel, self._format_not_found_message())
            return

        label = self._format_date_label(date_str)
        summary = self._format_period_summary(items.values())
        self._safe_send(irc_bot, channel, f"Next {self.DISPLAY_NAME} {self.PERIOD_NOUN} ({label}): {summary}")

    # ---- background thread entry point ---------------------------------

    def _run(self, irc_bot, channel, stop_event):
        """Runs entirely on a background thread: resolves context (if
        REQUIRES_CONTEXT), does the initial lookup, reports what's being
        tracked (or bails out), then polls until everything's done or
        <command> stop is called."""
        context = None
        if self.REQUIRES_CONTEXT:
            try:
                context = self._resolve_context()
            except Exception as e:
                print(f"{self.DISPLAY_NAME} context resolution error: {e}")
                context = None

            if context is None:
                self._drop_if_current(channel, stop_event)
                self._safe_send(irc_bot, channel, f"Error: could not reach the {self.DISPLAY_NAME} API.")
                return

        try:
            items = self._fetch_today_items(context)
        except Exception as e:
            print(f"{self.DISPLAY_NAME} initial fetch error: {e}")
            items = None

        if items is None:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, f"Error: could not reach the {self.DISPLAY_NAME} API.")
            return

        if not items:
            self._drop_if_current(channel, stop_event)
            self._safe_send(irc_bot, channel, f"No {self.DISPLAY_NAME} {self.TRACKED_NOUN} scheduled today.")
            return

        earliest = min(
            (dt for dt in (
                self._parse_start_dt(item.get(self.START_TIME_KEY)) for item in items.values()
            ) if dt is not None),
            default=None,
        )
        if earliest is not None:
            guard_until = earliest - datetime.timedelta(minutes=self.EARLY_START_GUARD_MINUTES)
            now = datetime.datetime.now(self.HELSINKI_TZ)
            if now < guard_until:
                self._drop_if_current(channel, stop_event)
                self._safe_send(irc_bot, channel, (
                    f"Too early to track — {self.DISPLAY_NAME} play starts at "
                    f"{earliest.strftime('%H:%M')}. You can run {self.COMMAND_NAME} start "
                    f"again from {guard_until.strftime('%H:%M')} onward."
                ))
                return

        state = self._build_initial_state(items)
        if not self._commit_initial_state(channel, stop_event, state):
            return  # stopped (or superseded) before the lookup finished

        summary = self._format_period_summary(items.values())
        self._safe_send(
            irc_bot, channel,
            f"Tracking {len(items)} {self.DISPLAY_NAME} {self.TRACKED_NOUN_COUNTED} today: {summary}",
        )

        poll_args = (context,) if self.REQUIRES_CONTEXT else ()
        self._poll_loop(irc_bot, channel, stop_event, *poll_args)

    def _fetch_today_items(self, context):
        """Returns {item_id: item_dict} for today, or None on failure (API
        unreachable). `context` is whatever _resolve_context() returned
        (None if REQUIRES_CONTEXT is False)."""
        raise NotImplementedError

    def _build_initial_state(self, items) -> dict:
        """Builds the {item_id: state_dict} to seed as the channel's
        initial tracked state from `items` (the dict _fetch_today_items()
        just returned) - e.g. Liiga's per-game goal-count snapshot, or
        Pesis's per-match snapshot plus its extra seeded event/roster
        data."""
        raise NotImplementedError

    def _drop_if_current(self, channel, stop_event):
        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None and entry["stop_event"] is stop_event:
                del self._channels[channel]

    # ---- per-channel tracked-item state (STATE_KEY), lock-guarded ------

    def _commit_initial_state(self, channel, stop_event, state) -> bool:
        """Stores `state` (the just-seeded initial snapshot) as the
        channel's tracked state - but only if this _run() is still the
        current one for it (a stop, or a second start superseding it, can
        already have happened while the initial fetch was in flight).
        Returns False if the caller should bail out without announcing or
        polling; True if it's safe to continue."""
        with self._lock:
            entry = self._channels.get(channel)
            if entry is None or entry["stop_event"] is not stop_event:
                return False
            entry[self.STATE_KEY] = state
            return True

    def _get_state(self, channel):
        """Returns the channel's current tracked state, or None if it's no
        longer being tracked (stopped, or superseded, since the last
        poll) - distinct from an empty-but-still-tracked state ({})."""
        with self._lock:
            entry = self._channels.get(channel)
            return entry[self.STATE_KEY] if entry is not None else None

    def _set_state(self, channel, state):
        """Stores `state` as the channel's new tracked state, if it's
        still being tracked (a no-op otherwise, e.g. stopped mid-poll)."""
        with self._lock:
            entry = self._channels.get(channel)
            if entry is not None:
                entry[self.STATE_KEY] = state

    def _safe_send(self, irc_bot, channel, message):
        """Never let a broken connection/socket take the polling thread down."""
        try:
            irc_bot.send_message(channel, message)
        except Exception as e:
            print(f"{self.DISPLAY_NAME}: failed to send message to {channel}: {e}")

    # ---- polling loop -------------------------------------------------

    def _poll_loop(self, irc_bot, channel, stop_event, *context_args):
        while not stop_event.is_set():
            try:
                all_ended = self._poll_once(irc_bot, channel, *context_args)
            except Exception as e:
                # Last line of defense against anything not anticipated by
                # a narrower handler below - print the traceback too, not
                # just str(e), since nothing more specific caught this one.
                print(f"{self.DISPLAY_NAME} poll error in {channel}: {e}")
                traceback.print_exc()
                all_ended = False

            if all_ended:
                self._drop_if_current(channel, stop_event)
                self._safe_send(
                    irc_bot, channel,
                    f"All of today's {self.DISPLAY_NAME} {self.TRACKED_NOUN} have finished. Live tracking stopped.",
                )
                return

            stop_event.wait(self.POLL_INTERVAL_SECONDS)

    def _poll_once(self, irc_bot, channel, *context_args) -> bool:
        raise NotImplementedError

    # ---- start-time summary (e.g. "17:00 A-B, C-D | 18:30 E-F") --------

    def _parse_start_dt(self, iso_str):
        """Scheduled start time as an aware Helsinki-local datetime, or
        None if there's no usable ISO-8601 timestamp."""
        if not iso_str:
            return None
        try:
            dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        return dt.astimezone(self.HELSINKI_TZ)

    def _start_time_label(self, iso_str):
        """Scheduled start time in Helsinki local time as 'HH:MM', or None
        if there's no usable ISO-8601 timestamp."""
        dt = self._parse_start_dt(iso_str)
        return dt.strftime("%H:%M") if dt else None

    def _format_start_time_summary(self, items, start_key, name_fn) -> str:
        """Groups items by scheduled start time (items[start_key], an
        ISO-8601 string), e.g. '17:00 HIFK-Ilves, Tappara-Kärpät | 18:30
        JYP-Lukko'. name_fn(item) builds each item's own display name."""
        groups = {}
        order = []
        for item in items:
            label = self._start_time_label(item.get(start_key)) or "??:??"
            name = name_fn(item)
            if label not in groups:
                groups[label] = []
                order.append(label)
            groups[label].append(name)

        order.sort(key=lambda label: (label == "??:??", label))
        return " | ".join(f"{label} {', '.join(groups[label])}" for label in order)

    # ---- date label (byte-identical between subclasses) ----------------

    def _format_date_label(self, date_str) -> str:
        try:
            target = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return date_str or "unknown date"

        today = datetime.datetime.now(self.HELSINKI_TZ).date()
        if target == today:
            return "today"
        if target == today + datetime.timedelta(days=1):
            return "tomorrow"
        return target.strftime("%a %d/%m")
