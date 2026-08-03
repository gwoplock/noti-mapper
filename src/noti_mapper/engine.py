"""The core: one thread, one queue, and the only writer of latch state.

Every mutation goes through one path on the core thread that

1. writes to SQLite durably, in a transaction,
2. recomputes which outputs changed as a result,
3. enqueues output pushes to the pending-push table,
4. logs the transition and its cause.

Output pushes cannot veto a state change. If PagerDuty is unreachable the latch
still sets and the lamp still turns on; the failed push retries. The inverse of
that rule is what makes most homegrown versions of this unreliable.
"""

import datetime
import logging
import queue
import threading
from dataclasses import dataclass

from noti_mapper.clock import Clock
from noti_mapper.config import DaemonSettings
from noti_mapper.dispatcher import Dispatcher
from noti_mapper.messages import (
    CoreMessage,
    InputEventMessage,
    PollHealthMessage,
    PushResultMessage,
    ShutdownMessage,
    UnlatchRequestMessage,
)
from noti_mapper.plugin import (
    InputPlugin,
    ObservedEvent,
    OutputPlugin,
)
from noti_mapper.rules import RuleGraph
from noti_mapper.storage import (
    EventKind,
    LatchRecord,
    Store,
)

# How long the core loop will sit in queue.get() with nothing else pending.
# Short enough that the systemd watchdog ping stays regular, long enough that
# an idle daemon is genuinely idle.
LOOP_MAX_INTERVAL_SECONDS: float = 5.0

HEALTH_POLL_INTERVAL_SECONDS: float = 30.0
LAST_SEEN_INTERVAL_SECONDS: float = 60.0

# A plugin that was unreachable during startup reconciliation is retried in the
# background rather than blocking startup.
RECONCILE_RETRY_INITIAL_SECONDS: float = 30.0
RECONCILE_RETRY_MAX_SECONDS: float = 900.0

# A cap on drain() rounds. Reaching it means the core is chasing its own tail,
# which is a bug worth a loud log line rather than a hang.
MAX_DRAIN_ROUNDS: int = 100


@dataclass
class PluginSet:
    """The live plugin instances, by name."""

    inputs: dict[str, InputPlugin]
    outputs: dict[str, OutputPlugin]

    @classmethod
    def empty(cls) -> "PluginSet":
        return cls(inputs={}, outputs={})


@dataclass(frozen=True)
class _ScheduledMessage:
    at: datetime.datetime
    message: CoreMessage


class Engine:
    """The core thread's state machine.

    Construct it, call :meth:`reconcile`, then call :meth:`run` on the thread
    that will own it. Everything else talks to it through :meth:`submit`.
    """

    def __init__(
        self,
        *,
        store: Store,
        graph: RuleGraph,
        plugins: PluginSet,
        dispatcher: Dispatcher,
        clock: Clock,
        settings: DaemonSettings,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._graph = graph
        self._plugins = plugins
        self._dispatcher = dispatcher
        self._clock = clock
        self._settings = settings
        self._log = logger if logger is not None else logging.getLogger("noti_mapper.engine")

        self._queue: queue.Queue[CoreMessage] = queue.Queue()
        self._timers: list[_ScheduledMessage] = []
        self._in_flight: set[str] = set()
        self._latches: dict[str, LatchRecord] = {}
        self._stopping = False
        self._ready = threading.Event()
        self._last_seen_written_at: datetime.datetime | None = None

        self.reload_latches()
        self._schedule(after_seconds=HEALTH_POLL_INTERVAL_SECONDS, message=PollHealthMessage())

    # -- accessors ------------------------------------------------------------

    @property
    def graph(self) -> RuleGraph:
        return self._graph

    @property
    def ready(self) -> threading.Event:
        """Set once startup reconciliation has finished."""
        return self._ready

    def latch_states(self) -> dict[str, bool]:
        states: dict[str, bool] = {}
        for name, record in self._latches.items():
            states[name] = record.state
        return states

    def reload_latches(self) -> None:
        """Refresh the in-memory latch cache from the database."""
        self._latches = {record.rule_name: record for record in self._store.latches()}

    # -- inbound --------------------------------------------------------------

    def submit(self, message: CoreMessage) -> None:
        """Hand a message to the core thread. Safe to call from any thread."""
        self._queue.put(message)

    def emit_callback(self, instance_name: str) -> "EmitFor":
        """The ``emit`` an input plugin instance is constructed with."""
        return EmitFor(engine=self, instance_name=instance_name)

    def unlatch_callback(self, instance_name: str) -> "UnlatchFor":
        """The ``request_unlatch`` an output plugin instance is constructed with."""
        return UnlatchFor(engine=self, instance_name=instance_name)

    def report_push_result(self, message: PushResultMessage) -> None:
        self.submit(message)

    def request_stop(self) -> None:
        self.submit(ShutdownMessage())

    # -- small helpers --------------------------------------------------------

    def _latch(self, rule_name: str) -> LatchRecord:
        existing = self._latches.get(rule_name)
        if existing is not None:
            return existing
        fresh = LatchRecord(
            rule_name=rule_name,
            state=False,
            set_at=None,
            cleared_at=None,
            trigger_count=0,
            last_cause=None,
        )
        self._latches[rule_name] = fresh
        return fresh

    def _write_latch(self, record: LatchRecord) -> None:
        self._store.write_latch(record)
        self._latches[record.rule_name] = record

    def _append_event(
        self,
        *,
        at: datetime.datetime,
        kind: EventKind,
        instance_name: str | None = None,
        rule_name: str | None = None,
        detail: str = "",
    ) -> None:
        self._store.append_event(
            at=at,
            kind=kind,
            instance_name=instance_name,
            rule_name=rule_name,
            detail=detail,
            max_rows=self._settings.event_log_max_rows,
        )


@dataclass(frozen=True)
class EmitFor:
    """The callable an input plugin instance calls to report an event.

    A small class rather than a closure so that the binding between an
    instance name and the queue is inspectable in a debugger and in a
    traceback.
    """

    engine: Engine
    instance_name: str

    def __call__(self, event: ObservedEvent) -> None:
        self.engine.submit(InputEventMessage(instance_name=self.instance_name, event=event))


@dataclass(frozen=True)
class UnlatchFor:
    """The callable an output plugin instance calls to request an unlatch."""

    engine: Engine
    instance_name: str

    def __call__(self, cause: str) -> None:
        self.engine.submit(UnlatchRequestMessage(instance_name=self.instance_name, cause=cause))
