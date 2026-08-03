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
