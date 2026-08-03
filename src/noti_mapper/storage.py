"""Persistent state, in SQLite.

The database lives at ``/var/lib/noti-mapper/state.db``. Per the FHS, ``/var``
itself holds only the standard subdirectories; persistent application-private
state belongs under ``/var/lib/<package>/``. The systemd unit creates the
directory with ``StateDirectory=noti-mapper``, which handles ownership and
permissions without a tmpfiles rule and exposes the path as ``$STATE_DIRECTORY``.

Two pragmas are deliberate:

* ``journal_mode=WAL`` -- one writer with occasional concurrent readers is
  exactly SQLite's sweet spot, and the ``status`` subcommand reads this file
  while the daemon is running.
* ``synchronous=FULL`` -- the whole product is "the latch survives". Losing the
  last transaction to a power cut is not an acceptable trade for throughput
  this daemon does not need.

Threading: the core thread is the only writer of latch state, and it does all
of its work through one connection. Plugins get their own connection through
:class:`PluginKeyValueStore`, which touches only the ``plugin_kv`` table --
disjoint from everything the core writes.
"""

import contextlib
import datetime
import enum
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from noti_mapper.clock import from_iso, to_iso

SCHEMA_VERSION: int = 1
DEFAULT_STATE_DIRECTORY: Path = Path("/var/lib/noti-mapper")
DATABASE_FILENAME: str = "state.db"

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE schema_version (version INTEGER NOT NULL)",
    """
    CREATE TABLE instances (
        name        TEXT PRIMARY KEY,
        plugin      TEXT NOT NULL,
        enabled     INTEGER NOT NULL,
        orphaned    INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE rules (
        name        TEXT PRIMARY KEY,
        enabled     INTEGER NOT NULL,
        orphaned    INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE rule_inputs (
        rule_name       TEXT NOT NULL REFERENCES rules(name) ON DELETE CASCADE,
        instance_name   TEXT NOT NULL,
        PRIMARY KEY (rule_name, instance_name)
    )
    """,
    """
    CREATE TABLE rule_outputs (
        rule_name       TEXT NOT NULL REFERENCES rules(name) ON DELETE CASCADE,
        instance_name   TEXT NOT NULL,
        PRIMARY KEY (rule_name, instance_name)
    )
    """,
    """
    CREATE TABLE latches (
        rule_name       TEXT PRIMARY KEY REFERENCES rules(name) ON DELETE CASCADE,
        state           INTEGER NOT NULL,
        set_at          TEXT,
        cleared_at      TEXT,
        trigger_count   INTEGER NOT NULL DEFAULT 0,
        last_cause      TEXT
    )
    """,
    """
    CREATE TABLE output_state (
        instance_name   TEXT PRIMARY KEY,
        last_applied    INTEGER,
        last_confirmed  INTEGER,
        last_sync_at    TEXT
    )
    """,
    """
    CREATE TABLE plugin_kv (
        instance_name   TEXT NOT NULL,
        key             TEXT NOT NULL,
        value           TEXT NOT NULL,
        PRIMARY KEY (instance_name, key)
    )
    """,
    """
    CREATE TABLE pending_pushes (
        instance_name   TEXT PRIMARY KEY,
        target_value    INTEGER NOT NULL,
        attempt_count   INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        last_error      TEXT
    )
    """,
    """
    CREATE TABLE instance_health (
        instance_name   TEXT PRIMARY KEY,
        status          TEXT NOT NULL,
        detail          TEXT NOT NULL DEFAULT '',
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE event_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        at              TEXT NOT NULL,
        kind            TEXT NOT NULL,
        instance_name   TEXT,
        rule_name       TEXT,
        detail          TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX event_log_at ON event_log(at)",
    "CREATE INDEX rule_inputs_instance ON rule_inputs(instance_name)",
    "CREATE INDEX rule_outputs_instance ON rule_outputs(instance_name)",
)


class EventKind(enum.Enum):
    """What a row in ``event_log`` records.

    ``event_log`` is a log, not an audit trail. Nothing in the daemon may read
    it back to make a decision; if a user truncates the table the daemon must
    behave identically. It exists so a human can answer "why did this fire".
    """

    INPUT_EVENT = "input-event"
    LATCH_SET = "latch-set"
    LATCH_RETRIGGERED = "latch-retriggered"
    LATCH_CLEARED = "latch-cleared"
    UNLATCH_IGNORED = "unlatch-ignored"
    OUTPUT_PUSH_SUCCEEDED = "output-push-succeeded"
    OUTPUT_PUSH_FAILED = "output-push-failed"
    RECONCILED = "reconciled"
    CONFIG_LOADED = "config-loaded"
    RULE_ORPHANED = "rule-orphaned"
    RULE_ADOPTED = "rule-adopted"
    RENAMED = "renamed"
    PURGED = "purged"
    DAEMON_STARTED = "daemon-started"
    DAEMON_STOPPED = "daemon-stopped"


class HealthStatus(enum.Enum):
    """A plugin instance's own opinion of how it is doing."""

    UNKNOWN = "unknown"
    STARTING = "starting"
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class LatchRecord:
    """Persistent latch state for one rule."""

    rule_name: str
    state: bool
    set_at: datetime.datetime | None
    cleared_at: datetime.datetime | None
    trigger_count: int
    last_cause: str | None


