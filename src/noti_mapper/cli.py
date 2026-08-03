"""The ``noti-mapper`` command.

Subcommands:

* ``run`` -- the daemon. This is what the systemd unit invokes.
* ``validate`` -- load and check the configuration and exit. The unit gates on
  it, and a user can check before restarting.
* ``status`` -- current latches, orphans, output sync state, plugin health, and
  pending retries. The first thing anyone asks for when it misbehaves.
* ``rename`` -- migrate a rule's latch to a new name. Renaming a rule in
  configuration without this loses the latch, because the latch is keyed on the
  rule name.
* ``purge`` -- delete orphaned latches and the stored state of removed
  instances.

Names containing spaces need quoting, which is worth remembering when typing
these by hand: ``noti-mapper rename "Package On Porch" "Porch Package"``.
"""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from noti_mapper import VERSION
from noti_mapper.clock import SystemClock
from noti_mapper.config import (
    DEFAULT_CONFIG_DIRECTORY,
    ConfigurationError,
    load_configuration,
)
from noti_mapper.discovery import default_search_path, discover, known_plugins
from noti_mapper.logging_setup import configure as configure_logging
from noti_mapper.logging_setup import level_from_name
from noti_mapper.runtime import Daemon, Paths, StartupError
from noti_mapper.secrets import DEFAULT_SECRETS_PATH, SecretsError, load_secrets
from noti_mapper.storage import (
    DEFAULT_STATE_DIRECTORY,
    StorageError,
)

STATE_DIRECTORY_ENVIRONMENT = "STATE_DIRECTORY"
CONFIGURATION_DIRECTORY_ENVIRONMENT = "CONFIGURATION_DIRECTORY"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit status."""
    parser = build_parser()
    arguments = parser.parse_args(argv)

    try:
        configure_logging(level=level_from_name(arguments.log_level))
    except ValueError as error:
        print(f"noti-mapper: {error}", file=sys.stderr)
        return EXIT_FAILURE

    paths = _paths_from(arguments)

    if arguments.command == "run":
        return _run(paths)
    if arguments.command == "validate":
        return _validate(paths)
    if arguments.command == "status":
        return _status(paths)
    if arguments.command == "rename":
        return _rename(paths, old_name=arguments.old_name, new_name=arguments.new_name)
    if arguments.command == "purge":
        return _purge(paths, assume_yes=arguments.yes)
    if arguments.command == "version":
        print(f"noti-mapper {VERSION}")
        return EXIT_OK

    parser.print_help()
    return EXIT_FAILURE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noti-mapper",
        description="Latch notification events onto outputs until acknowledged.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help=f"directory of *.json configuration files (default: {DEFAULT_CONFIG_DIRECTORY})",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        default=None,
        help=f"path to the secrets file (default: {DEFAULT_SECRETS_PATH})",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help=f"directory holding state.db (default: {DEFAULT_STATE_DIRECTORY})",
    )
    parser.add_argument(
        "--plugin-dir",
        type=Path,
        action="append",
        default=None,
        dest="plugin_directories",
        help="override the plugin search path; may be given more than once",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        help="debug, info, warning, error, or critical (default: info)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="run the daemon")
    subparsers.add_parser("validate", help="check the configuration and exit")
    subparsers.add_parser("status", help="print current state")
    subparsers.add_parser("version", help="print the version")

    rename = subparsers.add_parser(
        "rename",
        help="migrate a rule's latch to a new name",
        description=(
            "Renaming a rule in configuration is otherwise indistinguishable from "
            "deleting one rule and creating another: the old latch orphans and the "
            "new rule starts cleared. Run this while the daemon is stopped, then "
            "edit the configuration to match."
        ),
    )
    rename.add_argument("old_name", help="the rule's current name")
    rename.add_argument("new_name", help="the rule's new name")

    purge = subparsers.add_parser("purge", help="delete orphaned latches and instance state")
    purge.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    return parser


def _paths_from(arguments: argparse.Namespace) -> Paths:
    config_directory = arguments.config_dir
    if config_directory is None:
        from_environment = os.environ.get(CONFIGURATION_DIRECTORY_ENVIRONMENT)
        config_directory = Path(from_environment) if from_environment else DEFAULT_CONFIG_DIRECTORY

    state_directory = arguments.state_dir
    if state_directory is None:
        # systemd's StateDirectory= exports this, which is the whole reason the
        # unit uses it rather than a tmpfiles rule.
        from_environment = os.environ.get(STATE_DIRECTORY_ENVIRONMENT)
        state_directory = Path(from_environment) if from_environment else DEFAULT_STATE_DIRECTORY

    plugin_directories = arguments.plugin_directories
    if plugin_directories is None:
        plugin_directories = default_search_path()

    return Paths(
        config_directory=Path(config_directory),
        secrets_path=Path(arguments.secrets) if arguments.secrets else DEFAULT_SECRETS_PATH,
        state_directory=Path(state_directory),
        plugin_directories=tuple(plugin_directories),
    )


# -- run ----------------------------------------------------------------------


def _run(paths: Paths) -> int:
    log = logging.getLogger("noti_mapper")
    daemon = Daemon(paths=paths, clock=SystemClock(), logger=log)
    try:
        daemon.start()
    except StartupError as error:
        log.error("%s", error)
        return EXIT_CONFIG_ERROR
    except StorageError as error:
        log.error("%s", error)
        return EXIT_FAILURE

    try:
        daemon.run()
    finally:
        daemon.stop()
    return EXIT_OK


# -- validate -----------------------------------------------------------------


def _validate(paths: Paths) -> int:
    discovery = discover(search_path=list(paths.plugin_directories), logger=logging.getLogger())
    for failure in discovery.failures:
        print(f"warning: plugin at {failure.directory}: {failure.message}", file=sys.stderr)

    try:
        secrets = load_secrets(paths.secrets_path)
    except SecretsError as error:
        print(f"noti-mapper: {error}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        configuration = load_configuration(
            config_directory=paths.config_directory,
            secrets=secrets,
            known_plugins=known_plugins(discovery),
        )
    except ConfigurationError as error:
        print(f"noti-mapper: {error}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    print(
        f"configuration is valid: {len(configuration.instances)} instances, "
        f"{len(configuration.rules)} rules, from "
        f"{len(configuration.files)} file(s)"
    )
    return EXIT_OK
