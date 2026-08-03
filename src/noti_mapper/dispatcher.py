"""The outbound push pool.

The core thread must never block on a network call. It hands "apply this value
to this output" to this pool and carries on; the pool reports back through the
same core queue everything else arrives on, so the core thread stays the only
writer of persistent state.

This is also where v2's quiet hours will live. The dispatcher already sits
between a state change and the outbound I/O, which is the correct place to
defer a notification without deferring the latch. Nothing here needs to
anticipate it; it just must not be designed around its absence.
"""

import abc
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from noti_mapper.messages import PushResultMessage
from noti_mapper.plugin import OutputPlugin

ReportCallback = Callable[[PushResultMessage], None]


@dataclass(frozen=True)
class _PushRequest:
    instance_name: str
    value: bool


class Dispatcher(abc.ABC):
    """What the core needs from whatever performs outbound pushes.

    An interface rather than a concrete class because the core must be
    testable without threads: a test drives an implementation that applies on
    the calling thread and gets a deterministic ordering.
    """

    @abc.abstractmethod
    def start(self) -> None:
        """Begin accepting work."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop accepting work and wait for in-flight pushes."""

    @abc.abstractmethod
    def set_outputs(self, outputs: Mapping[str, OutputPlugin]) -> None:
        """Replace the output table, on reload."""

    @abc.abstractmethod
    def dispatch(self, *, instance_name: str, value: bool) -> None:
        """Queue a push. Must return promptly; the core thread is waiting."""
