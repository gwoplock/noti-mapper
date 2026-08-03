"""File watcher: emits an event when a watched path appears or changes.

The other half of keeping the plugin interface honest. Where the webhook
receiver has no meaningful catch-up, this one has a real one -- a file that
appeared while the daemon was down is still there, with an mtime saying when.
Between them the two shipped non-IMAP inputs exercise both shapes of the
interface.

Polling rather than inotify, deliberately: no third-party dependency, no
per-watch descriptor limits, and no behavioural difference at the scale this
daemon works at. A handful of paths checked once a second costs nothing.

Debounce is not optional in practice. Editors write a file three times and
rsync writes it in pieces; without a quiet period a single save produces a
burst of events. The default holds an event back until the path has been
unchanged for two seconds.
"""

import datetime
import fnmatch
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from noti_mapper.plugin import (
    EmitCallback,
    InputPlugin,
    ObservedEvent,
    PluginContext,
    PluginHealth,
    clamp_metadata,
)
from noti_mapper.storage import HealthStatus

PLUGIN_NAME = "file-input"

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_DEBOUNCE_SECONDS = 2.0
SEEN_KEY_PREFIX = "seen."

_REQUIRED_KEYS = ("path",)
_OPTIONAL_KEYS = ("glob", "poll_seconds", "debounce_seconds", "emit_on_modify")


@dataclass(frozen=True)
class _Observation:
    """A path as the last poll saw it."""

    path: Path
    modified_at: float
    size: int


