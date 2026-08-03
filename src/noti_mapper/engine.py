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
from dataclasses import dataclass, replace

from noti_mapper.clock import Clock
from noti_mapper.config import DaemonSettings
from noti_mapper.dispatcher import Dispatcher
from noti_mapper.messages import (
    CatchUpMessage,
    CoreMessage,
    InputEventMessage,
    PollHealthMessage,
    PushResultMessage,
    ReconcileOutputMessage,
    ReloadMessage,
    ShutdownMessage,
    UnlatchRequestMessage,
)
from noti_mapper.plugin import (
    InputPlugin,
    ObservedEvent,
    OutputPlugin,
    PluginHealth,
)
from noti_mapper.rules import RuleGraph
from noti_mapper.storage import (
    EventKind,
    HealthRecord,
    HealthStatus,
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

    # -- the loop -------------------------------------------------------------

    def run(self) -> None:
        """Consume the queue until told to stop. Owns the calling thread."""
        self._log.info("core loop running")

        while not self._stopping:
            timeout = self._next_timeout()
            try:
                message = self._queue.get(timeout=timeout)
            except queue.Empty:
                self._tick()
                continue
            self._handle(message)
            if not self._stopping:
                self._tick()

        self._on_stop()

    def drain(self) -> int:
        """Process everything currently queued, then return.

        Each round processes the queue and runs one tick; a tick can dispatch
        pushes whose results land back on the queue, so rounds repeat until
        nothing is left. Returns the number of messages handled.

        This is what :meth:`run` does between blocking waits, factored out so
        that a test can step the core deterministically instead of racing it.
        """
        handled = 0
        for _ in range(MAX_DRAIN_ROUNDS):
            processed = False
            while True:
                try:
                    message = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._handle(message)
                handled += 1
                processed = True
            self._tick()
            if not processed and self._queue.empty():
                return handled
        self._log.error(
            "drain did not settle after %d rounds; something is generating "
            "messages faster than the core can retire them",
            MAX_DRAIN_ROUNDS,
        )
        return handled

    def _on_stop(self) -> None:
        now = self._clock.now()
        with self._store.database.transaction():
            self._store.write_last_seen_at(now)
            self._append_event(at=now, kind=EventKind.DAEMON_STOPPED)
        self._log.info("core loop stopped")

    def _next_timeout(self) -> float:
        now = self._clock.now()
        deadlines: list[datetime.datetime] = []

        for timer in self._timers:
            deadlines.append(timer.at)

        soonest_push = self._store.earliest_pending_attempt()
        if soonest_push is not None:
            deadlines.append(soonest_push)

        if not deadlines:
            return LOOP_MAX_INTERVAL_SECONDS

        seconds = (min(deadlines) - now).total_seconds()
        if seconds < 0.0:
            return 0.0
        return min(seconds, LOOP_MAX_INTERVAL_SECONDS)

    def _tick(self) -> None:
        now = self._clock.now()
        self._fire_due_timers(now)
        self._dispatch_due_pushes(now)
        self._maybe_write_last_seen(now)

    def _fire_due_timers(self, now: datetime.datetime) -> None:
        due: list[_ScheduledMessage] = []
        remaining: list[_ScheduledMessage] = []
        for timer in self._timers:
            if timer.at <= now:
                due.append(timer)
            else:
                remaining.append(timer)
        self._timers = remaining
        for timer in due:
            self._handle(timer.message)

    def _schedule(self, *, after_seconds: float, message: CoreMessage) -> None:
        at = self._clock.now() + datetime.timedelta(seconds=after_seconds)
        self._timers.append(_ScheduledMessage(at=at, message=message))

    def _maybe_write_last_seen(self, now: datetime.datetime) -> None:
        previous = self._last_seen_written_at
        if previous is not None:
            elapsed = (now - previous).total_seconds()
            if elapsed < LAST_SEEN_INTERVAL_SECONDS:
                return
        with self._store.database.transaction():
            self._store.write_last_seen_at(now)
        self._last_seen_written_at = now

    # -- message dispatch -----------------------------------------------------

    def _handle(self, message: CoreMessage) -> None:
        if isinstance(message, InputEventMessage):
            self._handle_input_event(message)
        elif isinstance(message, UnlatchRequestMessage):
            self._handle_unlatch(message)
        elif isinstance(message, PushResultMessage):
            self._handle_push_result(message)
        elif isinstance(message, ReloadMessage):
            self._handle_reload(message)
        elif isinstance(message, ReconcileOutputMessage):
            self._handle_reconcile_retry(message)
        elif isinstance(message, CatchUpMessage):
            self._handle_catch_up_retry(message)
        elif isinstance(message, PollHealthMessage):
            self._handle_poll_health()
        else:
            self._stopping = True

    def _dispatch_due_pushes(self, now: datetime.datetime) -> None:
        due = self._store.pending_pushes_due(now)
        if not due:
            return

        ready: list[tuple[str, bool]] = []
        with self._store.database.transaction():
            for push in due:
                if push.instance_name in self._in_flight:
                    continue
                if push.instance_name not in self._plugins.outputs:
                    self._store.delete_pending_push(push.instance_name)
                    continue

                # A push is always "apply current state", never "apply the
                # delta that failed". The value is recomputed here, at dispatch
                # time, so replaying a stale value is impossible.
                desired = self._graph.desired_output_state(
                    instance_name=push.instance_name, latches=self.latch_states()
                )
                if desired != push.target_value:
                    self._store.write_pending_push(replace(push, target_value=desired))
                ready.append((push.instance_name, desired))

        for instance_name, value in ready:
            self._in_flight.add(instance_name)
            self._dispatcher.dispatch(instance_name=instance_name, value=value)

    # -- health ---------------------------------------------------------------

    def _handle_poll_health(self) -> None:
        now = self._clock.now()
        with self._store.database.transaction():
            for name, plugin in self._plugins.inputs.items():
                self._record_health(name=name, health=self._ask_health(name, plugin), now=now)
            for name, output in self._plugins.outputs.items():
                self._record_health(name=name, health=self._ask_health(name, output), now=now)
        self._schedule(after_seconds=HEALTH_POLL_INTERVAL_SECONDS, message=PollHealthMessage())

    def _ask_health(self, name: str, plugin: InputPlugin | OutputPlugin) -> PluginHealth:
        try:
            return plugin.health()
        except BaseException as error:  # noqa: BLE001 - a plugin must not kill the core
            self._log.warning("health() on %s raised: %s", name, error, extra={"instance": name})
            return PluginHealth(
                status=HealthStatus.FAILED, detail=f"{type(error).__name__}: {error}"
            )

    def _record_health(self, *, name: str, health: PluginHealth, now: datetime.datetime) -> None:
        self._store.write_health(
            HealthRecord(
                instance_name=name, status=health.status, detail=health.detail, updated_at=now
            )
        )

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
