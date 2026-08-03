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

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from noti_mapper.config import PluginDirection
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
