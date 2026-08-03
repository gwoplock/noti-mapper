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
import datetime
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from noti_mapper import VERSION
from noti_mapper.clock import SystemClock
from noti_mapper.config import (
    DEFAULT_CONFIG_DIRECTORY,
    Configuration,
    ConfigurationError,
    load_configuration,
)
from noti_mapper.discovery import default_search_path, discover, known_plugins
from noti_mapper.logging_setup import configure as configure_logging
from noti_mapper.logging_setup import level_from_name
from noti_mapper.names import InvalidNameError, validate_name
from noti_mapper.rules import RuleGraph
from noti_mapper.runtime import Daemon, Paths, StartupError
from noti_mapper.secrets import DEFAULT_SECRETS_PATH, SecretsError, empty_store, load_secrets
from noti_mapper.storage import (
    DEFAULT_STATE_DIRECTORY,
    Database,
    EventKind,
    StorageError,
    Store,
    database_path,
    initialize,
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


SUBCOMMANDS: tuple[str, ...] = (
    "run",
    "validate",
    "status",
    "rename",
    "purge",
    "version",
)


def subcommand_names(parser: argparse.ArgumentParser) -> list[str]:
    """The subcommands a parser actually accepts.

    argparse exposes this only through a private attribute. Reaching for it
    once, here, is better than every caller doing it, and it lets the man page
    and the systemd unit be checked against the real parser rather than
    against a list that can quietly drift away from it.
    """
    group = parser._subparsers  # noqa: SLF001
    if group is None:
        return []
    names: list[str] = []
    for action in group._group_actions:  # noqa: SLF001
        choices = getattr(action, "choices", None)
        if choices is None:
            continue
        names.extend(str(choice) for choice in choices)
    return sorted(names)


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


# -- status -------------------------------------------------------------------


def _status(paths: Paths) -> int:
    path = database_path(paths.state_directory)
    if not path.exists():
        print(f"noti-mapper: no state database at {path}; has the daemon ever run?")
        return EXIT_FAILURE

    database = Database(path=path)
    try:
        initialize(database)
    except StorageError as error:
        print(f"noti-mapper: {error}", file=sys.stderr)
        database.close()
        return EXIT_FAILURE

    store = Store(database=database)
    try:
        configuration = _configuration_or_none(paths)
        _print_status(store=store, configuration=configuration, now=SystemClock().now())
    finally:
        database.close()
    return EXIT_OK


def _configuration_or_none(paths: Paths) -> Configuration | None:
    discovery = discover(search_path=list(paths.plugin_directories), logger=logging.getLogger())
    try:
        secrets = load_secrets(paths.secrets_path)
    except SecretsError:
        secrets = empty_store(paths.secrets_path)
    try:
        return load_configuration(
            config_directory=paths.config_directory,
            secrets=secrets,
            known_plugins=known_plugins(discovery),
        )
    except ConfigurationError:
        return None


def _print_status(
    *, store: Store, configuration: Configuration | None, now: datetime.datetime
) -> None:
    graph = None if configuration is None else RuleGraph.from_configuration(configuration)
    if configuration is None:
        print(
            "warning: the configuration does not currently load, so desired output "
            "state cannot be computed. Run 'noti-mapper validate' for the errors.\n"
        )

    rules = {record.name: record for record in store.rules()}
    latches = store.latches()

    print("Latches")
    if not latches:
        print("  (none)")
    for latch in latches:
        rule = rules.get(latch.rule_name)
        orphaned = " [orphaned]" if rule is not None and rule.orphaned else ""
        disabled = " [disabled]" if rule is not None and not rule.enabled else ""
        state = "SET " if latch.state else "clear"
        when = latch.set_at if latch.state else latch.cleared_at
        stamp = "never" if when is None else _ago(when, now)
        print(
            f"  {state}  {latch.rule_name!r}{orphaned}{disabled}  "
            f"triggers={latch.trigger_count}  {stamp}  cause={latch.last_cause or '-'}"
        )

    orphans = [record.name for record in store.rules() if record.orphaned]
    if orphans:
        print("\nOrphaned rules (no longer in configuration; 'noti-mapper purge' clears them)")
        for name in orphans:
            print(f"  {name!r}")

    print("\nOutputs")
    states = {record.instance_name: record for record in store.output_states()}
    output_names = sorted(states) if graph is None else graph.output_names()
    if not output_names:
        print("  (none)")
    latch_states = {latch.rule_name: latch.state for latch in latches}
    for name in output_names:
        record = states.get(name)
        applied = (
            "unknown" if record is None or record.last_applied is None else str(record.last_applied)
        )
        synced = (
            "never"
            if record is None or record.last_sync_at is None
            else _ago(record.last_sync_at, now)
        )
        if graph is None:
            desired = "?"
        else:
            desired = str(graph.desired_output_state(instance_name=name, latches=latch_states))
        agreement = "" if desired in {applied, "?"} else "   <-- out of sync"
        print(f"  {name!r}  desired={desired}  applied={applied}  synced={synced}{agreement}")

    print("\nPending retries")
    pending = store.pending_pushes()
    if not pending:
        print("  (none)")
    for push in pending:
        print(
            f"  {push.instance_name!r}  target={push.target_value}  "
            f"attempts={push.attempt_count}  next={_ago(push.next_attempt_at, now)}  "
            f"error={push.last_error or '-'}"
        )

    print("\nPlugin health")
    health = store.health()
    if not health:
        print("  (none reported)")
    for report in health:
        detail = f"  {report.detail}" if report.detail else ""
        print(
            f"  {report.instance_name!r}  {report.status.value}  "
            f"({_ago(report.updated_at, now)}){detail}"
        )

    print("\nRecent events")
    events = store.recent_events(limit=10)
    if not events:
        print("  (none)")
    for entry in events:
        subject = entry.rule_name or entry.instance_name or "-"
        detail = f"  {entry.detail}" if entry.detail else ""
        print(f"  {_ago(entry.at, now)}  {entry.kind.value}  {subject}{detail}")


def _ago(moment: datetime.datetime, now: datetime.datetime) -> str:
    seconds = (now - moment).total_seconds()
    if seconds < 0:
        return f"in {_duration(-seconds)}"
    return f"{_duration(seconds)} ago"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# -- rename -------------------------------------------------------------------


def _rename(paths: Paths, *, old_name: str, new_name: str) -> int:
    try:
        old = validate_name(old_name)
        new = validate_name(new_name)
    except InvalidNameError as error:
        print(f"noti-mapper: {error}", file=sys.stderr)
        return EXIT_FAILURE

    path = database_path(paths.state_directory)
    if not path.exists():
        print(f"noti-mapper: no state database at {path}", file=sys.stderr)
        return EXIT_FAILURE

    database = Database(path=path)
    try:
        initialize(database)
        store = Store(database=database)
        store.rename_rule(old_name=old, new_name=new)
        store.append_event(
            at=SystemClock().now(),
            kind=EventKind.RENAMED,
            rule_name=new,
            detail=f"renamed from {old!r}",
        )
    except StorageError as error:
        print(f"noti-mapper: {error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        database.close()

    print(f"renamed rule {old!r} to {new!r}; its latch moved with it.")
    print("Update the configuration to match before starting the daemon.")
    return EXIT_OK


# -- purge --------------------------------------------------------------------


def _purge(paths: Paths, *, assume_yes: bool) -> int:
    path = database_path(paths.state_directory)
    if not path.exists():
        print(f"noti-mapper: no state database at {path}", file=sys.stderr)
        return EXIT_FAILURE

    database = Database(path=path)
    try:
        initialize(database)
        store = Store(database=database)

        orphan_rules = [record.name for record in store.rules() if record.orphaned]
        orphan_instances = [record.name for record in store.instances() if record.orphaned]
        if not orphan_rules and not orphan_instances:
            print("nothing to purge.")
            return EXIT_OK

        print("This will permanently delete:")
        for name in orphan_rules:
            print(f"  rule {name!r} and its latch")
        for name in orphan_instances:
            print(f"  instance {name!r} and its stored plugin state")

        if not assume_yes and not _confirm():
            print("cancelled.")
            return EXIT_OK

        purged_rules, purged_instances = store.purge_orphans()
        store.append_event(
            at=SystemClock().now(),
            kind=EventKind.PURGED,
            detail=f"{len(purged_rules)} rules, {len(purged_instances)} instances",
        )
        print(f"purged {len(purged_rules)} rule(s) and {len(purged_instances)} instance(s).")
    except StorageError as error:
        print(f"noti-mapper: {error}", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        database.close()
    return EXIT_OK


def _confirm() -> bool:
    if not sys.stdin.isatty():
        print("noti-mapper: not a terminal; re-run with --yes to confirm.", file=sys.stderr)
        return False
    answer = input("Proceed? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
