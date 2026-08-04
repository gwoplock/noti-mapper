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

from noti_mapper.jsonfile import JsonFileError, read_object
from noti_mapper.names import (
    NameRegistry,
    Namespace,
)
from noti_mapper.secrets import SecretStore

DEFAULT_CONFIG_DIRECTORY: Path = Path("/etc/noti-mapper.d")

_TOP_LEVEL_KEYS = ("daemon", "instances", "rules")
_INSTANCE_KEYS = ("plugin", "config", "enabled")
_RULE_KEYS = ("inputs", "outputs", "enabled")
_DAEMON_KEYS = (
    "event_log_max_rows",
    "dispatcher_threads",
    "retry_initial_seconds",
    "retry_max_seconds",
)


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
class DaemonSettings:
    """Daemon-wide knobs, from the optional top-level ``daemon`` object.

    Every one of these has a defensible default; the block exists so that an
    installation with unusual needs does not have to patch the source.
    """

    # event_log is a log, not an audit trail: it rolls, oldest rows first. No
    # code path may depend on it being complete.
    event_log_max_rows: int = 10_000
    dispatcher_threads: int = 4
    retry_initial_seconds: float = 5.0
    retry_max_seconds: float = 900.0


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


@dataclass(frozen=True)
class ConfigError:
    """One problem with the configuration, and where in the tree it is."""

    message: str
    where: ConfigPath | None = None

    def __str__(self) -> str:
        if self.where is None:
            return self.message
        return f"{self.where}:\n      {self.message}"


class ConfigurationError(Exception):
    """The configuration is unusable. Carries every problem found, not just the first."""

    def __init__(self, errors: list[ConfigError]) -> None:
        self.errors = _sorted_errors(errors)
        joined = "\n".join(f"  {error}" for error in self.errors)
        count = len(self.errors)
        noun = "error" if count == 1 else "errors"
        super().__init__(f"{count} configuration {noun}:\n{joined}")


def _sorted_errors(errors: list[ConfigError]) -> list[ConfigError]:
    def key(error: ConfigError) -> tuple[int, str, tuple[str, ...]]:
        if error.where is None:
            return (1, "", ())
        return (0, str(error.where.file), error.where.steps)

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
class _Draft:
    """A named object collected from a file, not yet checked."""

    name: str
    body: Mapping[str, object]
    origin: ConfigPath


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
        self._instance_drafts: list[_Draft] = []
        self._rule_drafts: list[_Draft] = []
        # Instances that were declared but failed to build. A rule referencing
        # one of these should not also be told the instance does not exist --
        # that turns one mistake into two errors and hides the real one.
        self._broken_instances: set[str] = set()
        self._daemon_body: Mapping[str, object] | None = None
        self._daemon_origin: ConfigPath | None = None

    def load(self) -> Configuration:
        files = self._read_files()
        for path in files:
            document = self._parse(path)
            if document is not None:
                self._collect_document(document=document, path=path)

        daemon = self._build_daemon()
        instances = self._build_instances()
        rules = self._build_rules(instances)

        if self._errors:
            raise ConfigurationError(self._errors)

        return Configuration(instances=instances, rules=rules, files=tuple(files), daemon=daemon)

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

    def _parse(self, path: Path) -> Mapping[str, object] | None:
        try:
            return read_object(path)
        except JsonFileError as error:
            self._errors.append(ConfigError(error.detail, ConfigPath(file=path)))
            return None

    def _collect_document(self, *, document: Mapping[str, object], path: Path) -> None:
        root = ConfigPath(file=path)
        for key, value in document.items():
            where = root.key(key)
            if key not in _TOP_LEVEL_KEYS:
                self._errors.append(
                    ConfigError(
                        f"unknown top-level key {key!r}; expected one of "
                        f"{', '.join(repr(name) for name in _TOP_LEVEL_KEYS)}",
                        where,
                    )
                )
                continue
            if not isinstance(value, dict):
                self._errors.append(ConfigError(f"{key!r} must be an object", where))
                continue
            if key == "daemon":
                self._collect_daemon(body=value, where=where)
            elif key == "instances":
                self._collect_named(container=value, namespace=Namespace.INSTANCE, where=where)
            else:
                self._collect_named(container=value, namespace=Namespace.RULE, where=where)

    def _collect_daemon(self, *, body: Mapping[str, object], where: ConfigPath) -> None:
        if self._daemon_origin is not None:
            self._errors.append(
                ConfigError(
                    'a second "daemon" block; it is already defined in '
                    f"{self._daemon_origin.file}. Daemon-wide settings live in "
                    "exactly one file.",
                    where,
                )
            )
            return
        self._daemon_body = body
        self._daemon_origin = where

    # -- daemon-wide settings -------------------------------------------------

    def _build_daemon(self) -> DaemonSettings:
        body = self._daemon_body
        where = self._daemon_origin
        if body is None or where is None:
            return DaemonSettings()

        self._reject_unknown_keys(body=body, where=where, allowed=_DAEMON_KEYS, subject='"daemon"')
        defaults = DaemonSettings()
        return DaemonSettings(
            event_log_max_rows=self._positive_int(
                body=body,
                where=where,
                key="event_log_max_rows",
                default=defaults.event_log_max_rows,
            ),
            dispatcher_threads=self._positive_int(
                body=body,
                where=where,
                key="dispatcher_threads",
                default=defaults.dispatcher_threads,
            ),
            retry_initial_seconds=self._positive_number(
                body=body,
                where=where,
                key="retry_initial_seconds",
                default=defaults.retry_initial_seconds,
            ),
            retry_max_seconds=self._positive_number(
                body=body,
                where=where,
                key="retry_max_seconds",
                default=defaults.retry_max_seconds,
            ),
        )

    def _positive_int(
        self, *, body: Mapping[str, object], where: ConfigPath, key: str, default: int
    ) -> int:
        if key not in body:
            return default
        value = body[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            self._errors.append(
                ConfigError(f'"daemon": "{key}" must be a positive integer', where.key(key))
            )
            return default
        return value

    def _positive_number(
        self, *, body: Mapping[str, object], where: ConfigPath, key: str, default: float
    ) -> float:
        if key not in body:
            return default
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            self._errors.append(
                ConfigError(f'"daemon": "{key}" must be a positive number', where.key(key))
            )
            return default
        return float(value)

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
