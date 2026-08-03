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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from noti_mapper.clock import Clock, SystemClock
from noti_mapper.config import (
    Configuration,
    ConfigurationError,
    InstanceConfig,
    load_configuration,
)
from noti_mapper.discovery import DiscoveryResult, known_plugins
from noti_mapper.dispatcher import OutputDispatcher
from noti_mapper.engine import Engine, PluginSet
from noti_mapper.messages import PushResultMessage
from noti_mapper.plugin import InputPlugin, OutputPlugin, PluginContext
from noti_mapper.sdnotify import Notifier
from noti_mapper.secrets import SecretsError, load_secrets
from noti_mapper.storage import (
    Database,
    InstanceRecord,
    PluginKeyValueStore,
    RuleRecord,
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

    # -- configuration --------------------------------------------------------

    def _load_configuration(self) -> Configuration:
        discovery = self._discovery
        assert discovery is not None

        try:
            secrets = load_secrets(self._paths.secrets_path)
        except SecretsError as error:
            raise StartupError(str(error)) from error

        try:
            return load_configuration(
                config_directory=self._paths.config_directory,
                secrets=secrets,
                known_plugins=known_plugins(discovery),
            )
        except ConfigurationError as error:
            raise StartupError(str(error)) from error

    def _mirror_configuration(self, *, store: Store, configuration: Configuration) -> None:
        store.sync_instances(
            [
                InstanceRecord(
                    name=instance.name,
                    plugin=instance.plugin,
                    enabled=instance.enabled,
                    orphaned=False,
                )
                for instance in configuration.instances.values()
            ]
        )
        orphaned, readopted = store.sync_rules(
            [
                RuleRecord(
                    name=rule.name,
                    enabled=rule.enabled,
                    orphaned=False,
                    inputs=rule.inputs,
                    outputs=rule.outputs,
                )
                for rule in configuration.rules.values()
            ]
        )
        for name in orphaned:
            self._log.warning(
                "rule %r is no longer in configuration; its latch is orphaned and no "
                "longer contributes to any output. 'noti-mapper purge' clears it.",
                name,
                extra={"rule": name},
            )
        for name in readopted:
            self._log.info(
                "rule %r has returned and re-adopted its latch", name, extra={"rule": name}
            )

    # -- plugin instances -----------------------------------------------------

    def _build_instances(
        self, *, configuration: Configuration, database: Database
    ) -> dict[str, _LiveInstance]:
        discovery = self._discovery
        engine = self._require_engine()
        assert discovery is not None

        built: dict[str, _LiveInstance] = {}
        for configured in configuration.enabled_instances():
            loaded = discovery.plugins.get(configured.plugin)
            if loaded is None:
                # Configuration validation already rejects unknown plugins, so
                # reaching here means the plugin set changed underneath us.
                raise StartupError(
                    f"instance {configured.name!r} needs plugin {configured.plugin!r}, "
                    "which is not loaded"
                )

            context = PluginContext(
                instance_name=configured.name,
                settings=configured.settings,
                storage=PluginKeyValueStore(database=database, instance_name=configured.name),
                clock=self._clock,
                logger=self._log.getChild(f"plugin.{configured.name}"),
            )
            live = _LiveInstance(
                name=configured.name,
                plugin_name=configured.plugin,
                settings=dict(configured.settings),
            )
            if loaded.input_class is not None:
                live.input_plugin = loaded.input_class(
                    context=context, emit=engine.emit_callback(configured.name)
                )
            if loaded.output_class is not None:
                live.output_plugin = loaded.output_class(
                    context=context, request_unlatch=engine.unlatch_callback(configured.name)
                )
            built[configured.name] = live

        return built

    def _start_instances(self, instances: Iterable[_LiveInstance]) -> None:
        for live in list(instances):
            if live.output_plugin is not None:
                try:
                    live.output_plugin.start()
                except Exception as error:
                    self._log.error(
                        "output %s failed to start: %s",
                        live.name,
                        error,
                        extra={"instance": live.name},
                        exc_info=True,
                    )
            if live.input_plugin is not None:
                thread = threading.Thread(
                    target=self._run_input,
                    args=(live,),
                    name=f"noti-input-{live.name}",
                    daemon=True,
                )
                live.thread = thread
                thread.start()

    def _run_input(self, live: _LiveInstance) -> None:
        plugin = live.input_plugin
        assert plugin is not None
        try:
            plugin.start()
        except Exception as error:
            self._log.error(
                "input %s stopped with an error: %s",
                live.name,
                error,
                extra={"instance": live.name},
                exc_info=True,
            )
        finally:
            self._close_thread_database()

    def _stop_instances(self, instances: list[_LiveInstance]) -> None:
        for live in instances:
            if live.input_plugin is not None:
                try:
                    live.input_plugin.stop()
                except Exception as error:
                    self._log.warning(
                        "input %s did not stop cleanly: %s",
                        live.name,
                        error,
                        extra={"instance": live.name},
                    )
            if live.output_plugin is not None:
                try:
                    live.output_plugin.stop()
                except Exception as error:
                    self._log.warning(
                        "output %s did not stop cleanly: %s",
                        live.name,
                        error,
                        extra={"instance": live.name},
                    )
        for live in instances:
            if live.thread is not None:
                live.thread.join(timeout=PLUGIN_STOP_TIMEOUT_SECONDS)
                if live.thread.is_alive():
                    self._log.warning(
                        "input %s did not return from start() within %.0fs",
                        live.name,
                        PLUGIN_STOP_TIMEOUT_SECONDS,
                        extra={"instance": live.name},
                    )

    def _close_thread_database(self) -> None:
        """Release this thread's SQLite connection on the way out.

        sqlite3 refuses cross-thread use, including close(), so every thread
        that touches the database has to hand its own connection back.
        """
        if self._database is not None:
            self._database.close()

    # -- odds and ends --------------------------------------------------------

    def _report_push_result(self, message: PushResultMessage) -> None:
        engine = self._engine
        if engine is None:
            return
        engine.submit(message)

    def _status_line(self) -> str:
        configuration = self._configuration
        if configuration is None:
            return "starting"
        latched = 0
        engine = self._engine
        if engine is not None:
            latched = sum(1 for state in engine.latch_states().values() if state)
        return (
            f"{len(configuration.rules)} rules, "
            f"{len(configuration.instances)} instances, {latched} latched"
        )

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
