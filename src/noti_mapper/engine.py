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
from collections.abc import Mapping, Sequence
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
    OutputUpdate,
    PluginHealth,
    RemoteBelief,
    RemoteState,
)
from noti_mapper.reconcile import ReconcileOutcome, resolve_rule
from noti_mapper.rules import RuleGraph
from noti_mapper.storage import (
    EventKind,
    HealthRecord,
    HealthStatus,
    LatchRecord,
    OutputStateRecord,
    PendingPush,
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
        self._heartbeat: datetime.datetime | None = None

        self.reload_latches()
        # Poll health on the first tick rather than waiting a full interval, so
        # that 'noti-mapper status' has something to say immediately after a
        # start rather than reporting "(none reported)" for the first 30s.
        self._schedule(after_seconds=0.0, message=PollHealthMessage())

    # -- accessors ------------------------------------------------------------

    @property
    def graph(self) -> RuleGraph:
        return self._graph

    @property
    def ready(self) -> threading.Event:
        """Set once startup reconciliation has finished."""
        return self._ready

    @property
    def heartbeat(self) -> datetime.datetime | None:
        """When the core loop last completed a tick.

        The watchdog reads this. A process that is alive with a dead core loop
        is exactly the silent failure this daemon must not have.
        """
        return self._heartbeat

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

        try:
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
        finally:
            # This thread opened its own SQLite connection; nobody else can
            # close it, because sqlite3 refuses cross-thread use.
            self._store.database.close()

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
        self._heartbeat = now
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

    # -- input events ---------------------------------------------------------

    def _handle_input_event(self, message: InputEventMessage) -> None:
        now = self._clock.now()
        rules = self._graph.rules_for_input(message.instance_name)
        event = message.event

        with self._store.database.transaction():
            self._append_event(
                at=now,
                kind=EventKind.INPUT_EVENT,
                instance_name=message.instance_name,
                detail=event.summary(),
            )

            if not rules:
                self._log.warning(
                    "event from %s matches no rule; nothing to latch",
                    message.instance_name,
                    extra={"instance": message.instance_name},
                )
                return

            for rule in rules:
                self._apply_trigger(
                    rule_name=rule.name,
                    at=event.occurred_at,
                    cause=message.instance_name,
                    summary=event.summary(),
                    now=now,
                )

            affected = self._graph.outputs_affected_by([rule.name for rule in rules])
            self._sync_outputs(affected, now=now)

    def _apply_trigger(
        self,
        *,
        rule_name: str,
        at: datetime.datetime,
        cause: str,
        summary: str,
        now: datetime.datetime,
    ) -> None:
        record = self._latch(rule_name)

        if record.state:
            # Re-triggering an already-set latch is not a no-op at the record
            # level -- the counter and timestamp move, so an output can render
            # "3 packages waiting" -- but it produces no output transition.
            updated = replace(
                record,
                set_at=at,
                trigger_count=record.trigger_count + 1,
                last_cause=cause,
                last_detail=summary,
            )
            self._write_latch(updated)
            self._append_event(
                at=now,
                kind=EventKind.LATCH_RETRIGGERED,
                rule_name=rule_name,
                instance_name=cause,
                detail=f"count={updated.trigger_count} {summary}",
            )
            self._log.info(
                "rule %r re-triggered by %r (count %d); no output transition",
                rule_name,
                cause,
                updated.trigger_count,
                extra={"rule": rule_name, "instance": cause, "count": updated.trigger_count},
            )
            return

        updated = replace(
            record,
            state=True,
            set_at=at,
            trigger_count=record.trigger_count + 1,
            last_cause=cause,
            last_detail=summary,
        )
        self._write_latch(updated)
        self._append_event(
            at=now,
            kind=EventKind.LATCH_SET,
            rule_name=rule_name,
            instance_name=cause,
            detail=summary,
        )
        self._log.info(
            "rule %r latched by %r: %s",
            rule_name,
            cause,
            summary,
            extra={"rule": rule_name, "instance": cause, "state": True},
        )

    # -- unlatch --------------------------------------------------------------

    def _handle_unlatch(self, message: UnlatchRequestMessage) -> None:
        now = self._clock.now()
        rules = self._graph.rules_for_output(message.instance_name)
        set_rules = [rule for rule in rules if self._latch(rule.name).state]

        with self._store.database.transaction():
            if not set_rules:
                # Loop prevention: clearing via output A resolves output B,
                # whose next poll observes the clear and issues its own unlatch.
                # That second request must be a no-op, not a second transition.
                self._append_event(
                    at=now,
                    kind=EventKind.UNLATCH_IGNORED,
                    instance_name=message.instance_name,
                    detail=message.cause,
                )
                self._log.debug(
                    "unlatch from %s ignored; nothing was set",
                    message.instance_name,
                    extra={"instance": message.instance_name, "cause": message.cause},
                )
                return

            for rule in set_rules:
                record = self._latch(rule.name)
                updated = replace(
                    record, state=False, cleared_at=now, last_cause=message.instance_name
                )
                self._write_latch(updated)
                self._append_event(
                    at=now,
                    kind=EventKind.LATCH_CLEARED,
                    rule_name=rule.name,
                    instance_name=message.instance_name,
                    detail=message.cause,
                )
                self._log.info(
                    "rule %r cleared by %r: %s",
                    rule.name,
                    message.instance_name,
                    message.cause,
                    extra={
                        "rule": rule.name,
                        "instance": message.instance_name,
                        "state": False,
                        "cause": message.cause,
                    },
                )

            affected = self._graph.outputs_affected_by([rule.name for rule in rules])
            self._sync_outputs(affected, now=now)

    # -- push results ---------------------------------------------------------

    def _handle_push_result(self, message: PushResultMessage) -> None:
        now = self._clock.now()
        self._in_flight.discard(message.instance_name)

        with self._store.database.transaction():
            if message.succeeded:
                self._store.write_output_state(
                    OutputStateRecord(
                        instance_name=message.instance_name,
                        last_applied=message.pushed_value,
                        last_confirmed=message.pushed_value,
                        last_sync_at=now,
                    )
                )
                self._append_event(
                    at=now,
                    kind=EventKind.OUTPUT_PUSH_SUCCEEDED,
                    instance_name=message.instance_name,
                    detail=f"value={message.pushed_value}",
                )
                self._log.info(
                    "pushed %s to %s",
                    message.pushed_value,
                    message.instance_name,
                    extra={"instance": message.instance_name, "value": message.pushed_value},
                )
            else:
                self._record_push_failure(message=message, now=now)

            self._sync_outputs([message.instance_name], now=now)

    def _record_push_failure(self, *, message: PushResultMessage, now: datetime.datetime) -> None:
        existing = self._store.pending_push(message.instance_name)
        attempts = 1 if existing is None else existing.attempt_count + 1
        delay = self._backoff_seconds(attempts)
        self._store.write_pending_push(
            PendingPush(
                instance_name=message.instance_name,
                target_value=message.pushed_value,
                attempt_count=attempts,
                next_attempt_at=now + datetime.timedelta(seconds=delay),
                last_error=message.error,
            )
        )
        self._append_event(
            at=now,
            kind=EventKind.OUTPUT_PUSH_FAILED,
            instance_name=message.instance_name,
            detail=f"attempt={attempts} retry_in={delay:.0f}s {message.error or ''}".strip(),
        )
        self._log.warning(
            "push to %s failed (attempt %d), retrying in %.0fs: %s",
            message.instance_name,
            attempts,
            delay,
            message.error,
            extra={
                "instance": message.instance_name,
                "attempt": attempts,
                "retry_in_seconds": delay,
            },
        )

    def _backoff_seconds(self, attempts: int) -> float:
        initial = self._settings.retry_initial_seconds
        maximum = self._settings.retry_max_seconds
        delay: float = initial * float(2 ** max(0, attempts - 1))
        if delay > maximum:
            return maximum
        return delay

    # -- output synchronisation -----------------------------------------------

    def _sync_outputs(self, instance_names: Sequence[str], *, now: datetime.datetime) -> None:
        for instance_name in instance_names:
            self._sync_output(instance_name, now=now, force=False)

    def _sync_output(self, instance_name: str, *, now: datetime.datetime, force: bool) -> None:
        """Make the pending-push table agree with what this output should be.

        ``force`` is used by startup reconciliation, which pushes every output
        whether or not the daemon believes it is already correct. That is what
        step 5 -- "force every output into agreement" -- means, and it is why
        ``apply`` is required to be idempotent.
        """
        desired = self._graph.desired_output_state(
            instance_name=instance_name, latches=self.latch_states()
        )
        record = self._store.output_state(instance_name)
        last_applied = None if record is None else record.last_applied
        existing = self._store.pending_push(instance_name)

        if not force and last_applied == desired:
            if existing is not None:
                self._store.delete_pending_push(instance_name)
            return

        if not force and existing is not None and existing.target_value == desired:
            # A retry is already scheduled for this value. Leave its backoff
            # alone rather than resetting it on every unrelated state change.
            return

        self._store.write_pending_push(
            PendingPush(
                instance_name=instance_name,
                target_value=desired,
                attempt_count=0,
                next_attempt_at=now,
                last_error=None,
            )
        )

    def _dispatch_due_pushes(self, now: datetime.datetime) -> None:
        due = self._store.pending_pushes_due(now)
        if not due:
            return

        ready: list[tuple[str, OutputUpdate]] = []
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
                ready.append((push.instance_name, self._build_update(push.instance_name, desired)))

        for instance_name, update in ready:
            self._in_flight.add(instance_name)
            self._dispatcher.dispatch(instance_name=instance_name, update=update)

    def _build_update(self, instance_name: str, desired: bool) -> OutputUpdate:
        """Describe why an output is being driven, not just to what.

        The cause and detail come from whichever of the driving rules was set
        most recently, and the trigger count is summed across all of them, so
        an output shared by two latched rules reports both.
        """
        if not desired:
            return OutputUpdate(state=False)

        driving: list[LatchRecord] = []
        for rule in self._graph.rules_for_output(instance_name):
            record = self._latch(rule.name)
            if record.state:
                driving.append(record)

        if not driving:
            return OutputUpdate(state=desired)

        newest = driving[0]
        total = 0
        for record in driving:
            total += record.trigger_count
            if _is_newer(record.set_at, newest.set_at):
                newest = record

        return OutputUpdate(
            state=True,
            cause=newest.last_cause or "",
            detail=newest.last_detail or "",
            trigger_count=total,
            rules=tuple(sorted(record.rule_name for record in driving)),
            since=newest.set_at,
        )

    # -- reload ---------------------------------------------------------------

    def _handle_reload(self, message: ReloadMessage) -> None:
        now = self._clock.now()
        previous_outputs = set(self._graph.output_names())

        if message.plugins is not None:
            self._plugins = message.plugins
            self._dispatcher.set_outputs(message.plugins.outputs)

        self._graph = RuleGraph.from_configuration(message.configuration, logger=self._log)
        self.reload_latches()

        with self._store.database.transaction():
            self._append_event(
                at=now,
                kind=EventKind.CONFIG_LOADED,
                detail=f"{len(message.configuration.rules)} rules, "
                f"{len(message.configuration.instances)} instances",
            )
            affected = sorted(previous_outputs | set(self._graph.output_names()))
            self._sync_outputs(affected, now=now)

        self._log.info(
            "configuration reloaded: %d rules, %d instances",
            len(message.configuration.rules),
            len(message.configuration.instances),
        )
        message.acknowledged.set()

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

    # -- reconciliation -------------------------------------------------------

    def reconcile(self) -> None:
        """Bring persisted state, inputs, and outputs into agreement.

        Run once, before :meth:`run`, on the thread that will own the loop. An
        unreachable plugin does not block startup: it falls back to persisted
        state, logs a warning, and is retried in the background.
        """
        now = self._clock.now()
        since = self._store.last_seen_at()
        self._log.info(
            "reconciling; last seen %s",
            "never" if since is None else since.isoformat(),
        )

        downtime = self._collect_downtime_events(since=since, now=now)
        reports = self._collect_remote_states(now=now)

        with self._store.database.transaction():
            self._append_event(at=now, kind=EventKind.DAEMON_STARTED)
            for rule in self._graph.rules():
                latest_event, event_count = _summarize(downtime, rule.inputs)
                rule_reports: list[RemoteState] = []
                for output_name in rule.outputs:
                    report = reports.get(output_name)
                    if report is not None:
                        rule_reports.append(report)

                self._apply_reconcile_outcome(
                    rule_name=rule.name,
                    outcome=resolve_rule(
                        now=now,
                        persisted=self._latch(rule.name),
                        latest_downtime_event=latest_event,
                        output_reports=rule_reports,
                    ),
                    extra_triggers=event_count,
                    now=now,
                )

            # Step 5: force every output into agreement with the result.
            for output_name in self._graph.output_names():
                self._sync_output(output_name, now=now, force=True)

            self._store.write_last_seen_at(now)

        self._last_seen_written_at = now
        self._dispatch_due_pushes(now)
        self._ready.set()

    def _apply_reconcile_outcome(
        self,
        *,
        rule_name: str,
        outcome: ReconcileOutcome,
        extra_triggers: int,
        now: datetime.datetime,
    ) -> None:
        record = self._latch(rule_name)
        updated = replace(
            record,
            state=outcome.state,
            set_at=outcome.set_at,
            cleared_at=outcome.cleared_at,
            trigger_count=record.trigger_count + extra_triggers,
        )
        changed = updated != record
        if changed:
            self._write_latch(updated)

        self._append_event(
            at=now,
            kind=EventKind.RECONCILED,
            rule_name=rule_name,
            detail=f"state={outcome.state} reason={outcome.reason}",
        )
        if outcome.changed_from(record):
            self._log.info(
                "reconciled rule %r to %s: %s",
                rule_name,
                outcome.state,
                outcome.reason,
                extra={"rule": rule_name, "state": outcome.state, "reason": outcome.reason},
            )
        else:
            self._log.debug(
                "reconciled rule %r unchanged (%s): %s",
                rule_name,
                outcome.state,
                outcome.reason,
                extra={"rule": rule_name},
            )

    def _collect_downtime_events(
        self, *, since: datetime.datetime | None, now: datetime.datetime
    ) -> dict[str, list[ObservedEvent]]:
        del now
        collected: dict[str, list[ObservedEvent]] = {}
        for instance_name in self._graph.input_names():
            plugin = self._plugins.inputs.get(instance_name)
            if plugin is None:
                continue
            try:
                collected[instance_name] = plugin.catch_up(since)
            except BaseException as error:  # noqa: BLE001 - must not block startup
                self._log.warning(
                    "catch_up on %s failed, falling back to persisted state: %s",
                    instance_name,
                    error,
                    extra={"instance": instance_name},
                )
                self._schedule(
                    after_seconds=RECONCILE_RETRY_INITIAL_SECONDS,
                    message=CatchUpMessage(instance_name=instance_name, attempt=1, since=since),
                )
        return collected

    def _collect_remote_states(self, *, now: datetime.datetime) -> dict[str, RemoteState]:
        del now
        collected: dict[str, RemoteState] = {}
        for instance_name in self._graph.output_names():
            plugin = self._plugins.outputs.get(instance_name)
            if plugin is None:
                continue
            state = self._query_output(instance_name=instance_name, plugin=plugin)
            if state.belief is RemoteBelief.UNKNOWN:
                self._log.warning(
                    "output %s could not be reached during reconciliation; "
                    "falling back to persisted state and retrying in the background",
                    instance_name,
                    extra={"instance": instance_name},
                )
                self._schedule(
                    after_seconds=RECONCILE_RETRY_INITIAL_SECONDS,
                    message=ReconcileOutputMessage(instance_name=instance_name, attempt=1),
                )
                continue
            collected[instance_name] = state
        return collected

    def _query_output(self, *, instance_name: str, plugin: OutputPlugin) -> RemoteState:
        try:
            return plugin.query()
        except BaseException as error:  # noqa: BLE001 - must not block startup
            self._log.warning(
                "query() on %s raised: %s", error, instance_name, extra={"instance": instance_name}
            )
            return RemoteState(belief=RemoteBelief.UNKNOWN)

    def _handle_reconcile_retry(self, message: ReconcileOutputMessage) -> None:
        plugin = self._plugins.outputs.get(message.instance_name)
        if plugin is None:
            return

        state = self._query_output(instance_name=message.instance_name, plugin=plugin)
        if state.belief is RemoteBelief.UNKNOWN:
            delay = min(
                RECONCILE_RETRY_INITIAL_SECONDS * (2**message.attempt),
                RECONCILE_RETRY_MAX_SECONDS,
            )
            self._schedule(
                after_seconds=delay,
                message=ReconcileOutputMessage(
                    instance_name=message.instance_name, attempt=message.attempt + 1
                ),
            )
            return

        now = self._clock.now()
        self._log.info(
            "output %s answered on retry %d: %s",
            message.instance_name,
            message.attempt,
            state.belief.value,
            extra={"instance": message.instance_name},
        )

        with self._store.database.transaction():
            for rule in self._graph.rules_for_output(message.instance_name):
                self._apply_reconcile_outcome(
                    rule_name=rule.name,
                    outcome=resolve_rule(
                        now=now,
                        persisted=self._latch(rule.name),
                        latest_downtime_event=None,
                        output_reports=[state],
                    ),
                    extra_triggers=0,
                    now=now,
                )
            affected = self._graph.outputs_affected_by(
                [rule.name for rule in self._graph.rules_for_output(message.instance_name)]
            )
            self._sync_outputs(affected, now=now)

    def _handle_catch_up_retry(self, message: CatchUpMessage) -> None:
        plugin = self._plugins.inputs.get(message.instance_name)
        if plugin is None:
            return

        try:
            events = plugin.catch_up(message.since)
        except BaseException as error:  # noqa: BLE001 - retried, not fatal
            delay = min(
                RECONCILE_RETRY_INITIAL_SECONDS * (2**message.attempt),
                RECONCILE_RETRY_MAX_SECONDS,
            )
            self._log.warning(
                "catch_up on %s failed again (attempt %d), retrying in %.0fs: %s",
                message.instance_name,
                message.attempt,
                delay,
                error,
                extra={"instance": message.instance_name},
            )
            self._schedule(
                after_seconds=delay,
                message=replace(message, attempt=message.attempt + 1),
            )
            return

        for event in events:
            self._handle_input_event(
                InputEventMessage(instance_name=message.instance_name, event=event)
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


def _is_newer(candidate: datetime.datetime | None, current: datetime.datetime | None) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    return candidate > current


def _summarize(
    downtime: Mapping[str, Sequence[ObservedEvent]], instance_names: Sequence[str]
) -> tuple[datetime.datetime | None, int]:
    """Return the latest downtime event timestamp across these inputs, and a count."""
    latest: datetime.datetime | None = None
    count = 0
    for instance_name in instance_names:
        for event in downtime.get(instance_name, []):
            count += 1
            if latest is None or event.occurred_at > latest:
                latest = event.occurred_at
    return (latest, count)
