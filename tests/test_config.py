import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from noti_mapper.config import (
    Configuration,
    ConfigurationError,
    KnownPlugin,
    PluginDirection,
    load_configuration,
)
from noti_mapper.secrets import SecretStore, empty_store

INPUT_ONLY = frozenset({PluginDirection.INPUT})
OUTPUT_ONLY = frozenset({PluginDirection.OUTPUT})
BOTH = frozenset({PluginDirection.INPUT, PluginDirection.OUTPUT})


def _plugins() -> dict[str, KnownPlugin]:
    return {
        "imap-input": KnownPlugin(plugin_name="imap-input", directions=INPUT_ONLY),
        "webhook-input": KnownPlugin(plugin_name="webhook-input", directions=INPUT_ONLY),
        "homekit-output": KnownPlugin(plugin_name="homekit-output", directions=OUTPUT_ONLY),
        "pagerduty-output": KnownPlugin(plugin_name="pagerduty-output", directions=OUTPUT_ONLY),
        "loopback": KnownPlugin(plugin_name="loopback", directions=BOTH),
    }


def _write(directory: Path, name: str, document: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _load(
    directory: Path,
    *,
    secrets: SecretStore | None = None,
    known: Mapping[str, KnownPlugin] | None = None,
) -> Configuration:
    return load_configuration(
        config_directory=directory,
        secrets=secrets if secrets is not None else empty_store(Path("secrets.json")),
        known_plugins=known if known is not None else _plugins(),
    )


def _errors(
    directory: Path,
    *,
    secrets: SecretStore | None = None,
    known: Mapping[str, KnownPlugin] | None = None,
) -> list[str]:
    with pytest.raises(ConfigurationError) as caught:
        _load(directory, secrets=secrets, known=known)
    return [str(error) for error in caught.value.errors]


def test_a_missing_config_directory_is_an_error(tmp_path: Path) -> None:
    errors = _errors(tmp_path / "absent")
    assert len(errors) == 1
    assert "does not exist" in errors[0]


def test_an_empty_config_directory_is_an_error(tmp_path: Path) -> None:
    tmp_path.joinpath("ignored.txt").write_text("", encoding="utf-8")
    errors = _errors(tmp_path)
    assert "no *.json configuration files" in errors[0]


# -- structural validation ----------------------------------------------------


def test_malformed_json_reports_the_file_and_the_position(tmp_path: Path) -> None:
    """The standard library gives a position for syntax errors, so it is passed on."""
    tmp_path.joinpath("bad.json").write_text('{\n  "instances": {,\n}\n', encoding="utf-8")
    errors = _errors(tmp_path)
    assert "bad.json" in errors[0]
    assert "line 2" in errors[0]


def test_duplicate_keys_in_one_object_are_an_error(tmp_path: Path) -> None:
    tmp_path.joinpath("dup.json").write_text('{"instances": {}, "instances": {}}', encoding="utf-8")
    errors = _errors(tmp_path)
    assert "duplicate key 'instances'" in errors[0]


def test_nan_and_infinity_are_rejected(tmp_path: Path) -> None:
    tmp_path.joinpath("nan.json").write_text('{"daemon": {"retry_max_seconds": NaN}}', "utf-8")
    errors = _errors(tmp_path)
    assert "NaN is not valid JSON" in errors[0]


def test_unknown_top_level_key_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instance": {}})
    errors = _errors(tmp_path)
    assert "unknown top-level key 'instance'" in errors[0]


def test_top_level_must_be_an_object(tmp_path: Path) -> None:
    tmp_path.joinpath("a.json").write_text("[]", encoding="utf-8")
    errors = _errors(tmp_path)
    assert "must be an object" in errors[0]
