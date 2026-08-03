import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from noti_mapper.config import (
    Configuration,
    ConfigurationError,
    KnownPlugin,
    PluginDirection,
    discover_config_files,
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


# -- happy path ---------------------------------------------------------------


def test_the_documented_example_loads(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "10-instances.json",
        {
            "instances": {
                "Porch Mail": {
                    "plugin": "imap-input",
                    "config": {
                        "host": "mail.example.net",
                        "senders": ["amazon.com", "ups.com"],
                        "subject_patterns": ["^Delivered:"],
                        "dry_run": False,
                    },
                },
                "Porch Lamp": {
                    "plugin": "homekit-output",
                    "config": {"display_name": "Package Waiting"},
                },
                "Porch Pager": {"plugin": "pagerduty-output"},
            }
        },
    )
    _write(
        tmp_path,
        "20-rules.json",
        {
            "rules": {
                "Package On Porch": {
                    "inputs": ["Porch Mail"],
                    "outputs": ["Porch Lamp", "Porch Pager"],
                }
            }
        },
    )

    configuration = _load(tmp_path)
    assert isinstance(configuration.instances, Mapping)
    assert sorted(configuration.instances) == ["Porch Lamp", "Porch Mail", "Porch Pager"]

    rule = configuration.rules["Package On Porch"]
    assert rule.inputs == ("Porch Mail",)
    assert rule.outputs == ("Porch Lamp", "Porch Pager")
    assert rule.enabled is True

    mail = configuration.instances["Porch Mail"]
    assert mail.plugin == "imap-input"
    assert mail.settings["host"] == "mail.example.net"
    assert mail.settings["dry_run"] is False
    assert mail.is_input() and not mail.is_output()

    pager = configuration.instances["Porch Pager"]
    assert pager.settings == {}


def test_instances_and_rules_may_share_one_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "all.json",
        {
            "instances": {
                "In": {"plugin": "imap-input"},
                "Out": {"plugin": "homekit-output"},
            },
            "rules": {"R": {"inputs": ["In"], "outputs": ["Out"]}},
        },
    )
    configuration = _load(tmp_path)
    assert list(configuration.rules) == ["R"]


def test_a_rule_may_share_a_name_with_an_instance(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "all.json",
        {
            "instances": {
                "Porch": {"plugin": "imap-input"},
                "Out": {"plugin": "homekit-output"},
            },
            "rules": {"Porch": {"inputs": ["Porch"], "outputs": ["Out"]}},
        },
    )
    configuration = _load(tmp_path)
    assert "Porch" in configuration.instances
    assert "Porch" in configuration.rules


def test_names_are_stripped_of_surrounding_whitespace(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "all.json",
        {
            "instances": {
                "  Porch Mail  ": {"plugin": "imap-input"},
                "Out": {"plugin": "homekit-output"},
            },
            "rules": {"R": {"inputs": ["Porch Mail"], "outputs": ["Out"]}},
        },
    )
    configuration = _load(tmp_path)
    assert "Porch Mail" in configuration.instances


def test_enabled_false_is_carried_through(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "all.json",
        {
            "instances": {
                "In": {"plugin": "imap-input", "enabled": False},
                "Out": {"plugin": "homekit-output"},
            },
            "rules": {"R": {"inputs": ["In"], "outputs": ["Out"], "enabled": False}},
        },
    )
    configuration = _load(tmp_path)
    assert configuration.instances["In"].enabled is False
    assert configuration.enabled_instances() == [configuration.instances["Out"]]
    assert configuration.enabled_rules() == []


# -- merging ------------------------------------------------------------------


def test_files_are_read_in_lexical_order(tmp_path: Path) -> None:
    _write(tmp_path, "20-b.json", {"instances": {"B": {"plugin": "homekit-output"}}})
    _write(tmp_path, "10-a.json", {"instances": {"A": {"plugin": "imap-input"}}})
    _write(tmp_path, "30-r.json", {"rules": {"R": {"inputs": ["A"], "outputs": ["B"]}}})

    assert [path.name for path in discover_config_files(tmp_path)] == [
        "10-a.json",
        "20-b.json",
        "30-r.json",
    ]
    configuration = _load(tmp_path)
    assert sorted(configuration.instances) == ["A", "B"]


def test_redefining_a_name_in_a_later_file_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "10-a.json", {"instances": {"Porch Mail": {"plugin": "imap-input"}}})
    _write(tmp_path, "20-b.json", {"instances": {"Porch Mail": {"plugin": "webhook-input"}}})

    errors = _errors(tmp_path)
    assert len(errors) == 1
    assert "duplicate instance name 'Porch Mail'" in errors[0]
    assert "10-a.json:3:5" in errors[0]
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


def test_malformed_json_reports_file_and_line(tmp_path: Path) -> None:
    tmp_path.joinpath("bad.json").write_text('{\n  "instances": {,\n}\n', encoding="utf-8")
    errors = _errors(tmp_path)
    assert "bad.json:2:17" in errors[0]


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


