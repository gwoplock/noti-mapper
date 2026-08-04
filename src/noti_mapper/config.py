"""Configuration loading and validation.

Configuration is a set of JSON files in ``/etc/noti-mapper.d/``, read in
lexical order and merged. Later files may add instances and rules; redefining
a name that an earlier file already defined is an error, not an override.

Validation reports every problem in one pass, each with the file it is in and
the path through that file to the thing that is wrong. Failing on the first
error and making the user fix them one restart at a time is the behaviour this
is written to avoid.
"""

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_DIRECTORY: Path = Path("/etc/noti-mapper.d")

_TOP_LEVEL_KEYS = ("daemon", "instances", "rules")
_INSTANCE_KEYS = ("plugin", "config", "enabled")
_RULE_KEYS = ("inputs", "outputs", "enabled")


@dataclass(frozen=True)
class ConfigPath:
    """Where in a configuration file something is.

    A path through the document rather than a line and column. ``json.loads``
    reports a position for a syntax error and for nothing else, and recovering
    per-value positions means maintaining a second parser in step with the
    first one forever.

    ``instances -> 'Porch Mail' -> plugin`` tells a reader where to look
    without any of that, and unlike a line number it survives the file being
    reformatted.
    """

    file: Path
    steps: tuple[str, ...] = ()

    def key(self, name: str) -> "ConfigPath":
        """Descend into a schema key, spelled the way the schema spells it."""
        return ConfigPath(file=self.file, steps=(*self.steps, name))

    def named(self, value: str) -> "ConfigPath":
        """Descend into a user-chosen name, quoted so it stands out from schema."""
        return ConfigPath(file=self.file, steps=(*self.steps, repr(value)))

    def element(self, index: int) -> "ConfigPath":
        """Descend into an array element, subscripting the step above it."""
        if not self.steps:
            return ConfigPath(file=self.file, steps=(f"[{index}]",))
        return ConfigPath(file=self.file, steps=(*self.steps[:-1], f"{self.steps[-1]}[{index}]"))

    def __str__(self) -> str:
        if not self.steps:
            return str(self.file)
        return f"{self.file}: " + " → ".join(self.steps)


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
    origin: ConfigPath

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
    origin: ConfigPath


@dataclass(frozen=True)
class Configuration:
    """Everything the daemon needs from ``/etc/noti-mapper.d/``."""

    instances: Mapping[str, InstanceConfig]
    rules: Mapping[str, RuleConfig]
    files: tuple[Path, ...]
    daemon: DaemonSettings = DaemonSettings()

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