class FileInput(InputPlugin):
    """Watches a path, or a directory filtered by a glob."""

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        problems: list[str] = []
        for key in settings:
            if key not in _REQUIRED_KEYS and key not in _OPTIONAL_KEYS:
                problems.append(f'unknown setting "{key}"')

        path = settings.get("path")
        if path is None:
            problems.append('"path" is required')
        elif not isinstance(path, str) or not path.strip():
            problems.append('"path" must be a non-empty string')
        elif not Path(path).is_absolute():
            problems.append('"path" must be absolute')

        pattern = settings.get("glob")
        if pattern is not None and (not isinstance(pattern, str) or not pattern.strip()):
            problems.append('"glob" must be a non-empty string')

        for key in ("poll_seconds", "debounce_seconds"):
            value = settings.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                problems.append(f'"{key}" must be a positive number')

        modify = settings.get("emit_on_modify")
        if modify is not None and not isinstance(modify, bool):
            problems.append('"emit_on_modify" must be true or false')

        return problems

    def __init__(self, *, context: PluginContext, emit: EmitCallback) -> None:
        super().__init__(context=context, emit=emit)
        settings = context.settings
        self._root = Path(str(settings["path"]))
        raw_glob = settings.get("glob")
        self._glob = None if raw_glob is None else str(raw_glob)
        self._poll_seconds = float(str(settings.get("poll_seconds", DEFAULT_POLL_SECONDS)))
        self._debounce_seconds = float(
            str(settings.get("debounce_seconds", DEFAULT_DEBOUNCE_SECONDS))
        )
        self._emit_on_modify = bool(settings.get("emit_on_modify", True))
        self._log = context.logger

        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._status = HealthStatus.STARTING
        self._detail = "not yet polling"
        self._pending: dict[Path, _Observation] = {}
        self._quiet_since: dict[Path, float] = {}

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._set_health(HealthStatus.OK, f"watching {self._describe()}")
        self._log.info(
            "watching %s every %.1fs with a %.1fs debounce",
            self._describe(),
            self._poll_seconds,
            self._debounce_seconds,
            extra={"instance": self.context.instance_name},
        )

        while not self._stop.wait(timeout=self._poll_seconds):
            try:
                self.poll_once()
            except OSError as error:
                self._set_health(HealthStatus.DEGRADED, f"{type(error).__name__}: {error}")
                self._log.warning(
                    "polling %s failed: %s",
                    self._describe(),
                    error,
                    extra={"instance": self.context.instance_name},
                )
        self._set_health(HealthStatus.STOPPED, "not polling")

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> PluginHealth:
        with self._state_lock:
            return PluginHealth(status=self._status, detail=self._detail)

    def catch_up(self, since: datetime.datetime | None) -> list[ObservedEvent]:
        """A file that appeared while the daemon was down is still there.

        The event's timestamp is the file's mtime, which is when the thing
        actually happened -- not now, when we noticed. Reconciliation compares
        that against an output's clear time, so getting it from the filesystem
        rather than the clock is the whole point.
        """
        events: list[ObservedEvent] = []
        for observation in self._observe():
            if not self._is_new(observation):
                continue
            if since is not None and _as_datetime(observation.modified_at) < since:
                # Seen-state says it is new, but it predates the last run. That
                # happens when the cursor was purged; do not fire for history.
                self._remember(observation)
                continue
            events.append(self._event_for(observation, cause="catch-up"))
            self._remember(observation)
        return events

    # -- polling --------------------------------------------------------------

    def poll_once(self) -> None:
        """Run one polling pass: notice changes, and emit anything that settled.

        Public because the loop in start() is a timer around this and nothing
        more, and because a test driving it with a manual clock is the only way
        to check debounce behaviour without sleeping.
        """
        now = self.context.clock.monotonic()
        current: dict[Path, _Observation] = {}
        for observation in self._observe():
            current[observation.path] = observation

        for path, observation in current.items():
            if not self._is_new(observation):
                self._forget_pending(path)
                continue

            previous = self._pending.get(path)
            if previous is None or previous != observation:
                # Still changing. Restart the quiet period rather than firing
                # in the middle of an editor's or rsync's write burst.
                self._pending[path] = observation
                self._quiet_since[path] = now
                continue

            if now - self._quiet_since[path] < self._debounce_seconds:
                continue

            self.emit(self._event_for(observation, cause="watch"))
            self._remember(observation)
            self._forget_pending(path)

        for path in list(self._pending):
            if path not in current:
                self._forget_pending(path)

    def _forget_pending(self, path: Path) -> None:
        self._pending.pop(path, None)
        self._quiet_since.pop(path, None)

    def _observe(self) -> list[_Observation]:
        candidates: list[Path] = []
        if self._glob is None:
            if self._root.is_file():
                candidates.append(self._root)
        elif self._root.is_dir():
            for entry in sorted(self._root.iterdir()):
                if entry.is_file() and fnmatch.fnmatch(entry.name, self._glob):
                    candidates.append(entry)

        observations: list[_Observation] = []
        for candidate in candidates:
            try:
                info = candidate.stat()
            except OSError:
                continue
            observations.append(
                _Observation(path=candidate, modified_at=info.st_mtime, size=info.st_size)
            )
        return observations

    # -- what counts as new ---------------------------------------------------

    def _is_new(self, observation: _Observation) -> bool:
        stored = self.context.storage.get(self._seen_key(observation.path))
        if stored is None:
            return True
        if not self._emit_on_modify:
            return False
        return stored != self._fingerprint(observation)

    def _remember(self, observation: _Observation) -> None:
        self.context.storage.set(self._seen_key(observation.path), self._fingerprint(observation))

    def _seen_key(self, path: Path) -> str:
        return SEEN_KEY_PREFIX + str(path)

    def _fingerprint(self, observation: _Observation) -> str:
        return f"{observation.modified_at:.6f}:{observation.size}"

    def _event_for(self, observation: _Observation, *, cause: str) -> ObservedEvent:
        return ObservedEvent(
            occurred_at=_as_datetime(observation.modified_at),
            metadata=clamp_metadata(
                {
                    "path": str(observation.path),
                    "size": str(observation.size),
                    "cause": cause,
                }
            ),
        )

    def _describe(self) -> str:
        if self._glob is None:
            return str(self._root)
        return f"{self._root}/{self._glob}"

    def _set_health(self, status: HealthStatus, detail: str) -> None:
        with self._state_lock:
            self._status = status
            self._detail = detail


def _as_datetime(timestamp: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(timestamp, tz=datetime.UTC)


INPUT_PLUGIN = FileInput
