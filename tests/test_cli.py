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
