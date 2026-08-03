"""Plugin discovery: a filesystem scan, and nothing else.

On startup the core scans, in order:

1. the in-tree ``plugins/`` directory, when running from a source checkout
2. ``/usr/lib/noti-mapper/plugins/`` for packaged plugins
3. ``/etc/noti-mapper/plugins/`` for locally-authored plugins

Each plugin directory is a Python module exposing well-known attributes:
``PLUGIN_NAME``, and ``INPUT_PLUGIN`` and/or ``OUTPUT_PLUGIN`` class
references.

Filesystem scan only. No Python entry points, no pip-installed plugins, no
discovery mechanism that depends on the Python packaging ecosystem. A plugin is
a directory in a known location. That is the whole contract, and it is
inspectable with ``ls``.

Import failures are logged with the traceback and skipped -- one broken
third-party plugin must not prevent startup. A configuration file referencing a
plugin that failed to load is, however, a fatal configuration error, and
:func:`known_plugins` feeding only successfully-loaded plugins to the config
validator is what produces that.
"""

import importlib.util
import inspect
import logging
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from noti_mapper.config import KnownPlugin, PluginDirection
from noti_mapper.plugin import InputPlugin, OutputPlugin

PACKAGED_PLUGIN_DIRECTORY: Path = Path("/usr/lib/noti-mapper/plugins")
LOCAL_PLUGIN_DIRECTORY: Path = Path("/etc/noti-mapper/plugins")

PLUGIN_NAME_ATTRIBUTE = "PLUGIN_NAME"
INPUT_PLUGIN_ATTRIBUTE = "INPUT_PLUGIN"
OUTPUT_PLUGIN_ATTRIBUTE = "OUTPUT_PLUGIN"

_MODULE_PREFIX = "noti_mapper_plugin_"


@dataclass(frozen=True)
class LoadedPlugin:
    """A plugin directory that imported cleanly and looks like a plugin."""

    plugin_name: str
    directory: Path
    input_class: type[InputPlugin] | None
    output_class: type[OutputPlugin] | None

    def directions(self) -> frozenset[PluginDirection]:
        found: set[PluginDirection] = set()
        if self.input_class is not None:
            found.add(PluginDirection.INPUT)
        if self.output_class is not None:
            found.add(PluginDirection.OUTPUT)
        return frozenset(found)


@dataclass(frozen=True)
class PluginLoadFailure:
    """A plugin directory that could not be used, and why."""

    directory: Path
    message: str
    traceback_text: str = ""


@dataclass(frozen=True)
class DiscoveryResult:
    """Everything the scan found."""

    plugins: Mapping[str, LoadedPlugin]
    failures: tuple[PluginLoadFailure, ...]

    def names(self) -> list[str]:
        return sorted(self.plugins)


def source_checkout_plugin_directory() -> Path | None:
    """Return the in-tree ``plugins/`` directory when running from a checkout.

    ``src/noti_mapper/discovery.py`` sits three levels below the repository
    root. The pyproject.toml check is what distinguishes a checkout from an
    installed tree that happens to have the same shape.
    """
    repository_root = Path(__file__).resolve().parents[2]
    if not (repository_root / "pyproject.toml").is_file():
        return None
    candidate = repository_root / "plugins"
    if not candidate.is_dir():
        return None
    return candidate


def default_search_path() -> list[Path]:
    """The directories scanned for plugins, in order."""
    directories: list[Path] = []
    in_tree = source_checkout_plugin_directory()
    if in_tree is not None:
        directories.append(in_tree)
    directories.append(PACKAGED_PLUGIN_DIRECTORY)
    directories.append(LOCAL_PLUGIN_DIRECTORY)
    return directories


def discover(
    *, search_path: Sequence[Path], logger: logging.Logger | None = None
) -> DiscoveryResult:
    """Scan for plugins. Never raises; problems come back as failures."""
    log = logger if logger is not None else logging.getLogger(__name__)
    plugins: dict[str, LoadedPlugin] = {}
    failures: list[PluginLoadFailure] = []

    for directory in search_path:
        if not directory.is_dir():
            log.debug("plugin directory %s does not exist, skipping", directory)
            continue
        for candidate in sorted(directory.iterdir()):
            if not _looks_like_a_plugin(candidate):
                continue
            outcome = _load_one(directory=candidate, log=log)
            if isinstance(outcome, PluginLoadFailure):
                failures.append(outcome)
                log.error(
                    "plugin at %s failed to load: %s",
                    outcome.directory,
                    outcome.message,
                    extra={"plugin_directory": str(outcome.directory)},
                )
                if outcome.traceback_text:
                    log.error("%s", outcome.traceback_text)
                continue

            existing = plugins.get(outcome.plugin_name)
            if existing is not None:
                failure = PluginLoadFailure(
                    directory=outcome.directory,
                    message=(
                        f"plugin name {outcome.plugin_name!r} is already provided by "
                        f"{existing.directory}; the first one found wins"
                    ),
                )
                failures.append(failure)
                log.error("%s", failure.message)
                continue

            plugins[outcome.plugin_name] = outcome
            log.info(
                "loaded plugin %s from %s",
                outcome.plugin_name,
                outcome.directory,
                extra={"plugin": outcome.plugin_name},
            )

    return DiscoveryResult(plugins=plugins, failures=tuple(failures))


