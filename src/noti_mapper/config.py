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
    JsonParseError,
    Location,
    parse_file,
)
from noti_mapper.names import (
    NameRegistry,
    Namespace,
    find_name_problems,
    normalize_name,
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


class _Loader:
    """Accumulates errors while walking the configuration files."""

    def __init__(
        self,
        *,
        config_directory: Path,
        secrets: SecretStore,
        known_plugins: Mapping[str, KnownPlugin],
    ) -> None:
        self._config_directory = config_directory
        self._secrets = secrets
        self._known_plugins = known_plugins
        self._errors: list[ConfigError] = []
        self._registry = NameRegistry()
        self._instance_drafts: list[_InstanceDraft] = []
        self._rule_drafts: list[_RuleDraft] = []
        # Instances that were declared but failed to build. A rule referencing
        # one of these should not also be told the instance does not exist --
        # that turns one mistake into two errors and hides the real one.
        self._broken_instances: set[str] = set()

    def load(self) -> Configuration:
        files = self._read_files()
        for path in files:
            document = self._parse(path)
            if document is not None:
                self._collect_document(document=document)

        instances = self._build_instances()
        rules = self._build_rules(instances)

        if self._errors:
            raise ConfigurationError(self._errors)

        return Configuration(instances=instances, rules=rules, files=tuple(files))

    # -- file handling --------------------------------------------------------

    def _read_files(self) -> list[Path]:
        if not self._config_directory.is_dir():
            self._errors.append(
                ConfigError(f"configuration directory {self._config_directory} does not exist")
            )
            return []
        files = discover_config_files(self._config_directory)
        if not files:
            self._errors.append(
                ConfigError(f"no *.json configuration files in {self._config_directory}")
            )
        return files

    def _parse(self, path: Path) -> JsonObject | None:
        try:
            document = parse_file(path)
        except JsonParseError as error:
            self._errors.append(ConfigError(error.message, error.location))
            return None
        except OSError as error:
            self._errors.append(ConfigError(f"cannot read {path}: {error.strerror}"))
            return None

        if not isinstance(document, JsonObject):
            self._errors.append(
                ConfigError(
                    "the top level of a configuration file must be an object with "
                    '"instances" and/or "rules" keys',
                    document.location,
                )
            )
            return None
        return document

    def _collect_document(self, *, document: JsonObject) -> None:
        for key, node in document.members.items():
            location = document.key_locations[key]
            if key not in _TOP_LEVEL_KEYS:
                self._errors.append(
                    ConfigError(
                        f"unknown top-level key {key!r}; expected one of "
                        f"{', '.join(repr(name) for name in _TOP_LEVEL_KEYS)}",
                        location,
                    )
                )
                continue
            if not isinstance(node, JsonObject):
                self._errors.append(
                    ConfigError(f"{key!r} must be an object keyed by name", node.location)
                )
                continue
            if key == "instances":
                self._collect_named(container=node, namespace=Namespace.INSTANCE)
            else:
                self._collect_named(container=node, namespace=Namespace.RULE)

    def _collect_named(self, *, container: JsonObject, namespace: Namespace) -> None:
        for raw_name, node in container.members.items():
            location = container.key_locations[raw_name]

            problems = find_name_problems(raw_name)
            if problems:
                for problem in problems:
                    self._errors.append(
                        ConfigError(f"{namespace.value} name {raw_name!r}: {problem}", location)
                    )
                continue

            name = normalize_name(raw_name)
            conflict = self._registry.find_conflict(name=name, namespace=namespace)
            if conflict is not None:
                self._errors.append(
                    ConfigError(
                        f"duplicate {namespace.value} name {name!r}; already defined as "
                        f"{conflict.name!r} at {conflict.origin}. Names are unique "
                        "case-insensitively, and later files may not redefine them.",
                        location,
                    )
                )
                continue

            if not isinstance(node, JsonObject):
                self._errors.append(
                    ConfigError(f"{namespace.value} {name!r} must be an object", node.location)
                )
                continue

            self._registry.register(name=name, namespace=namespace, origin=str(location))
            if namespace is Namespace.INSTANCE:
                self._instance_drafts.append(_InstanceDraft(name=name, node=node, origin=location))
            else:
                self._rule_drafts.append(_RuleDraft(name=name, node=node, origin=location))

    # -- instances ------------------------------------------------------------

    def _build_instances(self) -> dict[str, InstanceConfig]:
        instances: dict[str, InstanceConfig] = {}
        for draft in self._instance_drafts:
            instance = self._build_instance(draft)
            if instance is None:
                self._broken_instances.add(draft.name)
            else:
                instances[instance.name] = instance
        return instances

    # -- rules ----------------------------------------------------------------

    def _build_rules(self, instances: Mapping[str, InstanceConfig]) -> dict[str, RuleConfig]:
        rules: dict[str, RuleConfig] = {}
        for draft in self._rule_drafts:
            rule = self._build_rule(draft=draft, instances=instances)
            if rule is not None:
                rules[rule.name] = rule
        return rules
