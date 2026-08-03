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
