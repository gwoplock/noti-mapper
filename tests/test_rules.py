import json
import logging
from pathlib import Path

import pytest

from noti_mapper.config import KnownPlugin, PluginDirection, load_configuration
from noti_mapper.rules import Rule, RuleGraph
from noti_mapper.secrets import empty_store

INPUT_ONLY = frozenset({PluginDirection.INPUT})
OUTPUT_ONLY = frozenset({PluginDirection.OUTPUT})

_KNOWN = {
    "in": KnownPlugin(plugin_name="in", directions=INPUT_ONLY),
    "out": KnownPlugin(plugin_name="out", directions=OUTPUT_ONLY),
}


def _graph(*rules: Rule) -> RuleGraph:
    return RuleGraph(list(rules))


# -- indexing -----------------------------------------------------------------


def test_the_graph_indexes_by_input_and_by_output() -> None:
    graph = _graph(
        Rule(name="A", inputs=("Mail",), outputs=("Lamp", "Pager")),
        Rule(name="B", inputs=("Mail", "Hook"), outputs=("Lamp",)),
    )

    assert [rule.name for rule in graph.rules_for_input("Mail")] == ["A", "B"]
    assert [rule.name for rule in graph.rules_for_input("Hook")] == ["B"]
    assert [rule.name for rule in graph.rules_for_output("Lamp")] == ["A", "B"]
    assert [rule.name for rule in graph.rules_for_output("Pager")] == ["A"]

    assert graph.input_names() == ["Hook", "Mail"]
    assert graph.output_names() == ["Lamp", "Pager"]
    assert graph.instance_names() == ["Hook", "Lamp", "Mail", "Pager"]
    assert graph.rule_names() == ["A", "B"]


def test_unknown_instances_index_to_nothing() -> None:
    graph = _graph(Rule(name="A", inputs=("Mail",), outputs=("Lamp",)))
    assert graph.rules_for_input("Nope") == []
    assert graph.rules_for_output("Nope") == []
    assert graph.rule("Nope") is None


# -- output state is the OR of its rules' latches ------------------------------


def test_an_output_is_true_when_any_of_its_rules_is_latched() -> None:
    graph = _graph(
        Rule(name="A", inputs=("Mail",), outputs=("Shared",)),
        Rule(name="B", inputs=("Hook",), outputs=("Shared",)),
    )

    assert graph.desired_output_state(instance_name="Shared", latches={}) is False
    assert (
        graph.desired_output_state(instance_name="Shared", latches={"A": True, "B": False}) is True
    )
    assert (
        graph.desired_output_state(instance_name="Shared", latches={"A": False, "B": True}) is True
    )
    assert (
        graph.desired_output_state(instance_name="Shared", latches={"A": False, "B": False})
        is False
    )


def test_an_output_no_rule_names_is_false() -> None:
    graph = _graph(Rule(name="A", inputs=("Mail",), outputs=("Lamp",)))
    assert graph.desired_output_state(instance_name="Other", latches={"A": True}) is False


def test_outputs_affected_by_collects_across_rules() -> None:
    graph = _graph(
        Rule(name="A", inputs=("Mail",), outputs=("Lamp", "Pager")),
        Rule(name="B", inputs=("Hook",), outputs=("Lamp",)),
    )
    assert graph.outputs_affected_by(["A", "B"]) == ["Lamp", "Pager"]
    assert graph.outputs_affected_by(["B"]) == ["Lamp"]
    assert graph.outputs_affected_by(["Gone"]) == []


# -- building from configuration ----------------------------------------------


def _write(directory: Path, document: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps(document), encoding="utf-8")


def _from_config(tmp_path: Path, document: object, caplog: pytest.LogCaptureFixture) -> RuleGraph:
    _write(tmp_path, document)
    configuration = load_configuration(
        config_directory=tmp_path,
        secrets=empty_store(Path("secrets.json")),
        known_plugins=_KNOWN,
    )
    with caplog.at_level(logging.WARNING):
        return RuleGraph.from_configuration(configuration, logger=logging.getLogger("test.rules"))


def test_a_configuration_becomes_a_graph(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    graph = _from_config(
        tmp_path,
        {
            "instances": {
                "Mail": {"plugin": "in"},
                "Lamp": {"plugin": "out"},
                "Pager": {"plugin": "out"},
            },
            "rules": {"R": {"inputs": ["Mail"], "outputs": ["Lamp", "Pager"]}},
        },
        caplog,
    )
    rule = graph.rule("R")
    assert rule is not None
    assert rule.inputs == ("Mail",)
    assert rule.outputs == ("Lamp", "Pager")


def test_disabled_rules_are_not_in_the_graph(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    graph = _from_config(
        tmp_path,
        {
            "instances": {"Mail": {"plugin": "in"}, "Lamp": {"plugin": "out"}},
            "rules": {
                "Live": {"inputs": ["Mail"], "outputs": ["Lamp"]},
                "Off": {"inputs": ["Mail"], "outputs": ["Lamp"], "enabled": False},
            },
        },
        caplog,
    )
    assert graph.rule_names() == ["Live"]


def test_a_disabled_instance_drops_out_of_every_rule(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    graph = _from_config(
        tmp_path,
        {
            "instances": {
                "Mail": {"plugin": "in"},
                "Lamp": {"plugin": "out"},
                "Pager": {"plugin": "out", "enabled": False},
            },
            "rules": {"R": {"inputs": ["Mail"], "outputs": ["Lamp", "Pager"]}},
        },
        caplog,
    )
    rule = graph.rule("R")
    assert rule is not None
    assert rule.outputs == ("Lamp",)
    assert graph.output_names() == ["Lamp"]


def test_a_rule_left_with_no_outputs_is_kept_and_warned_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    graph = _from_config(
        tmp_path,
        {
            "instances": {
                "Mail": {"plugin": "in"},
                "Lamp": {"plugin": "out", "enabled": False},
            },
            "rules": {"R": {"inputs": ["Mail"], "outputs": ["Lamp"]}},
        },
        caplog,
    )
    rule = graph.rule("R")
    assert rule is not None
    assert rule.outputs == ()
    assert "nothing can unlatch it" in caplog.text


def test_a_rule_left_with_no_inputs_is_warned_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _from_config(
        tmp_path,
        {
            "instances": {
                "Mail": {"plugin": "in", "enabled": False},
                "Lamp": {"plugin": "out"},
            },
            "rules": {"R": {"inputs": ["Mail"], "outputs": ["Lamp"]}},
        },
        caplog,
    )
    assert "nothing can set its latch" in caplog.text
