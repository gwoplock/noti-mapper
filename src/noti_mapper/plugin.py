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

import datetime
import enum
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

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


EmitCallback = Callable[[ObservedEvent], None]
UnlatchCallback = Callable[[str], None]