def test_unknown_rule_key_is_an_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {"A": {"plugin": "imap-input"}, "B": {"plugin": "homekit-output"}},
            "rules": {"R": {"inputs": ["A"], "outputs": ["B"], "input": ["A"]}},
        },
    )
    errors = _errors(tmp_path)
    assert any("unknown key 'input'" in error for error in errors)


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


# -- rule validation ----------------------------------------------------------


def test_rule_referencing_an_unknown_instance(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {"Out": {"plugin": "homekit-output"}},
            "rules": {"R": {"inputs": ["Nope"], "outputs": ["Out"]}},
        },
    )
    errors = _errors(tmp_path)
    assert "unknown instance 'Nope'" in errors[0]
    assert "Defined instances: Out" in errors[0]


def test_a_case_mismatched_reference_suggests_the_real_name(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {
                "Porch Mail": {"plugin": "imap-input"},
                "Out": {"plugin": "homekit-output"},
            },
            "rules": {"R": {"inputs": ["porch mail"], "outputs": ["Out"]}},
        },
    )
    errors = _errors(tmp_path)
    assert "did you mean 'Porch Mail'?" in errors[0]


def test_an_output_instance_may_not_be_listed_as_an_input(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {
                "Lamp": {"plugin": "homekit-output"},
                "Mail": {"plugin": "imap-input"},
            },
            "rules": {"R": {"inputs": ["Lamp"], "outputs": ["Lamp"]}},
        },
    )
    errors = _errors(tmp_path)
    assert any("lists 'Lamp' under \"inputs\"" in error for error in errors)
    assert any("provides only: output" in error for error in errors)


def test_an_input_instance_may_not_be_listed_as_an_output(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {"Mail": {"plugin": "imap-input"}},
            "rules": {"R": {"inputs": ["Mail"], "outputs": ["Mail"]}},
        },
    )
    errors = _errors(tmp_path)
    assert any('under "outputs"' in error for error in errors)


def test_an_instance_may_not_be_both_input_and_output_of_one_rule(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {"Both": {"plugin": "loopback"}},
            "rules": {"R": {"inputs": ["Both"], "outputs": ["Both"]}},
        },
    )
    errors = _errors(tmp_path)
    assert "would make the rule latch itself" in errors[0]


def test_empty_inputs_or_outputs_is_an_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {"Out": {"plugin": "homekit-output"}},
            "rules": {"R": {"inputs": [], "outputs": ["Out"]}},
        },
    )
    errors = _errors(tmp_path)
    assert "is empty" in errors[0]


def test_missing_inputs_key_is_an_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {"Out": {"plugin": "homekit-output"}},
            "rules": {"R": {"outputs": ["Out"]}},
        },
    )
    errors = _errors(tmp_path)
    assert 'has no "inputs"' in errors[0]


def test_repeating_an_instance_in_one_list_is_an_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {
                "In": {"plugin": "imap-input"},
                "Out": {"plugin": "homekit-output"},
            },
            "rules": {"R": {"inputs": ["In", "In"], "outputs": ["Out"]}},
        },
    )
    errors = _errors(tmp_path)
    assert "is listed twice" in errors[0]


def test_a_broken_instance_does_not_also_produce_unknown_instance_errors(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {
                "In": {"plugin": "does-not-exist"},
                "Out": {"plugin": "homekit-output"},
            },
            "rules": {"R": {"inputs": ["In"], "outputs": ["Out"]}},
        },
    )
    errors = _errors(tmp_path)
    assert len(errors) == 1
    assert "unknown plugin" in errors[0]


# -- one pass -----------------------------------------------------------------


def test_every_error_is_reported_in_one_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {
                "Bad Plugin": {"plugin": "nope"},
                "Bad/Name": {"plugin": "imap-input"},
                "Out": {"plugin": "homekit-output", "enabled": "yes"},
            },
            "rules": {
                "R1": {"inputs": ["Missing"], "outputs": ["Out"]},
                "R2": {"inputs": [], "outputs": ["Out"]},
            },
        },
    )
    errors = _errors(tmp_path)
    assert len(errors) == 5


def test_errors_are_sorted_by_position(tmp_path: Path) -> None:
    _write(tmp_path, "10-a.json", {"instances": {"A": {"plugin": "nope1"}}})
    _write(tmp_path, "20-b.json", {"instances": {"B": {"plugin": "nope2"}}})
    errors = _errors(tmp_path)
    assert "10-a.json" in errors[0]
    assert "20-b.json" in errors[1]


# -- secrets ------------------------------------------------------------------


def test_secret_references_are_resolved_in_nested_config(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "instances": {
                "Mail": {
                    "plugin": "imap-input",
                    "config": {
                        "password": "${secret:pw}",
                        "nested": {"token": "${secret:tok}"},
                        "list": ["${secret:pw}", 5],
                    },
                }
            }
        },
    )
    store = SecretStore(path=Path("s.json"), values={"pw": "hunter2", "tok": "abc"})
    configuration = _load(tmp_path, secrets=store)
    settings = configuration.instances["Mail"].settings
    assert settings["password"] == "hunter2"
    assert settings["nested"] == {"token": "abc"}
    assert settings["list"] == ["hunter2", 5]


