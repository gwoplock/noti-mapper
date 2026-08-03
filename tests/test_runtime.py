"""End-to-end: real plugin directories, real threads, real SQLite.

The engine tests drive the core deterministically with fakes. These exist to
prove the wiring works when nothing is faked: discovery imports plugin
directories from disk, an input thread emits, the dispatcher pool pushes, and
shutdown returns.
"""

import json
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from noti_mapper.clock import SystemClock
from noti_mapper.runtime import Daemon, Paths
from noti_mapper.sdnotify import Notifier
from noti_mapper.storage import Database, HealthStatus, Store, database_path
from tests.probe_plugins import write_probe_plugins

TIMEOUT_SECONDS = 10.0


@pytest.fixture(autouse=True)
def clean_module_table() -> Iterator[None]:
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("noti_mapper_plugin_"):
            del sys.modules[name]


def _eventually(predicate: Callable[[], bool], *, what: str) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    plugins = write_probe_plugins(tmp_path / "plugins")
    config = tmp_path / "conf.d"
    config.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    signals = tmp_path / "signals"
    signals.mkdir()
    return {
        "plugins": plugins,
        "config": config,
        "state": state,
        "signals": signals,
        "root": tmp_path,
    }


def _write_config(workspace: dict[str, Path], document: object, name: str = "10-all.json") -> None:
    (workspace["config"] / name).write_text(json.dumps(document, indent=2), encoding="utf-8")


def _default_config(workspace: dict[str, Path]) -> None:
    signals = workspace["signals"]
    _write_config(
        workspace,
        {
            "instances": {
                "Porch Mail": {
                    "plugin": "probe-input",
                    "config": {
                        "trigger_file": str(signals / "trigger"),
                        "catch_up_file": str(signals / "catch-up"),
                    },
                },
                "Porch Lamp": {
                    "plugin": "probe-output",
                    "config": {
                        "state_file": str(signals / "lamp"),
                        "unlatch_file": str(signals / "unlatch"),
                        "belief_file": str(signals / "belief"),
                    },
                },
            },
            "rules": {"Package On Porch": {"inputs": ["Porch Mail"], "outputs": ["Porch Lamp"]}},
        },
    )


def _paths(workspace: dict[str, Path]) -> Paths:
    return Paths(
        config_directory=workspace["config"],
        secrets_path=workspace["root"] / "secrets.json",
        state_directory=workspace["state"],
        plugin_directories=(workspace["plugins"],),
    )


class _RunningDaemon:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self._thread = threading.Thread(target=daemon.run, name="test-core", daemon=True)

    def __enter__(self) -> Daemon:
        self.daemon.start()
        self._thread.start()
        return self.daemon

    def __exit__(self, *exception: object) -> None:
        self.daemon.stop()
        self._thread.join(timeout=TIMEOUT_SECONDS)


def _daemon(workspace: dict[str, Path], *, notifier: Notifier | None = None) -> _RunningDaemon:
    return _RunningDaemon(
        Daemon(
            paths=_paths(workspace),
            clock=SystemClock(),
            notifier=notifier if notifier is not None else Notifier(address=""),
            handle_signals=False,
        )
    )


def _lamp(workspace: dict[str, Path]) -> str | None:
    path = workspace["signals"] / "lamp"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _store(workspace: dict[str, Path]) -> Iterator[Store]:
    database = Database(path=database_path(workspace["state"]))
    try:
        yield Store(database=database)
    finally:
        database.close()


# -- the whole loop -----------------------------------------------------------


def test_an_event_latches_and_drives_a_real_output(workspace: dict[str, Path]) -> None:
    _default_config(workspace)

    with _daemon(workspace):
        _eventually(lambda: _lamp(workspace) == "false", what="the initial forced push")

        (workspace["signals"] / "trigger").write_text("Delivered: box", encoding="utf-8")
        _eventually(lambda: _lamp(workspace) == "true", what="the latch to drive the lamp")

        (workspace["signals"] / "unlatch").write_text("", encoding="utf-8")
        _eventually(lambda: _lamp(workspace) == "false", what="the unlatch to clear the lamp")

    for store in _store(workspace):
        latch = store.latch("Package On Porch")
        assert latch is not None
        assert latch.state is False
        assert latch.trigger_count == 1


def test_health_is_recorded_for_every_instance(workspace: dict[str, Path]) -> None:
    _default_config(workspace)

    with _daemon(workspace) as daemon:
        del daemon
        for store in _store(workspace):

            def has_both(store: Store = store) -> bool:
                names = {record.instance_name for record in store.health()}
                return {"Porch Mail", "Porch Lamp"} <= names

            _eventually(has_both, what="health reports from both instances")
            statuses = {record.instance_name: record.status for record in store.health()}
            assert statuses["Porch Mail"] is HealthStatus.OK


def test_readiness_is_signalled_only_after_reconciliation(workspace: dict[str, Path]) -> None:
    _default_config(workspace)
    notified: list[str] = []

    class RecordingNotifier(Notifier):
        def ready(self, status: str = "") -> None:
            notified.append(f"READY {status}")

        def status(self, text: str) -> None:
            notified.append(f"STATUS {text}")

        def stopping(self) -> None:
            notified.append("STOPPING")

    with _daemon(workspace, notifier=RecordingNotifier(address="")):
        _eventually(lambda: any(line.startswith("READY") for line in notified), what="readiness")

    assert notified[0].startswith("STATUS reconciling")
    assert notified[1].startswith("READY")
    assert "1 rules" in notified[1]
