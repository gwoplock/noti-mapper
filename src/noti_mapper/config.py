"""Configuration loading and validation.

Configuration is a set of JSON files in ``/etc/noti-mapper.d/``, read in
lexical order and merged. Later files may add instances and rules; redefining
a name that an earlier file already defined is an error, not an override.

Validation reports every problem in one pass, each with a file and a line.
Failing on the first error and making the user fix them one restart at a time
is the behaviour this is written to avoid.
"""

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from noti_mapper.jsonloc import (
    Location,
)

DEFAULT_CONFIG_DIRECTORY: Path = Path("/etc/noti-mapper.d")

_TOP_LEVEL_KEYS = ("instances", "rules")
_INSTANCE_KEYS = ("plugin", "config", "enabled")
_RULE_KEYS = ("inputs", "outputs", "enabled")


class PluginDirection(enum.Enum):
    """Which side of the rule graph a plugin can sit on."""

    INPUT = "input"
    OUTPUT = "output"


@dataclass(frozen=True)
class KnownPlugin:
    """A plugin that loaded successfully, as far as config validation cares.

    ``validate_settings`` is supplied by the plugin class and returns a list of
    human-readable problems with an instance's ``config`` block -- empty when
    the block is acceptable. It returns a list rather than raising so that all
    problems across all instances land in one report.
    """

    plugin_name: str
    directions: frozenset[PluginDirection]
    validate_settings: Callable[[Mapping[str, object]], list[str]] | None = None


@dataclass(frozen=True)
class InstanceConfig:
    """A configured, named occurrence of a plugin."""

    name: str
    plugin: str
    directions: frozenset[PluginDirection]
    settings: Mapping[str, object]
    enabled: bool
    origin: Location

    def is_input(self) -> bool:
        return PluginDirection.INPUT in self.directions

    def is_output(self) -> bool:
        return PluginDirection.OUTPUT in self.directions


@dataclass(frozen=True)
class RuleConfig:
    """A named mapping from a set of input instances to a set of output instances."""

    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    enabled: bool
    origin: Location


@dataclass(frozen=True)
class Configuration:
    """Everything the daemon needs from ``/etc/noti-mapper.d/``."""

    instances: Mapping[str, InstanceConfig]
    rules: Mapping[str, RuleConfig]
    files: tuple[Path, ...]

    def enabled_instances(self) -> list[InstanceConfig]:
        result: list[InstanceConfig] = []
        for instance in self.instances.values():
            if instance.enabled:
                result.append(instance)
        return sorted(result, key=lambda instance: instance.name)

    def enabled_rules(self) -> list[RuleConfig]:
        result: list[RuleConfig] = []
        for rule in self.rules.values():
            if rule.enabled:
                result.append(rule)
        return sorted(result, key=lambda rule: rule.name)
