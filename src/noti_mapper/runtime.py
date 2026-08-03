"""Assembling the daemon: plugins, threads, signals, and the systemd handshake.

This is the wiring. The interesting decisions live in :mod:`noti_mapper.engine`
and :mod:`noti_mapper.reconcile`; what happens here is that the pieces are put
together in the right order and taken apart again cleanly.

Order on startup matters and is the reason ``Type=notify`` is worth having:

1. discover plugins
2. load and validate configuration -- a failure here is fatal, before anything
   has been started
3. open the database and mirror the configuration into it
4. construct plugin instances
5. reconcile
6. only then signal READY=1

A daemon that reports ready before it knows what its latches are is lying, and
the whole point of this project is that the state is trustworthy.
"""

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from noti_mapper.clock import Clock, SystemClock
from noti_mapper.config import (
    Configuration,
    InstanceConfig,
)
from noti_mapper.discovery import DiscoveryResult
from noti_mapper.dispatcher import OutputDispatcher
from noti_mapper.engine import Engine, PluginSet
from noti_mapper.plugin import InputPlugin, OutputPlugin
from noti_mapper.sdnotify import Notifier
from noti_mapper.storage import (
    Database,
    Store,
)

# How long an input instance may sit in a FAILED state before the watchdog
# stops pinging and lets systemd restart the daemon. Prolonged absence of
# successful input-plugin activity is unhealthy, not idle: a live process with
# a dead IMAP socket is precisely the silent failure this guards against.
UNHEALTHY_GRACE_SECONDS: float = 300.0

# If the core loop has not completed a tick in this long, it is wedged.
HEARTBEAT_GRACE_SECONDS: float = 60.0

RELOAD_ACKNOWLEDGE_TIMEOUT_SECONDS: float = 30.0
PLUGIN_STOP_TIMEOUT_SECONDS: float = 15.0


class StartupError(Exception):
    """The daemon cannot start. The message is meant for a human reading the journal."""


@dataclass
class _LiveInstance:
    """One running plugin instance, and what it was built from."""

    name: str
    plugin_name: str
    settings: Mapping[str, object]
    input_plugin: InputPlugin | None = None
    output_plugin: OutputPlugin | None = None
    thread: threading.Thread | None = None

    def matches(self, configured: InstanceConfig) -> bool:
        return self.plugin_name == configured.plugin and self.settings == configured.settings


@dataclass(frozen=True)
class Paths:
    """Where everything lives. Overridable so tests never touch /etc or /var."""

    config_directory: Path
    secrets_path: Path
    state_directory: Path
    plugin_directories: tuple[Path, ...]


class Daemon:
    """The whole running service."""

    def __init__(
        self,
        *,
        paths: Paths,
        clock: Clock | None = None,
        notifier: Notifier | None = None,
        logger: logging.Logger | None = None,
        handle_signals: bool = True,
    ) -> None:
        self._paths = paths
        # Signal handlers can only be installed from the main thread, and a
        # caller that already owns signal handling should keep owning it.
        self._handle_signals = handle_signals
        self._clock = clock if clock is not None else SystemClock()
        self._notifier = notifier if notifier is not None else Notifier()
        self._log = logger if logger is not None else logging.getLogger("noti_mapper")

        self._database: Database | None = None
        self._store: Store | None = None
        self._engine: Engine | None = None
        self._dispatcher: OutputDispatcher | None = None
        self._discovery: DiscoveryResult | None = None
        self._configuration: Configuration | None = None
        self._live: dict[str, _LiveInstance] = {}

        self._reload_requested = threading.Event()
        self._shutting_down = threading.Event()
        self._reload_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None

    def _close_thread_database(self) -> None:
        """Release this thread's SQLite connection on the way out.

        sqlite3 refuses cross-thread use, including close(), so every thread
        that touches the database has to hand its own connection back.
        """
        if self._database is not None:
            self._database.close()

    def _require_engine(self) -> Engine:
        engine = self._engine
        if engine is None:
            raise StartupError("the engine has not been constructed yet")
        return engine

    def _require_store(self) -> Store:
        store = self._store
        if store is None:
            raise StartupError("the database has not been opened yet")
        return store

    def _require_database(self) -> Database:
        database = self._database
        if database is None:
            raise StartupError("the database has not been opened yet")
        return database


def _plugin_set(live: Mapping[str, _LiveInstance]) -> PluginSet:
    plugins = PluginSet.empty()
    for name, instance in live.items():
        if instance.input_plugin is not None:
            plugins.inputs[name] = instance.input_plugin
        if instance.output_plugin is not None:
            plugins.outputs[name] = instance.output_plugin
    return plugins
