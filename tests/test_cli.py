import datetime
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from noti_mapper import VERSION
from noti_mapper.cli import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK, main
from noti_mapper.storage import (
    Database,
    InstanceRecord,
    LatchRecord,
    PluginKeyValueStore,
    RuleRecord,
    Store,
    database_path,
    initialize,
)
from tests.probe_plugins import write_probe_plugins

MOMENT = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def clean_module_table() -> Iterator[None]:
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("noti_mapper_plugin_"):
            del sys.modules[name]


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    plugins = write_probe_plugins(tmp_path / "plugins")
    config = tmp_path / "conf.d"
    config.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    return {"plugins": plugins, "config": config, "state": state, "root": tmp_path}


def _argv(workspace: dict[str, Path], *arguments: str) -> list[str]:
    return [
        "--config-dir",
        str(workspace["config"]),
        "--secrets",
        str(workspace["root"] / "secrets.json"),
        "--state-dir",
        str(workspace["state"]),
        "--plugin-dir",
        str(workspace["plugins"]),
        *arguments,
    ]


def _write_config(workspace: dict[str, Path], document: object, name: str = "10-all.json") -> None:
    (workspace["config"] / name).write_text(json.dumps(document, indent=2), encoding="utf-8")


def _valid_config(workspace: dict[str, Path]) -> None:
    _write_config(
        workspace,
        {
            "instances": {
                "Porch Mail": {
                    "plugin": "probe-input",
                    "config": {"trigger_file": str(workspace["root"] / "trigger")},
                },
                "Porch Lamp": {
                    "plugin": "probe-output",
                    "config": {"state_file": str(workspace["root"] / "lamp")},
                },
            },
            "rules": {"Package On Porch": {"inputs": ["Porch Mail"], "outputs": ["Porch Lamp"]}},
        },
    )


def _seed_state(workspace: dict[str, Path], *, latched: bool = True) -> None:
    database = Database(path=database_path(workspace["state"]))
    try:
        initialize(database)
        store = Store(database=database)
        store.sync_instances(
            [
                InstanceRecord(
                    name="Porch Mail", plugin="probe-input", enabled=True, orphaned=False
                ),
                InstanceRecord(
                    name="Porch Lamp", plugin="probe-output", enabled=True, orphaned=False
                ),
            ]
        )
        store.sync_rules(
            [
                RuleRecord(
                    name="Package On Porch",
                    enabled=True,
                    orphaned=False,
                    inputs=("Porch Mail",),
                    outputs=("Porch Lamp",),
                )
            ]
        )
        store.write_latch(
            LatchRecord(
                rule_name="Package On Porch",
                state=latched,
                set_at=MOMENT,
                cleared_at=None,
                trigger_count=3,
                last_cause="Porch Mail",
            )
        )
    finally:
        database.close()


# -- version ------------------------------------------------------------------


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == EXIT_OK
    assert VERSION in capsys.readouterr().out


def test_an_unknown_log_level_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--log-level", "chatty", "version"]) == EXIT_FAILURE
    assert "unknown log level" in capsys.readouterr().err


# -- validate -----------------------------------------------------------------


