"""The rule graph, and the latch semantics that follow from it.

A rule has a set of inputs and a set of outputs. The latch belongs to the rule.

* An event from any input in the rule's input list sets that rule's latch.
  Inputs are OR'd.
* An output's state is the OR of the latches of every rule listing it as an
  output.
* An unlatch from output ``O`` clears every currently-set latch belonging to a
  rule that lists ``O`` as an output.

Two consequences surprise people, and both are worked through in
``docs/configuration.md``:

* A rule with two outputs couples them. Clearing from either output clears the
  rule, so both go quiet. A user wanting independent clearing writes two rules.
  This is the main advantage of rule-keyed latches over input-keyed ones: the
  coupling becomes a configuration decision rather than a hardcoded semantic.
* The same input appearing in two rules produces two independent latches. The
  input fires, both latch, and clearing one leaves the other set.

This module is pure: no I/O, no storage, no threads. It answers "which rules
does this instance touch" and "what should this output be, given these
latches", and nothing else.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    """A rule reduced to what the graph needs: a name and two instance lists."""

    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


class RuleGraph:
    """An index over the configured rules."""

    def __init__(self, rules: Sequence[Rule]) -> None:
        self._rules: dict[str, Rule] = {}
        self._by_input: dict[str, list[Rule]] = {}
        self._by_output: dict[str, list[Rule]] = {}

        for rule in rules:
            self._rules[rule.name] = rule
            for instance_name in rule.inputs:
                self._by_input.setdefault(instance_name, []).append(rule)
            for instance_name in rule.outputs:
                self._by_output.setdefault(instance_name, []).append(rule)

    def rules(self) -> list[Rule]:
        return [self._rules[name] for name in sorted(self._rules)]

    def rule(self, name: str) -> Rule | None:
        return self._rules.get(name)

    def rule_names(self) -> list[str]:
        return sorted(self._rules)

    def rules_for_input(self, instance_name: str) -> list[Rule]:
        """Every rule this instance can set the latch of."""
        return list(self._by_input.get(instance_name, []))

    def rules_for_output(self, instance_name: str) -> list[Rule]:
        """Every rule this instance renders, and can clear."""
        return list(self._by_output.get(instance_name, []))

    def input_names(self) -> list[str]:
        return sorted(self._by_input)

    def output_names(self) -> list[str]:
        return sorted(self._by_output)

    def instance_names(self) -> list[str]:
        return sorted(set(self._by_input) | set(self._by_output))

    def desired_output_state(self, *, instance_name: str, latches: Mapping[str, bool]) -> bool:
        """An output is true when any rule listing it as an output is latched."""
        return any(latches.get(rule.name, False) for rule in self._by_output.get(instance_name, []))

    def outputs_affected_by(self, rule_names: Sequence[str]) -> list[str]:
        """Every output instance whose state could change if these rules changed."""
        affected: set[str] = set()
        for rule_name in rule_names:
            rule = self._rules.get(rule_name)
            if rule is None:
                continue
            affected.update(rule.outputs)
        return sorted(affected)