@dataclass(frozen=True)
class RuleRecord:
    """A rule as the database knows it, including rules config no longer defines."""

    name: str
    enabled: bool
    orphaned: bool
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


@dataclass(frozen=True)
class InstanceRecord:
    """An instance as the database knows it."""

    name: str
    plugin: str
    enabled: bool
    orphaned: bool


@dataclass(frozen=True)
class OutputStateRecord:
    """What the daemon last pushed to an output, and what the output confirmed."""

    instance_name: str
    last_applied: bool | None
    last_confirmed: bool | None
    last_sync_at: datetime.datetime | None


@dataclass(frozen=True)
class PendingPush:
    """An outbound push waiting to be attempted.

    Keyed on the instance, so there is at most one row per output. A push is
    always "apply current state", never "apply the delta that failed", and
    keying on the instance is what makes replaying a stale value structurally
    impossible rather than merely unlikely.
    """

    instance_name: str
    target_value: bool
    attempt_count: int
    next_attempt_at: datetime.datetime
    last_error: str | None


@dataclass(frozen=True)
class HealthRecord:
    """The last health report from an instance."""

    instance_name: str
    status: HealthStatus
    detail: str
    updated_at: datetime.datetime


@dataclass(frozen=True)
class EventLogEntry:
    """One row of the rolling event log."""

    identifier: int
    at: datetime.datetime
    kind: EventKind
    instance_name: str | None
    rule_name: str | None
    detail: str


def database_path(state_directory: Path) -> Path:
    """Return the database path inside a state directory."""
    return state_directory / DATABASE_FILENAME


