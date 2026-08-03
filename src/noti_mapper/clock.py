"""Time, behind an interface, so tests can control it.

Reconciliation turns on comparing an input event's timestamp against the time
an output reported being cleared. That comparison is the single most important
thing to get right in this project, and it is untestable if the code reaches
for ``datetime.now()`` directly.
"""

import abc
import datetime
import threading
import time


class Clock(abc.ABC):
    """Wall-clock and monotonic time."""

    @abc.abstractmethod
    def now(self) -> datetime.datetime:
        """Current wall-clock time, timezone-aware and in UTC."""

    @abc.abstractmethod
    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, never going backwards."""

    @abc.abstractmethod
    def sleep(self, seconds: float) -> None:
        """Block for ``seconds``."""


class SystemClock(Clock):
    """The real clock."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(tz=datetime.UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ManualClock(Clock):
    """A clock that only moves when a test moves it.

    Lives beside the real one rather than in the test tree because plugins
    need it too, and a plugin author writing tests should not have to
    reimplement it.
    """

    def __init__(self, *, start: datetime.datetime | None = None) -> None:
        if start is None:
            start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        if start.tzinfo is None:
            raise ValueError("ManualClock needs a timezone-aware start time")
        self._lock = threading.Lock()
        self._now = start.astimezone(datetime.UTC)
        self._monotonic = 0.0

    def now(self) -> datetime.datetime:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._monotonic

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now = self._now + datetime.timedelta(seconds=seconds)
            self._monotonic += seconds
