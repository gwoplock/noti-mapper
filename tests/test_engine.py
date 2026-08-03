import datetime
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from noti_mapper.clock import ManualClock
from noti_mapper.config import (
    DaemonSettings,
)
from noti_mapper.engine import Engine, PluginSet
from noti_mapper.messages import (
    InputEventMessage,
    PushResultMessage,
    UnlatchRequestMessage,
)
from noti_mapper.plugin import ObservedEvent
from noti_mapper.rules import Rule, RuleGraph
from noti_mapper.storage import (
    Database,
    EventKind,
    HealthStatus,
    InstanceRecord,
    RuleRecord,
    Store,
    database_path,
    initialize,
)
from tests.support import (
    FakeInput,
    FakeOutput,
    FakeOutputBehaviour,
    InlineDispatcher,
    make_context,
)

START = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)


@dataclass
class _Deferred:
    """Breaks the construction cycle between the engine and the things it calls."""

    engine: Engine | None = None

    def report(self, message: PushResultMessage) -> None:
        assert self.engine is not None
        self.engine.submit(message)


@dataclass
class Harness:
    store: Store
    engine: Engine
    clock: ManualClock
    dispatcher: InlineDispatcher
    inputs: dict[str, FakeInput] = field(default_factory=dict)
    outputs: dict[str, FakeOutput] = field(default_factory=dict)

    def fire(
        self,
        instance_name: str,
        *,
        at: datetime.datetime | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.engine.submit(
            InputEventMessage(
                instance_name=instance_name,
                event=ObservedEvent(
                    occurred_at=at if at is not None else self.clock.now(),
                    metadata={} if metadata is None else metadata,
                ),
            )
        )
        self.engine.drain()

    def unlatch(self, instance_name: str, cause: str = "operator") -> None:
        self.engine.submit(UnlatchRequestMessage(instance_name=instance_name, cause=cause))
        self.engine.drain()

    def latch(self, rule_name: str) -> bool:
        record = self.store.latch(rule_name)
        assert record is not None
        return record.state

    def applied(self, instance_name: str) -> list[bool]:
        return list(self.outputs[instance_name].applied)

    def event_kinds(self) -> list[EventKind]:
        return [entry.kind for entry in self.store.recent_events(limit=200)]


def _build(
    tmp_path: Path,
    *,
    rules: Mapping[str, tuple[Sequence[str], Sequence[str]]],
    clock: ManualClock | None = None,
    settings: DaemonSettings | None = None,
) -> Iterator[Harness]:
    database = Database(path=database_path(tmp_path))
    initialize(database)
    store = Store(database=database)

    input_names: set[str] = set()
    output_names: set[str] = set()
    for inputs, outputs in rules.values():
        input_names.update(inputs)
        output_names.update(outputs)

    store.sync_instances(
        [
            InstanceRecord(name=name, plugin="fake-input", enabled=True, orphaned=False)
            for name in sorted(input_names)
        ]
        + [
            InstanceRecord(name=name, plugin="fake-output", enabled=True, orphaned=False)
            for name in sorted(output_names)
        ]
    )
    store.sync_rules(
        [
            RuleRecord(
                name=name,
                enabled=True,
                orphaned=False,
                inputs=tuple(inputs),
                outputs=tuple(outputs),
            )
            for name, (inputs, outputs) in rules.items()
        ]
    )

    graph = RuleGraph(
        [
            Rule(name=name, inputs=tuple(inputs), outputs=tuple(outputs))
            for name, (inputs, outputs) in rules.items()
        ]
    )

    the_clock = clock if clock is not None else ManualClock(start=START)
    deferred = _Deferred()
    dispatcher = InlineDispatcher(outputs={}, report=deferred.report)
    plugins = PluginSet.empty()

    engine = Engine(
        store=store,
        graph=graph,
        plugins=plugins,
        dispatcher=dispatcher,
        clock=the_clock,
        settings=settings if settings is not None else DaemonSettings(),
        logger=logging.getLogger("test.engine"),
    )
    deferred.engine = engine

    harness = Harness(store=store, engine=engine, clock=the_clock, dispatcher=dispatcher)

    for name in sorted(input_names):
        plugin = FakeInput(
            context=make_context(instance_name=name, database=database, clock=the_clock),
            emit=engine.emit_callback(name),
        )
        plugins.inputs[name] = plugin
        harness.inputs[name] = plugin

    for name in sorted(output_names):
        output = FakeOutput(
            context=make_context(instance_name=name, database=database, clock=the_clock),
            request_unlatch=engine.unlatch_callback(name),
        )
        plugins.outputs[name] = output
        harness.outputs[name] = output

    dispatcher.set_outputs(plugins.outputs)

    try:
        yield harness
    finally:
        database.close()


@pytest.fixture
def simple(tmp_path: Path) -> Iterator[Harness]:
    yield from _build(tmp_path, rules={"R": (["Mail"], ["Lamp"])})


# -- the basic loop -----------------------------------------------------------


def test_an_event_latches_the_rule_and_drives_the_output(simple: Harness) -> None:
    assert simple.latch("R") is False

    simple.fire("Mail", metadata={"subject": "Delivered: box"})

    assert simple.latch("R") is True
    assert simple.applied("Lamp") == [True]

    record = simple.store.latch("R")
    assert record is not None
    assert record.trigger_count == 1
    assert record.last_cause == "Mail"
    assert record.set_at == START


def test_the_event_log_records_the_transition_and_its_cause(simple: Harness) -> None:
    simple.fire("Mail", metadata={"sender": "ups.com"})
    kinds = simple.event_kinds()
    assert EventKind.INPUT_EVENT in kinds
    assert EventKind.LATCH_SET in kinds
    assert EventKind.OUTPUT_PUSH_SUCCEEDED in kinds

    latch_entry = next(
        entry for entry in simple.store.recent_events(200) if entry.kind is EventKind.LATCH_SET
    )
    assert latch_entry.rule_name == "R"
    assert latch_entry.instance_name == "Mail"
    assert "ups.com" in latch_entry.detail


def test_an_event_matching_no_rule_is_logged_and_dropped(simple: Harness) -> None:
    simple.engine.submit(
        InputEventMessage(
            instance_name="Unwired", event=ObservedEvent(occurred_at=simple.clock.now())
        )
    )
    simple.engine.drain()
    assert simple.latch("R") is False
    assert EventKind.INPUT_EVENT in simple.event_kinds()


# -- re-triggering ------------------------------------------------------------


def test_retriggering_bumps_the_counter_without_a_second_transition(simple: Harness) -> None:
    simple.fire("Mail")
    simple.clock.advance(30)
    simple.fire("Mail")
    simple.clock.advance(30)
    simple.fire("Mail")

    record = simple.store.latch("R")
    assert record is not None
    assert record.state is True
    assert record.trigger_count == 3
    assert record.set_at == START + datetime.timedelta(seconds=60)

    assert simple.applied("Lamp") == [True]
    assert simple.event_kinds().count(EventKind.LATCH_SET) == 1
    assert simple.event_kinds().count(EventKind.LATCH_RETRIGGERED) == 2


# -- rule semantics -----------------------------------------------------------


def test_multiple_inputs_are_ord(tmp_path: Path) -> None:
    for harness in _build(tmp_path, rules={"R": (["Mail", "Hook"], ["Lamp"])}):
        harness.fire("Hook")
        assert harness.latch("R") is True
        assert harness.applied("Lamp") == [True]

        harness.fire("Mail")
        assert harness.applied("Lamp") == [True]


# -- pushes cannot veto state -------------------------------------------------


def test_a_failing_push_does_not_prevent_the_latch(tmp_path: Path) -> None:
    for harness in _build(tmp_path, rules={"R": (["Mail"], ["Lamp", "Pager"])}):
        harness.outputs["Pager"].configure(FakeOutputBehaviour(apply_always_fails=True))

        harness.fire("Mail")

        assert harness.latch("R") is True
        assert harness.applied("Lamp") == [True]
        assert harness.applied("Pager") == []

        pending = harness.store.pending_push("Pager")
        assert pending is not None
        assert pending.target_value is True
        assert pending.attempt_count == 1
        assert pending.last_error is not None
        assert EventKind.OUTPUT_PUSH_FAILED in harness.event_kinds()


def test_a_failed_push_retries_with_exponential_backoff(tmp_path: Path) -> None:
    settings = DaemonSettings(retry_initial_seconds=5.0, retry_max_seconds=20.0)
    for harness in _build(tmp_path, rules={"R": (["Mail"], ["Lamp"])}, settings=settings):
        harness.outputs["Lamp"].configure(FakeOutputBehaviour(apply_always_fails=True))
        harness.fire("Mail")

        delays: list[float] = []
        for _ in range(5):
            pending = harness.store.pending_push("Lamp")
            assert pending is not None
            delays.append((pending.next_attempt_at - harness.clock.now()).total_seconds())
            harness.clock.advance(delays[-1])
            harness.engine.drain()

        assert delays == [5.0, 10.0, 20.0, 20.0, 20.0]


def test_a_retry_eventually_succeeds_and_clears_the_pending_row(tmp_path: Path) -> None:
    for harness in _build(tmp_path, rules={"R": (["Mail"], ["Lamp"])}):
        harness.outputs["Lamp"].configure(FakeOutputBehaviour(apply_failures=2))
        harness.fire("Mail")

        for _ in range(3):
            harness.clock.advance(3600)
            harness.engine.drain()

        assert harness.applied("Lamp") == [True]
        assert harness.store.pending_push("Lamp") is None

        state = harness.store.output_state("Lamp")
        assert state is not None
        assert state.last_applied is True


def test_a_pending_push_survives_a_restart(tmp_path: Path) -> None:
    for harness in _build(tmp_path, rules={"R": (["Mail"], ["Lamp"])}):
        harness.outputs["Lamp"].configure(FakeOutputBehaviour(apply_always_fails=True))
        harness.fire("Mail")
        assert harness.store.pending_push("Lamp") is not None

    for restarted in _build(tmp_path, rules={"R": (["Mail"], ["Lamp"])}):
        assert restarted.latch("R") is True
        pending = restarted.store.pending_push("Lamp")
        assert pending is not None
        assert pending.target_value is True

        restarted.clock.advance(3600)
        restarted.engine.drain()
        assert restarted.applied("Lamp") == [True]


def test_a_push_to_a_vanished_instance_is_dropped(simple: Harness) -> None:
    simple.outputs["Lamp"].configure(FakeOutputBehaviour(apply_always_fails=True))
    simple.fire("Mail")
    assert simple.store.pending_push("Lamp") is not None

    del simple.engine._plugins.outputs["Lamp"]  # noqa: SLF001 - simulating a reload
    simple.clock.advance(3600)
    simple.engine.drain()

    assert simple.store.pending_push("Lamp") is None


# -- health -------------------------------------------------------------------


def test_health_is_polled_and_recorded(simple: Harness) -> None:
    simple.clock.advance(31)
    simple.engine.drain()

    records = {record.instance_name: record for record in simple.store.health()}
    assert records["Mail"].status is HealthStatus.OK
    assert records["Lamp"].status is HealthStatus.OK


def test_a_plugin_raising_from_health_is_recorded_as_failed(simple: Harness) -> None:
    class Exploding(FakeOutput):
        def health(self) -> object:  # type: ignore[override]
            raise RuntimeError("kaboom")

    exploding = Exploding(
        context=simple.outputs["Lamp"].context,
        request_unlatch=simple.engine.unlatch_callback("Lamp"),
    )
    simple.engine._plugins.outputs["Lamp"] = exploding  # noqa: SLF001

    simple.clock.advance(31)
    simple.engine.drain()

    records = {record.instance_name: record for record in simple.store.health()}
    assert records["Lamp"].status is HealthStatus.FAILED
    assert "kaboom" in records["Lamp"].detail
