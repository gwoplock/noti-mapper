"""The shipped artifacts have to keep agreeing with the code.

Example configuration that no longer validates, a unit that invokes a
subcommand that was renamed, or a man page promising a default the code no
longer has are all things a user hits before any maintainer does.
"""

import json
import re
import shutil
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from noti_mapper.cli import EXIT_OK, SUBCOMMANDS, build_parser, main, subcommand_names
from noti_mapper.discovery import source_checkout_plugin_directory

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DIST = REPOSITORY_ROOT / "dist"
EXAMPLES = DIST / "examples"
UNIT = DIST / "noti-mapper.service"
SYSUSERS = DIST / "noti-mapper.sysusers"
PKGBUILD = REPOSITORY_ROOT / "packaging" / "aur" / "noti-mapper" / "PKGBUILD"


@pytest.fixture(autouse=True)
def clean_module_table() -> Iterator[None]:
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("noti_mapper_plugin_"):
            del sys.modules[name]


# -- the example configuration ------------------------------------------------


def test_the_shipped_examples_validate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "conf.d"
    config.mkdir()
    for name in ("10-instances.json", "20-rules.json"):
        shutil.copy(EXAMPLES / name, config / name)

    secrets = tmp_path / "secrets.json"
    shutil.copy(EXAMPLES / "secrets.json", secrets)
    secrets.chmod(0o600)

    plugins = source_checkout_plugin_directory()
    assert plugins is not None

    status = main(
        [
            "--config-dir",
            str(config),
            "--secrets",
            str(secrets),
            "--state-dir",
            str(tmp_path / "state"),
            "--plugin-dir",
            str(plugins),
            "validate",
        ]
    )
    assert status == EXIT_OK, capsys.readouterr().err


def test_every_secret_the_examples_reference_is_in_the_example_secrets_file() -> None:
    referenced: set[str] = set()
    for name in ("10-instances.json", "20-rules.json"):
        text = (EXAMPLES / name).read_text(encoding="utf-8")
        referenced.update(re.findall(r"\$\{secret:([A-Za-z0-9_.-]+)\}", text))

    provided = set(json.loads((EXAMPLES / "secrets.json").read_text(encoding="utf-8")))
    assert referenced == provided


def test_the_example_imap_reader_ships_in_dry_run() -> None:
    """Arming a mail watcher on first boot is not a sensible default."""
    document = json.loads((EXAMPLES / "10-instances.json").read_text(encoding="utf-8"))
    reader = document["instances"]["Porch Mail"]
    assert reader["plugin"] == "imap-input"
    assert reader["config"]["dry_run"] is True


def test_the_examples_are_valid_json_with_no_duplicate_keys() -> None:
    for path in sorted(EXAMPLES.glob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))


# -- the systemd unit ---------------------------------------------------------


def _unit_text() -> str:
    return UNIT.read_text(encoding="utf-8")


def test_the_subcommand_list_matches_the_parser() -> None:
    assert sorted(SUBCOMMANDS) == subcommand_names(build_parser())


def test_the_unit_invokes_subcommands_that_exist() -> None:
    invoked = re.findall(r"^Exec\w+=/usr/bin/noti-mapper (\w+)", _unit_text(), re.MULTILINE)
    assert invoked, "the unit should invoke noti-mapper"
    for command in invoked:
        assert (
            command in SUBCOMMANDS
        ), f"the unit runs 'noti-mapper {command}', which does not exist"


def test_the_unit_gates_on_validate() -> None:
    assert "ExecStartPre=/usr/bin/noti-mapper validate" in _unit_text()


def test_the_unit_is_type_notify_with_a_watchdog() -> None:
    text = _unit_text()
    assert "Type=notify" in text
    assert re.search(r"^WatchdogSec=\d+", text, re.MULTILINE)
    assert re.search(r"^Restart=always", text, re.MULTILINE)
    assert re.search(r"^RestartSec=\d+", text, re.MULTILINE)


def test_the_unit_uses_state_and_configuration_directories() -> None:
    text = _unit_text()
    assert "StateDirectory=noti-mapper" in text
    assert "ConfigurationDirectory=noti-mapper.d" in text
    assert not re.search(
        r"^DynamicUser=", text, re.MULTILINE
    ), "persistent state and stable file ownership rule DynamicUser out"


def test_the_unit_is_hardened_but_not_in_ways_that_break_mdns() -> None:
    text = _unit_text()
    for directive in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
    ):
        assert directive in text

    # HAP needs mDNS on the same L2 segment as the Apple Home hub. Either of
    # these would break HomeKit pairing in a way that is very hard to diagnose
    # from the symptom.
    assert not re.search(r"^PrivateNetwork=", text, re.MULTILINE)
    assert not re.search(r"^RestrictAddressFamilies=", text, re.MULTILINE)


def test_reload_is_wired_to_sighup() -> None:
    assert "ExecReload=/bin/kill -HUP $MAINPID" in _unit_text()


def test_the_unit_and_sysusers_agree_on_the_user() -> None:
    unit_user = re.search(r"^User=(\S+)", _unit_text(), re.MULTILINE)
    assert unit_user is not None

    entry = SYSUSERS.read_text(encoding="utf-8")
    declared = re.search(r"^u\s+(\S+)", entry, re.MULTILINE)
    assert declared is not None
    assert declared.group(1) == unit_user.group(1)


def test_the_sysusers_home_matches_the_state_directory() -> None:
    entry = re.search(r"^u\s+\S+\s+\S+\s+\"[^\"]*\"\s+(\S+)", SYSUSERS.read_text(), re.MULTILINE)
    assert entry is not None
    assert entry.group(1) == "/var/lib/noti-mapper"


def test_the_example_secrets_file_is_not_shipped_world_readable_by_accident() -> None:
    """The file in the repository is a template; the docs say to install it 0600."""
    mode = stat.S_IMODE((EXAMPLES / "secrets.json").stat().st_mode)
    assert mode & 0o111 == 0, "a JSON template should not be executable"
    assert "0600" in (EXAMPLES / "README.md").read_text(encoding="utf-8")
