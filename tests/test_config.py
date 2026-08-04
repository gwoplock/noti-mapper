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


def test_redefining_a_name_in_a_later_file_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "10-a.json", {"instances": {"Porch Mail": {"plugin": "imap-input"}}})
    _write(tmp_path, "20-b.json", {"instances": {"Porch Mail": {"plugin": "webhook-input"}}})

    errors = _errors(tmp_path)
    assert len(errors) == 1
    assert "duplicate instance name 'Porch Mail'" in errors[0]
    assert "10-a.json: instances → 'Porch Mail'" in errors[0]
    assert "20-b.json" in errors[0]


def test_duplicate_names_are_detected_case_insensitively(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {"instances": {"Porch Mail": {"plugin": "imap-input"}}},
    )
    _write(
        tmp_path,
        "b.json",
        {"instances": {"porch mail": {"plugin": "imap-input"}}},
    )
    errors = _errors(tmp_path)
    assert any("case-insensitively" in error for error in errors)


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


def test_errors_name_the_path_through_the_document(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instances": {"Porch Mail": {"plugin": "nope"}}})
    errors = _errors(tmp_path)
    assert "instances → 'Porch Mail' → plugin" in errors[0]


def test_one_bad_file_does_not_hide_errors_in_another(tmp_path: Path) -> None:
    tmp_path.joinpath("10-bad.json").write_text("{", encoding="utf-8")
    _write(tmp_path, "20-also-bad.json", {"instances": {"A": {"plugin": "nope"}}})
    errors = _errors(tmp_path)
    assert len(errors) == 2
    assert "10-bad.json" in errors[0]
    assert "20-also-bad.json" in errors[1]


def test_unknown_top_level_key_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instance": {}})
    errors = _errors(tmp_path)
    assert "unknown top-level key 'instance'" in errors[0]


def test_top_level_must_be_an_object(tmp_path: Path) -> None:
    tmp_path.joinpath("a.json").write_text("[]", encoding="utf-8")
    errors = _errors(tmp_path)
    assert "must be an object" in errors[0]


def test_unknown_instance_key_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instances": {"A": {"plugin": "imap-input", "conifg": {}}}})
    errors = _errors(tmp_path)
    assert any("unknown key 'conifg'" in error for error in errors)


def test_invalid_name_characters_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instances": {"Porch/Mail": {"plugin": "imap-input"}}})
    errors = _errors(tmp_path)
    assert "disallowed characters" in errors[0]


def test_unknown_plugin_lists_the_plugins_that_loaded(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instances": {"A": {"plugin": "imap"}}})
    errors = _errors(tmp_path)
    assert "unknown plugin 'imap'" in errors[0]
    assert "imap-input" in errors[0]


def test_unknown_plugin_with_no_plugins_loaded(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instances": {"A": {"plugin": "imap"}}})
    errors = _errors(tmp_path, known={})
    assert "(none loaded)" in errors[0]


def test_missing_plugin_field_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instances": {"A": {"config": {}}}})
    errors = _errors(tmp_path)
    assert 'has no "plugin"' in errors[0]


def test_config_must_be_an_object(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instances": {"A": {"plugin": "imap-input", "config": []}}})
    errors = _errors(tmp_path)
    assert '"config" must be an object' in errors[0]


def test_enabled_must_be_a_boolean(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instances": {"A": {"plugin": "imap-input", "enabled": "yes"}}})
    errors = _errors(tmp_path)
    assert "must be true or false" in errors[0]