def test_validate_accepts_a_good_configuration(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _valid_config(workspace)
    assert main(_argv(workspace, "validate")) == EXIT_OK

    output = capsys.readouterr().out
    assert "configuration is valid" in output
    assert "2 instances" in output
    assert "1 rules" in output


def test_validate_reports_every_error_with_a_location(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(
        workspace,
        {
            "instances": {"Porch Mail": {"plugin": "no-such-plugin"}},
            "rules": {"R": {"inputs": ["Ghost"], "outputs": ["Ghost"]}},
        },
    )
    assert main(_argv(workspace, "validate")) == EXIT_CONFIG_ERROR

    errors = capsys.readouterr().err
    assert "unknown plugin" in errors
    assert "10-all.json:" in errors
    assert "configuration errors" in errors


def test_validate_reports_a_missing_required_setting(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(workspace, {"instances": {"Porch Mail": {"plugin": "probe-input"}}})
    assert main(_argv(workspace, "validate")) == EXIT_CONFIG_ERROR
    assert "trigger_file" in capsys.readouterr().err


def test_validate_refuses_readable_secrets(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _valid_config(workspace)
    secrets = workspace["root"] / "secrets.json"
    secrets.write_text(json.dumps({"pw": "x"}), encoding="utf-8")
    secrets.chmod(0o644)

    assert main(_argv(workspace, "validate")) == EXIT_CONFIG_ERROR
    assert "chmod 0600" in capsys.readouterr().err


def test_validate_warns_about_broken_plugins_without_failing(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    broken = workspace["plugins"] / "broken"
    broken.mkdir()
    (broken / "__init__.py").write_text("raise RuntimeError('nope')\n", encoding="utf-8")
    _valid_config(workspace)

    assert main(_argv(workspace, "validate")) == EXIT_OK
    captured = capsys.readouterr()
    assert "warning: plugin at" in captured.err
    assert "configuration is valid" in captured.out


# -- status -------------------------------------------------------------------


def test_status_without_a_database_says_so(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(workspace, "status")) == EXIT_FAILURE
    assert "has the daemon ever run" in capsys.readouterr().out


def test_status_prints_latches_outputs_and_events(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _valid_config(workspace)
    _seed_state(workspace)

    assert main(_argv(workspace, "status")) == EXIT_OK
    output = capsys.readouterr().out

    assert "Latches" in output
    assert "'Package On Porch'" in output
    assert "SET" in output
    assert "triggers=3" in output
    assert "cause=Porch Mail" in output

    assert "Outputs" in output
    assert "desired=True" in output
    assert "Pending retries" in output
    assert "Plugin health" in output


def test_status_flags_an_output_that_is_out_of_sync(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _valid_config(workspace)
    _seed_state(workspace)

    assert main(_argv(workspace, "status")) == EXIT_OK
    assert "out of sync" in capsys.readouterr().out


def test_status_lists_orphans(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _valid_config(workspace)
    _seed_state(workspace)

    database = Database(path=database_path(workspace["state"]))
    try:
        initialize(database)
        Store(database=database).sync_rules([])
    finally:
        database.close()

    assert main(_argv(workspace, "status")) == EXIT_OK
    output = capsys.readouterr().out
    assert "Orphaned rules" in output
    assert "noti-mapper purge" in output


def test_status_still_works_when_the_configuration_is_broken(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(workspace, {"instances": {"Porch Mail": {"plugin": "gone"}}})
    _seed_state(workspace)

    assert main(_argv(workspace, "status")) == EXIT_OK
    output = capsys.readouterr().out
    assert "does not currently load" in output
    assert "'Package On Porch'" in output


# -- rename -------------------------------------------------------------------


def test_rename_migrates_the_latch(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _valid_config(workspace)
    _seed_state(workspace)

    assert main(_argv(workspace, "rename", "Package On Porch", "Porch Package")) == EXIT_OK
    assert "its latch moved with it" in capsys.readouterr().out

    database = Database(path=database_path(workspace["state"]))
    try:
        store = Store(database=database)
        assert store.latch("Package On Porch") is None
        moved = store.latch("Porch Package")
        assert moved is not None
        assert moved.state is True
        assert moved.trigger_count == 3
    finally:
        database.close()


def test_rename_rejects_an_invalid_new_name(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_state(workspace)
    assert main(_argv(workspace, "rename", "Package On Porch", "Porch/Package")) == EXIT_FAILURE
    assert "disallowed characters" in capsys.readouterr().err


def test_rename_rejects_an_unknown_rule(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_state(workspace)
    assert main(_argv(workspace, "rename", "Nope", "Also Nope")) == EXIT_FAILURE
    assert "no rule named" in capsys.readouterr().err


def test_rename_without_a_database(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(workspace, "rename", "A", "B")) == EXIT_FAILURE
    assert "no state database" in capsys.readouterr().err


# -- purge --------------------------------------------------------------------


def test_purge_with_nothing_to_do(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_state(workspace)
    assert main(_argv(workspace, "purge", "--yes")) == EXIT_OK
    assert "nothing to purge" in capsys.readouterr().out


def test_purge_deletes_orphans(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_state(workspace)

    database = Database(path=database_path(workspace["state"]))
    try:
        store = Store(database=database)
        PluginKeyValueStore(database=database, instance_name="Porch Mail").set("uid", "7")
        store.sync_rules([])
        store.sync_instances([])
    finally:
        database.close()

    assert main(_argv(workspace, "purge", "--yes")) == EXIT_OK
    output = capsys.readouterr().out
    assert "This will permanently delete" in output
    assert "purged 1 rule(s) and 2 instance(s)" in output

    database = Database(path=database_path(workspace["state"]))
    try:
        store = Store(database=database)
        assert store.latch("Package On Porch") is None
        assert store.rules() == []
        assert PluginKeyValueStore(database=database, instance_name="Porch Mail").get("uid") is None
    finally:
        database.close()


def test_purge_refuses_to_guess_when_not_a_terminal(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_state(workspace)
    database = Database(path=database_path(workspace["state"]))
    try:
        Store(database=database).sync_rules([])
    finally:
        database.close()

    assert main(_argv(workspace, "purge")) == EXIT_OK
    captured = capsys.readouterr()
    assert "re-run with --yes" in captured.err
    assert "cancelled" in captured.out

    database = Database(path=database_path(workspace["state"]))
    try:
        assert Store(database=database).latch("Package On Porch") is not None
    finally:
        database.close()
