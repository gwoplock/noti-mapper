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

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from noti_mapper.config import Configuration


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

    @classmethod
    def from_configuration(
        cls, configuration: Configuration, *, logger: logging.Logger | None = None
    ) -> "RuleGraph":
        """Build the graph from configuration, dropping what is disabled.

        A disabled instance is removed from every rule's lists rather than the
        rules being removed: a rule whose inputs are all disabled can still be
        cleared, and a rule whose outputs are all disabled still latches. Both
        are worth a warning, because a rule with no outputs left has no way to
        be unlatched.
        """
        log = logger if logger is not None else logging.getLogger(__name__)
        enabled_instances = {instance.name for instance in configuration.enabled_instances()}

        rules: list[Rule] = []
        for rule_config in configuration.enabled_rules():
            inputs = tuple(name for name in rule_config.inputs if name in enabled_instances)
            outputs = tuple(name for name in rule_config.outputs if name in enabled_instances)
            if not outputs:
                log.warning(
                    "rule %r has no enabled outputs; it can latch but nothing can "
                    "unlatch it except an operator",
                    rule_config.name,
                    extra={"rule": rule_config.name},
                )
            if not inputs:
                log.warning(
                    "rule %r has no enabled inputs; nothing can set its latch",
                    rule_config.name,
                    extra={"rule": rule_config.name},
                )
            rules.append(Rule(name=rule_config.name, inputs=inputs, outputs=outputs))

        return cls(rules)

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
