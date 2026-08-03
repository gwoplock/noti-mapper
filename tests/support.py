"""Shared test doubles.

Fake plugins that record what the core asked them to do and let a test decide
what to answer. No network, no real services.
"""

import datetime
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from noti_mapper.clock import Clock, ManualClock
from noti_mapper.dispatcher import Dispatcher
from noti_mapper.messages import PushResultMessage
from noti_mapper.plugin import (
    EmitCallback,
    InputPlugin,
    ObservedEvent,
    OutputPlugin,
    OutputUpdate,
    PluginContext,
    PluginError,
    PluginHealth,
    RemoteBelief,
    RemoteState,
    UnlatchCallback,
)
from noti_mapper.storage import Database, HealthStatus, PluginKeyValueStore


def make_context(
    *,
    instance_name: str,
    database: Database,
    settings: Mapping[str, object] | None = None,
    clock: Clock | None = None,
    state_directory: Path | None = None,
) -> PluginContext:
    return PluginContext(
        instance_name=instance_name,
        settings={} if settings is None else settings,
        storage=PluginKeyValueStore(database=database, instance_name=instance_name),
        clock=clock if clock is not None else ManualClock(),
        logger=logging.getLogger(f"test.plugin.{instance_name}"),
        state_directory=(state_directory if state_directory is not None else database.path.parent),
    )


@dataclass
class FakeInputBehaviour:
    """What a FakeInput should do when the core talks to it."""

    catch_up_events: list[ObservedEvent] = field(default_factory=list)
    catch_up_raises: Exception | None = None
    health: PluginHealth = field(
        default_factory=lambda: PluginHealth(status=HealthStatus.OK, detail="")
    )


class FakeInput(InputPlugin):
    """An input that fires only when a test tells it to."""

    def __init__(self, *, context: PluginContext, emit: EmitCallback) -> None:
        super().__init__(context=context, emit=emit)
        self._behaviour = FakeInputBehaviour()
        self.started = False
        self.stopped = False
        self.catch_up_calls: list[datetime.datetime | None] = []
        self._release = threading.Event()

    def configure(self, behaviour: FakeInputBehaviour) -> None:
        self._behaviour = behaviour

    def start(self) -> None:
        self.started = True
        self._release.wait()

    def stop(self) -> None:
        self.stopped = True
        self._release.set()

    def health(self) -> PluginHealth:
        return self._behaviour.health

    def catch_up(self, since: datetime.datetime | None) -> list[ObservedEvent]:
        self.catch_up_calls.append(since)
        if self._behaviour.catch_up_raises is not None:
            raise self._behaviour.catch_up_raises
        return list(self._behaviour.catch_up_events)

    def fire(self, *, at: datetime.datetime, metadata: Mapping[str, str] | None = None) -> None:
        self.emit(ObservedEvent(occurred_at=at, metadata={} if metadata is None else metadata))


@dataclass
class FakeOutputBehaviour:
    """What a FakeOutput should do when the core talks to it."""

    apply_failures: int = 0
    apply_always_fails: bool = False
    query_result: RemoteState = field(
        default_factory=lambda: RemoteState(belief=RemoteBelief.UNKNOWN)
    )
    query_raises: Exception | None = None
    health: PluginHealth = field(
        default_factory=lambda: PluginHealth(status=HealthStatus.OK, detail="")
    )


class FakeOutput(OutputPlugin):
    """An output that records every apply and answers queries from a script."""

    def __init__(self, *, context: PluginContext, request_unlatch: UnlatchCallback) -> None:
        super().__init__(context=context, request_unlatch=request_unlatch)
        self._behaviour = FakeOutputBehaviour()
        self._lock = threading.Lock()
        self.applied: list[bool] = []
        self.updates: list[OutputUpdate] = []
        self.started = False
        self.stopped = False
        self.query_calls = 0

    def configure(self, behaviour: FakeOutputBehaviour) -> None:
        with self._lock:
            self._behaviour = behaviour

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def apply(self, update: OutputUpdate) -> None:
        with self._lock:
            if self._behaviour.apply_always_fails:
                raise PluginError(f"{self.context.instance_name} is unreachable")
            if self._behaviour.apply_failures > 0:
                self._behaviour.apply_failures -= 1
                raise PluginError(f"{self.context.instance_name} is unreachable")
            self.applied.append(update.state)
            self.updates.append(update)

    def query(self) -> RemoteState:
        with self._lock:
            self.query_calls += 1
            if self._behaviour.query_raises is not None:
                raise self._behaviour.query_raises
            return self._behaviour.query_result

    def health(self) -> PluginHealth:
        with self._lock:
            return self._behaviour.health

    def last_applied(self) -> bool | None:
        with self._lock:
            if not self.applied:
                return None
            return self.applied[-1]


class InlineDispatcher(Dispatcher):
    """A dispatcher that applies on the calling thread.

    Gives tests a deterministic ordering: dispatch() calls apply() and reports
    the result before returning, so Engine.drain() settles in a bounded number
    of rounds with no sleeping or polling.
    """

    def __init__(
        self, *, outputs: Mapping[str, OutputPlugin], report: Callable[[PushResultMessage], None]
    ) -> None:
        self._outputs: dict[str, OutputPlugin] = dict(outputs)
        self._report = report
        self.dispatched: list[tuple[str, bool]] = []
        self.last_update: OutputUpdate | None = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def set_outputs(self, outputs: Mapping[str, OutputPlugin]) -> None:
        self._outputs = dict(outputs)

    def dispatch(self, *, instance_name: str, update: OutputUpdate) -> None:
        self.dispatched.append((instance_name, update.state))
        self.last_update = update
        output = self._outputs.get(instance_name)
        if output is None:
            self._report(
                PushResultMessage(
                    instance_name=instance_name,
                    pushed_value=update.state,
                    succeeded=False,
                    error="output instance is no longer configured",
                )
            )
            return
        try:
            output.apply(update)
        except Exception as error:
            self._report(
                PushResultMessage(
                    instance_name=instance_name,
                    pushed_value=update.state,
                    succeeded=False,
                    error=f"{type(error).__name__}: {error}",
                )
            )
            return
        self._report(
            PushResultMessage(
                instance_name=instance_name, pushed_value=update.state, succeeded=True
            )
        )