def test_a_missing_secret_is_a_config_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {"instances": {"Mail": {"plugin": "imap-input", "config": {"password": "${secret:pw}"}}}},
    )
    errors = _errors(tmp_path)
    assert "undefined secret 'pw'" in errors[0]
    assert "(none defined)" in errors[0]


def test_a_missing_secret_lists_the_defined_ones(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {"instances": {"Mail": {"plugin": "imap-input", "config": {"password": "${secret:pw}"}}}},
    )
    store = SecretStore(path=Path("s.json"), values={"other": "x"})
    errors = _errors(tmp_path, secrets=store)
    assert "defines: other" in errors[0]


# -- plugin-supplied settings validation --------------------------------------


def test_plugin_settings_validation_errors_are_reported(tmp_path: Path) -> None:
    def validate(settings: Mapping[str, object]) -> list[str]:
        problems: list[str] = []
        if "host" not in settings:
            problems.append('"host" is required')
        if "folder" not in settings:
            problems.append('"folder" is required')
        return problems

    known = {
        "imap-input": KnownPlugin(
            plugin_name="imap-input", directions=INPUT_ONLY, validate_settings=validate
        )
    }
    _write(tmp_path, "a.json", {"instances": {"Mail": {"plugin": "imap-input", "config": {}}}})
    errors = _errors(tmp_path, known=known)
    assert len(errors) == 2
    assert '"host" is required' in errors[0]
    assert '"folder" is required' in errors[1]


def test_plugin_settings_validation_sees_resolved_secrets(tmp_path: Path) -> None:
    seen: list[Mapping[str, object]] = []

    def validate(settings: Mapping[str, object]) -> list[str]:
        seen.append(settings)
        return []

    known = {
        "imap-input": KnownPlugin(
            plugin_name="imap-input", directions=INPUT_ONLY, validate_settings=validate
        )
    }
    _write(
        tmp_path,
        "a.json",
        {"instances": {"Mail": {"plugin": "imap-input", "config": {"password": "${secret:pw}"}}}},
    )
    store = SecretStore(path=Path("s.json"), values={"pw": "hunter2"})
    _load(tmp_path, secrets=store, known=known)
    assert seen == [{"password": "hunter2"}]


# -- daemon-wide settings -----------------------------------------------------


def test_daemon_settings_default_when_absent(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"instances": {"A": {"plugin": "imap-input"}}})
    configuration = _load(tmp_path)
    assert configuration.daemon.event_log_max_rows == 10_000
    assert configuration.daemon.dispatcher_threads == 4
    assert configuration.daemon.retry_initial_seconds == 5.0
    assert configuration.daemon.retry_max_seconds == 900.0


def test_daemon_settings_are_read(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.json",
        {
            "daemon": {
                "event_log_max_rows": 500,
                "dispatcher_threads": 2,
                "retry_initial_seconds": 1,
                "retry_max_seconds": 60.5,
            },
            "instances": {"A": {"plugin": "imap-input"}},
        },
    )
    daemon = _load(tmp_path).daemon
    assert daemon.event_log_max_rows == 500
    assert daemon.dispatcher_threads == 2
    assert daemon.retry_initial_seconds == 1.0
    assert daemon.retry_max_seconds == 60.5


def test_a_second_daemon_block_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "10-a.json", {"daemon": {"dispatcher_threads": 2}})
    _write(tmp_path, "20-b.json", {"daemon": {"dispatcher_threads": 3}})
    errors = _errors(tmp_path)
    assert any('a second "daemon" block' in error for error in errors)


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "4", None])
def test_daemon_integers_must_be_positive_integers(tmp_path: Path, value: object) -> None:
    _write(
        tmp_path,
        "a.json",
        {"daemon": {"dispatcher_threads": value}, "instances": {"A": {"plugin": "imap-input"}}},
    )
    errors = _errors(tmp_path)
    assert any("must be a positive integer" in error for error in errors)


@pytest.mark.parametrize("value", [0, -1, True, "4", None])
def test_daemon_numbers_must_be_positive(tmp_path: Path, value: object) -> None:
    _write(
        tmp_path,
        "a.json",
        {"daemon": {"retry_max_seconds": value}, "instances": {"A": {"plugin": "imap-input"}}},
    )
    errors = _errors(tmp_path)
    assert any("must be a positive number" in error for error in errors)


def test_unknown_daemon_key_is_an_error(tmp_path: Path) -> None:
    _write(
        tmp_path, "a.json", {"daemon": {"threads": 2}, "instances": {"A": {"plugin": "imap-input"}}}
    )
    errors = _errors(tmp_path)
    assert any("unknown key 'threads'" in error for error in errors)
