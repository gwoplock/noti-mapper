"""End-to-end: real plugin directories, real threads, real SQLite.

The engine tests drive the core deterministically with fakes. These exist to
prove the wiring works when nothing is faked: discovery imports plugin
directories from disk, an input thread emits, the dispatcher pool pushes, and
shutdown returns.
"""

import datetime
import json
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from noti_mapper.clock import SystemClock
from noti_mapper.runtime import Daemon, Paths, StartupError
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


def test_state_survives_a_restart(workspace: dict[str, Path]) -> None:
    _default_config(workspace)

    with _daemon(workspace):
        (workspace["signals"] / "trigger").write_text("first", encoding="utf-8")
        _eventually(lambda: _lamp(workspace) == "true", what="the first latch")

    (workspace["signals"] / "lamp").unlink()

    with _daemon(workspace):
        # Reconciliation forces the output back into agreement with the latch
        # that survived the restart.
        _eventually(lambda: _lamp(workspace) == "true", what="the latch to survive")


def test_a_downtime_event_after_a_remote_clear_re_latches(workspace: dict[str, Path]) -> None:
    _default_config(workspace)
    signals = workspace["signals"]

    with _daemon(workspace):
        signals.joinpath("trigger").write_text("first", encoding="utf-8")
        _eventually(lambda: _lamp(workspace) == "true", what="the first latch")

    clear_time = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=2)
    event_time = clear_time + datetime.timedelta(hours=1)
    signals.joinpath("belief").write_text(f"cleared:{clear_time.isoformat()}", encoding="utf-8")
    signals.joinpath("catch-up").write_text(event_time.isoformat(), encoding="utf-8")
    signals.joinpath("lamp").unlink()

    with _daemon(workspace):
        _eventually(lambda: _lamp(workspace) == "true", what="the later event to win")

    for store in _store(workspace):
        latch = store.latch("Package On Porch")
        assert latch is not None
        assert latch.state is True
        assert latch.set_at == event_time


def test_a_remote_clear_with_no_later_event_clears(workspace: dict[str, Path]) -> None:
    _default_config(workspace)
    signals = workspace["signals"]

    with _daemon(workspace):
        signals.joinpath("trigger").write_text("first", encoding="utf-8")
        _eventually(lambda: _lamp(workspace) == "true", what="the first latch")

    signals.joinpath("belief").write_text("cleared", encoding="utf-8")

    with _daemon(workspace):
        _eventually(lambda: _lamp(workspace) == "false", what="the remote clear to win")


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


# -- startup failures ---------------------------------------------------------


def test_an_invalid_configuration_refuses_to_start(workspace: dict[str, Path]) -> None:
    _write_config(workspace, {"instances": {"A": {"plugin": "does-not-exist"}}})
    daemon = Daemon(paths=_paths(workspace), notifier=Notifier(address=""), handle_signals=False)
    with pytest.raises(StartupError, match="unknown plugin"):
        daemon.start()
    daemon.stop()


def test_a_missing_required_setting_is_a_startup_error(workspace: dict[str, Path]) -> None:
    _write_config(
        workspace,
        {
            "instances": {"Porch Mail": {"plugin": "probe-input", "config": {}}},
            "rules": {},
        },
    )
    daemon = Daemon(paths=_paths(workspace), notifier=Notifier(address=""), handle_signals=False)
    with pytest.raises(StartupError, match="trigger_file"):
        daemon.start()
    daemon.stop()


def test_world_readable_secrets_refuse_to_start(workspace: dict[str, Path]) -> None:
    _default_config(workspace)
    secrets = workspace["root"] / "secrets.json"
    secrets.write_text(json.dumps({"pw": "hunter2"}), encoding="utf-8")
    secrets.chmod(0o644)

    daemon = Daemon(paths=_paths(workspace), notifier=Notifier(address=""), handle_signals=False)
    with pytest.raises(StartupError, match="chmod 0600"):
        daemon.start()
    daemon.stop()


# -- reload -------------------------------------------------------------------


