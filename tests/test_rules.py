from noti_mapper.config import KnownPlugin, PluginDirection
from noti_mapper.rules import Rule, RuleGraph

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
