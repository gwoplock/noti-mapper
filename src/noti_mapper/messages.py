"""The message types that travel on the core queue.

Everything that can change latch state arrives here: input events, unlatch
requests from outputs, the results of outbound pushes, reload requests, and
timer wake-ups. One queue, one consumer, which is what makes the single-writer
rule true by construction rather than by discipline.
"""

import datetime
from dataclasses import dataclass

from noti_mapper.config import Configuration
from noti_mapper.plugin import ObservedEvent


@dataclass(frozen=True)
class InputEventMessage:
    """An input instance observed something."""

    instance_name: str
    event: ObservedEvent


@dataclass(frozen=True)
class UnlatchRequestMessage:
    """An output instance is asking for its latches to be cleared.

    ``cause`` is free text for the log: "HomeKit switch written false",
    "PagerDuty incident resolved", "operator".
    """

    instance_name: str
    cause: str


@dataclass(frozen=True)
class PushResultMessage:
    """A dispatcher thread finished an outbound push.

    Results come back through the queue rather than being written where they
    happened, so that the core thread stays the only writer.
    """

    instance_name: str
    pushed_value: bool
    succeeded: bool
    error: str | None = None


@dataclass(frozen=True)
class ReconcileOutputMessage:
    """Retry reconciliation for one output that was unreachable at startup."""

    instance_name: str
    attempt: int


@dataclass(frozen=True)
class CatchUpMessage:
    """Retry downtime catch-up for one input that was unreachable at startup."""

    instance_name: str
    attempt: int
    since: datetime.datetime | None


@dataclass(frozen=True)
class ReloadMessage:
    """Configuration has been re-read and validated; adopt it."""

    configuration: Configuration


@dataclass(frozen=True)
class PollHealthMessage:
    """Time to ask every plugin how it is doing."""


@dataclass(frozen=True)
class ShutdownMessage:
    """Stop the core loop."""


CoreMessage = (
    InputEventMessage
    | UnlatchRequestMessage
    | PushResultMessage
    | ReconcileOutputMessage
    | CatchUpMessage
    | ReloadMessage
    | PollHealthMessage
    | ShutdownMessage
)
