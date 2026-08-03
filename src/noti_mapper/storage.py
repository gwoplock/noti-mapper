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
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path

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
