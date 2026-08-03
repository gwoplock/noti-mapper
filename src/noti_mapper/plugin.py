"""The plugin interfaces.

Two abstract base classes, both with explicit ``@abstractmethod`` declarations
rather than duck typing. A plugin that does not implement the interface fails
at construction with a clear message instead of at 3am with an AttributeError.

Outputs are bidirectional, which is unusual for a sink abstraction and is the
crux of this project. An output both renders latch state and *sources* unlatch
requests: you flip the HomeKit switch off, or you resolve the PagerDuty
incident, and that is how the latch clears. Do not model an output as
write-only -- both shipped output plugins need the reverse channel.
"""

import abc
import datetime
import enum
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from noti_mapper.clock import Clock
from noti_mapper.storage import HealthStatus, PluginKeyValueStore

# The maximum length of any single metadata value. Metadata is rendered into
# log lines and into PagerDuty payloads; a webhook body has no natural bound.
MAX_METADATA_VALUE_LENGTH: int = 1024


class PluginError(Exception):
    """A plugin failed at something the core asked it to do.

    Raising this from :meth:`OutputPlugin.apply` means the push failed and
    should be retried. It never means the state change is refused -- output
    pushes cannot veto a state change.
    """


@dataclass(frozen=True)
class ObservedEvent:
    """An edge: something happened at a point in time.

    ``metadata`` is free-form context the input attaches so that outputs can
    render something useful -- sender, subject, message date for IMAP; the
    request body for a webhook. Values are strings because they end up in log
    lines and JSON payloads.
    """

    occurred_at: datetime.datetime
    metadata: Mapping[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        """A one-line rendering for logs and ``status`` output."""
        if not self.metadata:
            return "(no metadata)"
        parts: list[str] = []
        for key in sorted(self.metadata):
            parts.append(f"{key}={self.metadata[key]!r}")
        return " ".join(parts)


def clamp_metadata(metadata: Mapping[str, str]) -> dict[str, str]:
    """Truncate over-long metadata values.

    Plugins are expected to call this on anything whose size they do not
    control, a webhook body being the obvious case.
    """
    clamped: dict[str, str] = {}
    for key, value in metadata.items():
        if len(value) > MAX_METADATA_VALUE_LENGTH:
            clamped[key] = value[:MAX_METADATA_VALUE_LENGTH] + "...[truncated]"
        else:
            clamped[key] = value
    return clamped


class RemoteBelief(enum.Enum):
    """What an output's remote end currently thinks the state is."""

    ACTIVE = "active"
    CLEARED = "cleared"
    # The remote could not be reached, or has no opinion yet. Reconciliation
    # falls back to persisted state rather than guessing.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RemoteState:
    """An output's report of what its remote end believes.

    ``cleared_at`` matters enormously: reconciliation compares it against the
    timestamps of events observed during downtime. An output reporting CLEARED
    without a timestamp is treated as "cleared at an unknown time in the past",
    which loses to any input event.
    """

    belief: RemoteBelief
    cleared_at: datetime.datetime | None = None


@dataclass(frozen=True)
class OutputUpdate:
    """What an output is being asked to render.

    A bare boolean is not enough. The rule semantics deliberately keep counting
    re-triggers of an already-set latch so that an output can render "3
    packages waiting", and the PagerDuty payload is supposed to carry the
    subject line that caused the alert. Neither is possible if the only thing
    crossing this boundary is True or False.

    ``rules`` lists the rules currently driving this output true, which is what
    makes the coupling visible to a user looking at an incident: an output that
    is true because of two rules says so.
    """

    state: bool
    cause: str = ""
    detail: str = ""
    trigger_count: int = 0
    rules: tuple[str, ...] = ()
    since: datetime.datetime | None = None

    def summary(self) -> str:
        """A one-line rendering for an alert title or a log line."""
        if not self.state:
            return "cleared"
        if self.trigger_count > 1:
            return f"{self.detail or 'latched'} ({self.trigger_count} triggers)"
        return self.detail or "latched"


@dataclass(frozen=True)
class PluginHealth:
    """A plugin instance's own opinion of how it is doing."""

    status: HealthStatus
    detail: str = ""


@dataclass(frozen=True)
class PluginContext:
    """Everything a plugin instance is given at construction.

    ``storage`` is durable per-instance key/value scratch space backed by the
    same SQLite file as everything else -- one thing to back up, one thing to
    migrate. Plugins do not manage their own files.
    """

    instance_name: str
    settings: Mapping[str, object]
    storage: PluginKeyValueStore
    clock: Clock
    logger: logging.Logger
    # Where a plugin may keep files that will not fit in key/value storage --
    # HAP pairing state being the case that forces this to exist. Prefer
    # ``storage``; use this only when a library insists on a path.
    state_directory: Path = Path("/var/lib/noti-mapper")

    def state_path(self, filename: str) -> Path:
        """A per-instance path under the state directory, with the parent created.

        Instance names may contain spaces and are not otherwise sanitized here,
        because the name character set already excludes slashes and control
        characters.
        """
        directory = self.state_directory / "plugins" / self.instance_name
        directory.mkdir(parents=True, exist_ok=True)
        return directory / filename


EmitCallback = Callable[[ObservedEvent], None]
UnlatchCallback = Callable[[str], None]


class InputPlugin(abc.ABC):
    """Watches something and emits events.

    One thread per input instance. Blocking in :meth:`start` is expected and
    correct -- an IMAP IDLE loop belongs in its own thread and needs no async
    plumbing.
    """

    def __init__(self, *, context: PluginContext, emit: EmitCallback) -> None:
        self._context = context
        self._emit_callback = emit

    @property
    def context(self) -> PluginContext:
        return self._context

    def emit(self, event: ObservedEvent) -> None:
        """Report an event to the core. Safe to call from the plugin's thread."""
        self._emit_callback(event)

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        """Return every problem with an instance's ``config`` block.

        Returns a list rather than raising so that configuration validation can
        report all problems across all instances in one pass. The default
        accepts anything; plugins with required settings override it.
        """
        del settings
        return []

    @abc.abstractmethod
    def start(self) -> None:
        """Begin watching. Called on the instance's own thread."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Ask the plugin to return from :meth:`start`. Called from another thread."""

    @abc.abstractmethod
    def health(self) -> PluginHealth:
        """Report health. Called from the core thread, so do not block."""

    @abc.abstractmethod
    def catch_up(self, since: datetime.datetime | None) -> list[ObservedEvent]:
        """Return events observed while the daemon was stopped, with timestamps.

        ``since`` is the last time the daemon is known to have been running, or
        None if there is no such record. The timestamps on the returned events
        are what reconciliation compares against an output's clear time, so
        they must be the time the event actually happened -- the message date,
        not the time it was noticed.

        Returning an empty list because the remote is unreachable is
        acceptable; raise :class:`PluginError` to say so explicitly and have it
        logged and retried.
        """


class OutputPlugin(abc.ABC):
    """Renders latch state, and sources unlatch requests.

    The reverse channel is not optional. An output that cannot tell the core
    "the operator cleared this" leaves the user with no way to unlatch.
    """

    def __init__(self, *, context: PluginContext, request_unlatch: UnlatchCallback) -> None:
        self._context = context
        self._unlatch_callback = request_unlatch

    @property
    def context(self) -> PluginContext:
        return self._context

    def request_unlatch(self, cause: str) -> None:
        """Ask the core to clear every latch this output contributes to.

        Safe to call from any thread the plugin owns; the request goes onto the
        same queue as input events and is handled by the same serialized path.
        Idempotent by construction -- a request against already-cleared state
        is a no-op, not a second transition -- so a poller that notices a clear
        it caused itself does no harm.
        """
        self._unlatch_callback(cause)

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        """Return every problem with an instance's ``config`` block."""
        del settings
        return []

    @abc.abstractmethod
    def start(self) -> None:
        """Start whatever threads or servers the output needs. Must not block."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Shut down. Must be safe to call whether or not start() succeeded."""

    @abc.abstractmethod
    def apply(self, update: OutputUpdate) -> None:
        """Drive the remote to ``update.state``.

        Called from a dispatcher thread, never the core thread, so blocking on
        the network here is fine. Raise :class:`PluginError` on failure; the
        push is retried with backoff and the latch is unaffected.

        Must be idempotent: the same value may be applied repeatedly, and is
        during reconciliation.
        """

    @abc.abstractmethod
    def query(self) -> RemoteState:
        """Ask the remote what it currently believes, for reconciliation.

        Return ``RemoteBelief.UNKNOWN`` when the remote cannot be reached.
        Raising is also acceptable and is treated the same way.
        """

    @abc.abstractmethod
    def health(self) -> PluginHealth:
        """Report health. Called from the core thread, so do not block."""
