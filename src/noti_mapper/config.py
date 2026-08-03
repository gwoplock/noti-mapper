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
    JsonObject,
    Location,
)
from noti_mapper.secrets import SecretStore

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


@dataclass(frozen=True)
class ConfigError:
    """One problem with the configuration, and where it is."""

    message: str
    location: Location | None = None

    def __str__(self) -> str:
        if self.location is None:
            return self.message
        return f"{self.location}: {self.message}"


class ConfigurationError(Exception):
    """The configuration is unusable. Carries every problem found, not just the first."""

    def __init__(self, errors: list[ConfigError]) -> None:
        self.errors = _sorted_errors(errors)
        joined = "\n".join(f"  {error}" for error in self.errors)
        count = len(self.errors)
        noun = "error" if count == 1 else "errors"
        super().__init__(f"{count} configuration {noun}:\n{joined}")


def _sorted_errors(errors: list[ConfigError]) -> list[ConfigError]:
    def key(error: ConfigError) -> tuple[int, str, int, int]:
        if error.location is None:
            return (1, "", 0, 0)
        return (0, str(error.location.path), error.location.line, error.location.column)

    return sorted(errors, key=key)


def discover_config_files(directory: Path) -> list[Path]:
    """Return the ``*.json`` files in ``directory``, in lexical order."""
    return sorted(directory.glob("*.json"))


def load_configuration(
    *,
    config_directory: Path,
    secrets: SecretStore,
    known_plugins: Mapping[str, KnownPlugin],
) -> Configuration:
    """Load, merge, and validate the configuration.

    Raises :class:`ConfigurationError` carrying every problem found.
    """
    loader = _Loader(
        config_directory=config_directory,
        secrets=secrets,
        known_plugins=known_plugins,
    )
    return loader.load()


@dataclass
class _InstanceDraft:
    name: str
    node: JsonObject
    origin: Location


@dataclass
class _RuleDraft:
    name: str
    node: JsonObject
    origin: Location