def known_plugins(result: DiscoveryResult) -> dict[str, KnownPlugin]:
    """Convert a discovery result into what configuration validation needs."""
    known: dict[str, KnownPlugin] = {}
    for name, plugin in result.plugins.items():
        known[name] = KnownPlugin(
            plugin_name=name,
            directions=plugin.directions(),
            validate_settings=_settings_validator(plugin),
        )
    return known


def _settings_validator(plugin: LoadedPlugin) -> Callable[[Mapping[str, object]], list[str]]:
    """Build the callable configuration validation uses for one plugin.

    A plugin providing both directions gets both validators run, with the
    problems concatenated. That is rare, but silently running only one of them
    would be worse than the small cost of handling it.
    """

    def validate(settings: Mapping[str, object]) -> list[str]:
        problems: list[str] = []
        if plugin.input_class is not None:
            problems.extend(plugin.input_class.validate_settings(settings))
        if plugin.output_class is not None:
            for problem in plugin.output_class.validate_settings(settings):
                if problem not in problems:
                    problems.append(problem)
        return problems

    return validate


def _looks_like_a_plugin(candidate: Path) -> bool:
    if not candidate.is_dir():
        return False
    if candidate.name.startswith((".", "_")):
        return False
    return (candidate / "__init__.py").is_file()


def _load_one(*, directory: Path, log: logging.Logger) -> LoadedPlugin | PluginLoadFailure:
    module_name = _MODULE_PREFIX + directory.name.replace("-", "_")
    init_file = directory / "__init__.py"

    try:
        spec = importlib.util.spec_from_file_location(
            module_name, init_file, submodule_search_locations=[str(directory)]
        )
        if spec is None or spec.loader is None:
            return PluginLoadFailure(
                directory=directory, message="Python could not build an import spec for it"
            )
        module = importlib.util.module_from_spec(spec)
        # Registered before execution so that a plugin split across several
        # files can import its own submodules.
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except BaseException as error:  # noqa: BLE001 - one bad plugin must not stop startup
        sys.modules.pop(module_name, None)
        return PluginLoadFailure(
            directory=directory,
            message=f"{type(error).__name__}: {error}",
            traceback_text=traceback.format_exc(),
        )

    log.debug("imported %s from %s", module_name, init_file)
    return _inspect_module(module=module, directory=directory)


def _inspect_module(*, module: object, directory: Path) -> LoadedPlugin | PluginLoadFailure:
    plugin_name = getattr(module, PLUGIN_NAME_ATTRIBUTE, None)
    if plugin_name is None:
        return PluginLoadFailure(
            directory=directory, message=f"it does not define {PLUGIN_NAME_ATTRIBUTE}"
        )
    if not isinstance(plugin_name, str) or not plugin_name.strip():
        return PluginLoadFailure(
            directory=directory, message=f"{PLUGIN_NAME_ATTRIBUTE} must be a non-empty string"
        )

    input_class = getattr(module, INPUT_PLUGIN_ATTRIBUTE, None)
    output_class = getattr(module, OUTPUT_PLUGIN_ATTRIBUTE, None)

    if input_class is None and output_class is None:
        return PluginLoadFailure(
            directory=directory,
            message=(
                f"it defines neither {INPUT_PLUGIN_ATTRIBUTE} nor "
                f"{OUTPUT_PLUGIN_ATTRIBUTE}, so it cannot do anything"
            ),
        )

    if input_class is not None:
        problem = _check_class(
            candidate=input_class, base=InputPlugin, attribute=INPUT_PLUGIN_ATTRIBUTE
        )
        if problem is not None:
            return PluginLoadFailure(directory=directory, message=problem)

    if output_class is not None:
        problem = _check_class(
            candidate=output_class, base=OutputPlugin, attribute=OUTPUT_PLUGIN_ATTRIBUTE
        )
        if problem is not None:
            return PluginLoadFailure(directory=directory, message=problem)

    return LoadedPlugin(
        plugin_name=plugin_name.strip(),
        directory=directory,
        input_class=input_class,
        output_class=output_class,
    )


def _check_class(*, candidate: object, base: type, attribute: str) -> str | None:
    if not inspect.isclass(candidate):
        return f"{attribute} is not a class"
    if not issubclass(candidate, base):
        return f"{attribute} does not subclass {base.__name__}"
    if inspect.isabstract(candidate):
        missing = ", ".join(sorted(getattr(candidate, "__abstractmethods__", frozenset())))
        return f"{attribute} does not implement: {missing}"
    return None