def test_reload_adopts_a_new_rule(workspace: dict[str, Path]) -> None:
    _default_config(workspace)
    signals = workspace["signals"]

    with _daemon(workspace) as daemon:
        _eventually(lambda: _lamp(workspace) == "false", what="the initial push")

        _write_config(
            workspace,
            {
                "instances": {
                    "Porch Mail": {
                        "plugin": "probe-input",
                        "config": {"trigger_file": str(signals / "trigger")},
                    },
                    "Porch Lamp": {
                        "plugin": "probe-output",
                        "config": {
                            "state_file": str(signals / "lamp"),
                            "unlatch_file": str(signals / "unlatch"),
                        },
                    },
                    "Hall Lamp": {
                        "plugin": "probe-output",
                        "config": {"state_file": str(signals / "hall")},
                    },
                },
                "rules": {
                    "Package On Porch": {
                        "inputs": ["Porch Mail"],
                        "outputs": ["Porch Lamp", "Hall Lamp"],
                    }
                },
            },
        )
        daemon._reload()  # noqa: SLF001 - the SIGHUP path without the signal

        _eventually(lambda: (signals / "hall").exists(), what="the new output to be pushed")
        signals.joinpath("trigger").write_text("after reload", encoding="utf-8")
        _eventually(
            lambda: (signals / "hall").read_text(encoding="utf-8") == "true",
            what="the new output to follow the latch",
        )


def test_reload_with_an_invalid_configuration_keeps_running(
    workspace: dict[str, Path],
) -> None:
    _default_config(workspace)

    with _daemon(workspace) as daemon:
        _eventually(lambda: _lamp(workspace) == "false", what="the initial push")

        _write_config(workspace, {"instances": {"Broken": {"plugin": "nope"}}}, name="20-bad.json")
        daemon._reload()  # noqa: SLF001

        workspace["signals"].joinpath("trigger").write_text("still alive", encoding="utf-8")
        _eventually(lambda: _lamp(workspace) == "true", what="the daemon to keep working")


def test_removing_a_rule_on_reload_orphans_it_and_drops_the_output(
    workspace: dict[str, Path],
) -> None:
    _default_config(workspace)
    signals = workspace["signals"]

    with _daemon(workspace) as daemon:
        signals.joinpath("trigger").write_text("latch me", encoding="utf-8")
        _eventually(lambda: _lamp(workspace) == "true", what="the latch")

        _write_config(
            workspace,
            {
                "instances": {
                    "Porch Mail": {
                        "plugin": "probe-input",
                        "config": {"trigger_file": str(signals / "trigger")},
                    },
                    "Porch Lamp": {
                        "plugin": "probe-output",
                        "config": {"state_file": str(signals / "lamp")},
                    },
                },
                "rules": {},
            },
        )
        daemon._reload()  # noqa: SLF001

        _eventually(lambda: _lamp(workspace) == "false", what="the orphaned rule to drop")

    for store in _store(workspace):
        latch = store.latch("Package On Porch")
        assert latch is not None
        assert latch.state is True, "the orphaned latch is retained, not deleted"
        assert [rule.orphaned for rule in store.rules()] == [True]


# -- the watchdog -------------------------------------------------------------


def test_the_watchdog_reports_a_stale_core_loop(workspace: dict[str, Path]) -> None:
    _default_config(workspace)
    daemon = Daemon(paths=_paths(workspace), notifier=Notifier(address=""), handle_signals=False)

    now = datetime.datetime.now(tz=datetime.UTC)
    assert daemon._unhealthy_reason(now) == "the core loop has not started"  # noqa: SLF001

    with _daemon(workspace) as running:
        _eventually(lambda: _lamp(workspace) == "false", what="the initial push")
        assert (
            running._unhealthy_reason(datetime.datetime.now(tz=datetime.UTC)) is None
        )  # noqa: SLF001

        far_future = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=1)
        reason = running._unhealthy_reason(far_future)  # noqa: SLF001
        assert reason is not None
        assert "has not ticked" in reason
