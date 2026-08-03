import datetime
from collections.abc import Iterator
from pathlib import Path

import pytest

from file_input import FileInput
from noti_mapper.clock import ManualClock
from noti_mapper.plugin import ObservedEvent
from noti_mapper.storage import Database, database_path, initialize
from tests.support import make_context

START = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)


class Harness:
    def __init__(self, plugin: FileInput, events: list[ObservedEvent], clock: ManualClock) -> None:
        self.plugin = plugin
        self.events = events
        self.clock = clock

    def poll(self, *, advance: float = 1.0) -> None:
        self.clock.advance(advance)
        self.plugin.poll_once()

    def paths(self) -> list[str]:
        return [event.metadata["path"] for event in self.events]


def _harness(tmp_path: Path, database: Database, **settings: object) -> Harness:
    events: list[ObservedEvent] = []
    clock = ManualClock(start=START)
    merged: dict[str, object] = {"path": str(tmp_path), "poll_seconds": 1, "debounce_seconds": 2}
    merged.update(settings)
    plugin = FileInput(
        context=make_context(
            instance_name="Watcher", database=database, clock=clock, settings=merged
        ),
        emit=events.append,
    )
    return Harness(plugin, events, clock)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    opened = Database(path=database_path(tmp_path / "state"))
    initialize(opened)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def watched(tmp_path: Path) -> Path:
    directory = tmp_path / "watched"
    directory.mkdir()
    return directory


# -- debounce -----------------------------------------------------------------


def test_a_new_file_fires_only_after_the_quiet_period(watched: Path, database: Database) -> None:
    harness = _harness(watched, database, glob="*.txt")

    (watched / "package.txt").write_text("one", encoding="utf-8")
    harness.poll()
    assert harness.events == []

    harness.poll()
    assert harness.events == []

    harness.poll()
    assert len(harness.events) == 1
    assert harness.paths() == [str(watched / "package.txt")]


def test_a_write_burst_produces_one_event(watched: Path, database: Database) -> None:
    harness = _harness(watched, database, glob="*.txt")
    target = watched / "package.txt"

    for index in range(5):
        target.write_text("x" * (index + 1), encoding="utf-8")
        harness.poll()
    assert harness.events == [], "an editor's write burst must not fire mid-write"

    harness.poll()
    harness.poll()
    harness.poll()
    assert len(harness.events) == 1


def test_a_file_that_disappears_mid_debounce_fires_nothing(
    watched: Path, database: Database
) -> None:
    harness = _harness(watched, database, glob="*.txt")
    target = watched / "package.txt"
    target.write_text("one", encoding="utf-8")
    harness.poll()
    target.unlink()

    harness.poll()
    harness.poll()
    assert harness.events == []


# -- settings -----------------------------------------------------------------


def _validate(**overrides: object) -> list[str]:
    settings: dict[str, object] = {"path": "/var/spool/notify"}
    settings.update(overrides)
    return FileInput.validate_settings(settings)


def test_a_minimal_configuration_validates() -> None:
    assert _validate() == []


def test_the_path_is_required_and_must_be_absolute() -> None:
    assert FileInput.validate_settings({}) == ['"path" is required']
    assert any("must be absolute" in problem for problem in _validate(path="relative/path"))


def test_numeric_settings_must_be_positive() -> None:
    assert any("positive number" in problem for problem in _validate(poll_seconds=0))
    assert any("positive number" in problem for problem in _validate(debounce_seconds=-1))
    assert any("positive number" in problem for problem in _validate(poll_seconds=True))
    assert _validate(poll_seconds=0.5, debounce_seconds=1.5) == []


def test_unknown_settings_are_reported() -> None:
    assert _validate(pattern="*.txt") == ['unknown setting "pattern"']


def test_emit_on_modify_must_be_a_boolean() -> None:
    assert any("true or false" in problem for problem in _validate(emit_on_modify="yes"))