class Database:
    """Owns connections to the SQLite file, one per thread.

    SQLite connections are not safe to share across threads, and passing
    ``check_same_thread=False`` to pretend otherwise trades a loud failure for
    a quiet one. A connection per thread costs nothing at this scale.
    """

    def __init__(self, *, path: Path) -> None:
        self._path = path
        self._local = threading.local()

    @property
    def path(self) -> Path:
        return self._path

    def connection(self) -> sqlite3.Connection:
        existing: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if existing is not None:
            return existing
        opened = self._open()
        self._local.connection = opened
        return opened

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None turns off the driver's implicit transaction
        # handling so that transaction boundaries are the ones written here.
        connection = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a durable write transaction.

        BEGIN IMMEDIATE rather than the default deferred begin: the write lock
        is taken up front, so a concurrent reader cannot turn a write into a
        mid-transaction SQLITE_BUSY.
        """
        connection = self.connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def close(self) -> None:
        """Close the calling thread's connection, if it has one.

        Only the calling thread's, because sqlite3 refuses to let one thread
        touch another's connection -- including to close it. Each thread that
        touches the database closes its own connection on the way out; a
        connection whose thread has already exited is cleaned up when the
        object is collected.
        """
        existing: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if existing is not None:
            existing.close()
            self._local.connection = None


def initialize(database: Database) -> None:
    """Create the schema if it is not there, and refuse an unknown version."""
    connection = database.connection()
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()

    if row is None:
        with database.transaction() as transaction:
            for statement in _SCHEMA_STATEMENTS:
                transaction.execute(statement)
            transaction.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
        return

    version_row = connection.execute("SELECT version FROM schema_version").fetchone()
    version = int(version_row["version"])
    if version != SCHEMA_VERSION:
        raise StorageError(
            f"{database.path} has schema version {version}, but this build of "
            f"noti-mapper speaks version {SCHEMA_VERSION}"
        )


class StorageError(Exception):
    """The database cannot be used."""


def _as_bool(value: object) -> bool:
    return bool(value)


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_time(value: object) -> datetime.datetime | None:
    if value is None:
        return None
    return from_iso(str(value))


class Store:
    """Typed access to the daemon's persistent state.

    Every method is a small operation with an explicit signature. Callers never
    see SQL and never see a bare row.
    """

    def __init__(self, *, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    # -- configuration mirror -------------------------------------------------

    def sync_instances(self, instances: Sequence[InstanceRecord]) -> list[str]:
        """Mirror the configured instances into the database.

        Instances that configuration no longer defines are marked orphaned
        rather than deleted: their ``plugin_kv`` scratch storage and
        ``output_state`` stay put, so an instance that comes back does not have
        to rebuild them. Returns the names newly marked orphaned.
        """
        present = {instance.name for instance in instances}
        newly_orphaned: list[str] = []
        with self._database.transaction() as transaction:
            for instance in instances:
                transaction.execute(
                    """
                    INSERT INTO instances (name, plugin, enabled, orphaned)
                    VALUES (?, ?, ?, 0)
                    ON CONFLICT(name) DO UPDATE SET
                        plugin = excluded.plugin,
                        enabled = excluded.enabled,
                        orphaned = 0
                    """,
                    (instance.name, instance.plugin, int(instance.enabled)),
                )
            rows = transaction.execute("SELECT name FROM instances WHERE orphaned = 0").fetchall()
            for row in rows:
                name = str(row["name"])
                if name not in present:
                    transaction.execute("UPDATE instances SET orphaned = 1 WHERE name = ?", (name,))
                    newly_orphaned.append(name)
        return newly_orphaned

    def sync_rules(self, rules: Sequence[RuleRecord]) -> tuple[list[str], list[str]]:
        """Mirror the configured rules into the database.

        A rule that configuration no longer defines is marked orphaned. Its
        latch record persists and is re-adoptable if a rule with that name
        returns, but it stops contributing to output state immediately.

        Returns ``(newly_orphaned, readopted)``.
        """
        present = {rule.name for rule in rules}
        newly_orphaned: list[str] = []
        readopted: list[str] = []

        with self._database.transaction() as transaction:
            for rule in rules:
                was_orphaned_row = transaction.execute(
                    "SELECT orphaned FROM rules WHERE name = ?", (rule.name,)
                ).fetchone()
                if was_orphaned_row is not None and bool(was_orphaned_row["orphaned"]):
                    readopted.append(rule.name)

                transaction.execute(
                    """
                    INSERT INTO rules (name, enabled, orphaned) VALUES (?, ?, 0)
                    ON CONFLICT(name) DO UPDATE SET
                        enabled = excluded.enabled,
                        orphaned = 0
                    """,
                    (rule.name, int(rule.enabled)),
                )
                transaction.execute(
                    "INSERT OR IGNORE INTO latches (rule_name, state, trigger_count) "
                    "VALUES (?, 0, 0)",
                    (rule.name,),
                )
                transaction.execute("DELETE FROM rule_inputs WHERE rule_name = ?", (rule.name,))
                transaction.execute("DELETE FROM rule_outputs WHERE rule_name = ?", (rule.name,))
                for instance_name in rule.inputs:
                    transaction.execute(
                        "INSERT INTO rule_inputs (rule_name, instance_name) VALUES (?, ?)",
                        (rule.name, instance_name),
                    )
                for instance_name in rule.outputs:
                    transaction.execute(
                        "INSERT INTO rule_outputs (rule_name, instance_name) VALUES (?, ?)",
                        (rule.name, instance_name),
                    )

            rows = transaction.execute("SELECT name FROM rules WHERE orphaned = 0").fetchall()
            for row in rows:
                name = str(row["name"])
                if name not in present:
                    transaction.execute("UPDATE rules SET orphaned = 1 WHERE name = ?", (name,))
                    newly_orphaned.append(name)

        return (newly_orphaned, readopted)

    def rules(self, *, include_orphaned: bool = True) -> list[RuleRecord]:
        connection = self._database.connection()
        clause = "" if include_orphaned else " WHERE orphaned = 0"
        rows = connection.execute(f"SELECT * FROM rules{clause} ORDER BY name").fetchall()

        inputs: dict[str, list[str]] = {}
        outputs: dict[str, list[str]] = {}
        for row in connection.execute("SELECT * FROM rule_inputs ORDER BY instance_name"):
            inputs.setdefault(str(row["rule_name"]), []).append(str(row["instance_name"]))
        for row in connection.execute("SELECT * FROM rule_outputs ORDER BY instance_name"):
            outputs.setdefault(str(row["rule_name"]), []).append(str(row["instance_name"]))

        records: list[RuleRecord] = []
        for row in rows:
            name = str(row["name"])
            records.append(
                RuleRecord(
                    name=name,
                    enabled=_as_bool(row["enabled"]),
                    orphaned=_as_bool(row["orphaned"]),
                    inputs=tuple(inputs.get(name, [])),
                    outputs=tuple(outputs.get(name, [])),
                )
            )
        return records

    def instances(self, *, include_orphaned: bool = True) -> list[InstanceRecord]:
        connection = self._database.connection()
        clause = "" if include_orphaned else " WHERE orphaned = 0"
        rows = connection.execute(f"SELECT * FROM instances{clause} ORDER BY name").fetchall()
        records: list[InstanceRecord] = []
        for row in rows:
            records.append(
                InstanceRecord(
                    name=str(row["name"]),
                    plugin=str(row["plugin"]),
                    enabled=_as_bool(row["enabled"]),
                    orphaned=_as_bool(row["orphaned"]),
                )
            )
        return records

    # -- latches --------------------------------------------------------------

    def latch(self, rule_name: str) -> LatchRecord | None:
        row = (
            self._database.connection()
            .execute("SELECT * FROM latches WHERE rule_name = ?", (rule_name,))
            .fetchone()
        )
        if row is None:
            return None
        return _latch_from_row(row)

    def latches(self) -> list[LatchRecord]:
        rows = (
            self._database.connection()
            .execute("SELECT * FROM latches ORDER BY rule_name")
            .fetchall()
        )
        records: list[LatchRecord] = []
        for row in rows:
            records.append(_latch_from_row(row))
        return records

    def write_latch(self, record: LatchRecord) -> None:
        """Write a latch record. Callers on the core thread only."""
        self._database.connection().execute(
            """
            INSERT INTO latches (rule_name, state, set_at, cleared_at, trigger_count, last_cause)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_name) DO UPDATE SET
                state = excluded.state,
                set_at = excluded.set_at,
                cleared_at = excluded.cleared_at,
                trigger_count = excluded.trigger_count,
                last_cause = excluded.last_cause
            """,
            (
                record.rule_name,
                int(record.state),
                None if record.set_at is None else to_iso(record.set_at),
                None if record.cleared_at is None else to_iso(record.cleared_at),
                record.trigger_count,
                record.last_cause,
            ),
        )

    def rename_rule(self, *, old_name: str, new_name: str) -> None:
        """Migrate a rule and its latch to a new name.

        Renaming a rule in configuration is otherwise indistinguishable from
        deleting one rule and creating another: the old latch orphans and the
        new rule starts cleared. This is what makes a rename not lose state.
        """
        with self._database.transaction() as transaction:
            existing = transaction.execute(
                "SELECT enabled FROM rules WHERE name = ?", (old_name,)
            ).fetchone()
            if existing is None:
                raise StorageError(f"no rule named {old_name!r} in {self._database.path}")
            clash = transaction.execute(
                "SELECT name FROM rules WHERE name = ?", (new_name,)
            ).fetchone()
            if clash is not None:
                raise StorageError(f"a rule named {new_name!r} already exists")

            # Insert the new parent row first, move every child onto it, then
            # drop the old parent. Renaming the parent in place would leave the
            # children pointing at a name that no longer exists for the length
            # of one statement, which the foreign keys correctly refuse.
            transaction.execute(
                "INSERT INTO rules (name, enabled, orphaned) VALUES (?, ?, 0)",
                (new_name, int(bool(existing["enabled"]))),
            )
            transaction.execute(
                "UPDATE latches SET rule_name = ? WHERE rule_name = ?", (new_name, old_name)
            )
            transaction.execute(
                "UPDATE rule_inputs SET rule_name = ? WHERE rule_name = ?", (new_name, old_name)
            )
            transaction.execute(
                "UPDATE rule_outputs SET rule_name = ? WHERE rule_name = ?", (new_name, old_name)
            )
            transaction.execute("DELETE FROM rules WHERE name = ?", (old_name,))

    def purge_orphans(self) -> tuple[list[str], list[str]]:
        """Delete orphaned rules (with their latches) and orphaned instance state.

        Returns ``(purged_rules, purged_instances)``.
        """
        with self._database.transaction() as transaction:
            rule_rows = transaction.execute(
                "SELECT name FROM rules WHERE orphaned = 1 ORDER BY name"
            ).fetchall()
            instance_rows = transaction.execute(
                "SELECT name FROM instances WHERE orphaned = 1 ORDER BY name"
            ).fetchall()

            purged_rules = [str(row["name"]) for row in rule_rows]
            purged_instances = [str(row["name"]) for row in instance_rows]

            for name in purged_rules:
                transaction.execute("DELETE FROM latches WHERE rule_name = ?", (name,))
                transaction.execute("DELETE FROM rule_inputs WHERE rule_name = ?", (name,))
                transaction.execute("DELETE FROM rule_outputs WHERE rule_name = ?", (name,))
                transaction.execute("DELETE FROM rules WHERE name = ?", (name,))

            for name in purged_instances:
                transaction.execute("DELETE FROM plugin_kv WHERE instance_name = ?", (name,))
                transaction.execute("DELETE FROM output_state WHERE instance_name = ?", (name,))
                transaction.execute("DELETE FROM pending_pushes WHERE instance_name = ?", (name,))
                transaction.execute("DELETE FROM instance_health WHERE instance_name = ?", (name,))
                transaction.execute("DELETE FROM instances WHERE name = ?", (name,))

        return (purged_rules, purged_instances)

    # -- output state ---------------------------------------------------------

    def output_state(self, instance_name: str) -> OutputStateRecord | None:
        row = (
            self._database.connection()
            .execute("SELECT * FROM output_state WHERE instance_name = ?", (instance_name,))
            .fetchone()
        )
        if row is None:
            return None
        return _output_state_from_row(row)

    def output_states(self) -> list[OutputStateRecord]:
        rows = (
            self._database.connection()
            .execute("SELECT * FROM output_state ORDER BY instance_name")
            .fetchall()
        )
        records: list[OutputStateRecord] = []
        for row in rows:
            records.append(_output_state_from_row(row))
        return records

    def write_output_state(self, record: OutputStateRecord) -> None:
        self._database.connection().execute(
            """
            INSERT INTO output_state (instance_name, last_applied, last_confirmed, last_sync_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(instance_name) DO UPDATE SET
                last_applied = excluded.last_applied,
                last_confirmed = excluded.last_confirmed,
                last_sync_at = excluded.last_sync_at
            """,
            (
                record.instance_name,
                None if record.last_applied is None else int(record.last_applied),
                None if record.last_confirmed is None else int(record.last_confirmed),
                None if record.last_sync_at is None else to_iso(record.last_sync_at),
            ),
        )

    # -- pending pushes -------------------------------------------------------

    def write_pending_push(self, push: PendingPush) -> None:
        """Insert or replace the pending push for an instance.

        There is at most one per instance by construction, so a newer state
        change always supersedes an older one rather than queueing behind it.
        """
        self._database.connection().execute(
            """
            INSERT INTO pending_pushes
                (instance_name, target_value, attempt_count, next_attempt_at, last_error)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(instance_name) DO UPDATE SET
                target_value = excluded.target_value,
                attempt_count = excluded.attempt_count,
                next_attempt_at = excluded.next_attempt_at,
                last_error = excluded.last_error
            """,
            (
                push.instance_name,
                int(push.target_value),
                push.attempt_count,
                to_iso(push.next_attempt_at),
                push.last_error,
            ),
        )

    def delete_pending_push(self, instance_name: str) -> None:
        self._database.connection().execute(
            "DELETE FROM pending_pushes WHERE instance_name = ?", (instance_name,)
        )

    def pending_pushes(self) -> list[PendingPush]:
        rows = (
            self._database.connection()
            .execute("SELECT * FROM pending_pushes ORDER BY next_attempt_at, instance_name")
            .fetchall()
        )
        pushes: list[PendingPush] = []
        for row in rows:
            pushes.append(_pending_push_from_row(row))
        return pushes

    def pending_pushes_due(self, moment: datetime.datetime) -> list[PendingPush]:
        rows = (
            self._database.connection()
            .execute(
                "SELECT * FROM pending_pushes WHERE next_attempt_at <= ? "
                "ORDER BY next_attempt_at, instance_name",
                (to_iso(moment),),
            )
            .fetchall()
        )
        pushes: list[PendingPush] = []
        for row in rows:
            pushes.append(_pending_push_from_row(row))
        return pushes

    def earliest_pending_attempt(self) -> datetime.datetime | None:
        row = (
            self._database.connection()
            .execute("SELECT MIN(next_attempt_at) AS soonest FROM pending_pushes")
            .fetchone()
        )
        if row is None or row["soonest"] is None:
            return None
        return from_iso(str(row["soonest"]))


class PluginKeyValueStore:
    """Durable per-instance scratch storage for plugins.

    The IMAP reader needs somewhere to keep ``UIDVALIDITY`` and the last
    processed UID. Plugins get this rather than managing their own files so
    that there is one thing to back up and one thing to migrate.

    This is the one part of the database plugins touch, it is reachable from
    plugin threads, and it writes only to ``plugin_kv`` -- a table no other
    code path writes.
    """

    def __init__(self, *, database: Database, instance_name: str) -> None:
        self._database = database
        self._instance_name = instance_name

    @property
    def instance_name(self) -> str:
        return self._instance_name

    def get(self, key: str) -> str | None:
        row = (
            self._database.connection()
            .execute(
                "SELECT value FROM plugin_kv WHERE instance_name = ? AND key = ?",
                (self._instance_name, key),
            )
            .fetchone()
        )
        if row is None:
            return None
        return str(row["value"])

    def set(self, key: str, value: str) -> None:
        with self._database.transaction() as transaction:
            transaction.execute(
                """
                INSERT INTO plugin_kv (instance_name, key, value) VALUES (?, ?, ?)
                ON CONFLICT(instance_name, key) DO UPDATE SET value = excluded.value
                """,
                (self._instance_name, key, value),
            )

    def delete(self, key: str) -> None:
        with self._database.transaction() as transaction:
            transaction.execute(
                "DELETE FROM plugin_kv WHERE instance_name = ? AND key = ?",
                (self._instance_name, key),
            )

    def items(self) -> Mapping[str, str]:
        rows = (
            self._database.connection()
            .execute(
                "SELECT key, value FROM plugin_kv WHERE instance_name = ? ORDER BY key",
                (self._instance_name,),
            )
            .fetchall()
        )
        result: dict[str, str] = {}
        for row in rows:
            result[str(row["key"])] = str(row["value"])
        return result


def _latch_from_row(row: sqlite3.Row) -> LatchRecord:
    return LatchRecord(
        rule_name=str(row["rule_name"]),
        state=_as_bool(row["state"]),
        set_at=_optional_time(row["set_at"]),
        cleared_at=_optional_time(row["cleared_at"]),
        trigger_count=int(row["trigger_count"]),
        last_cause=None if row["last_cause"] is None else str(row["last_cause"]),
    )


def _output_state_from_row(row: sqlite3.Row) -> OutputStateRecord:
    return OutputStateRecord(
        instance_name=str(row["instance_name"]),
        last_applied=_optional_bool(row["last_applied"]),
        last_confirmed=_optional_bool(row["last_confirmed"]),
        last_sync_at=_optional_time(row["last_sync_at"]),
    )


def _pending_push_from_row(row: sqlite3.Row) -> PendingPush:
    return PendingPush(
        instance_name=str(row["instance_name"]),
        target_value=_as_bool(row["target_value"]),
        attempt_count=int(row["attempt_count"]),
        next_attempt_at=from_iso(str(row["next_attempt_at"])),
        last_error=None if row["last_error"] is None else str(row["last_error"]),
    )
