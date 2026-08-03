import datetime
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from noti_mapper.clock import from_iso, to_iso
from noti_mapper.storage import (
    SCHEMA_VERSION,
    Database,
    PluginKeyValueStore,
    StorageError,
    Store,
    database_path,
    initialize,
)

MOMENT = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    opened = Database(path=database_path(tmp_path))
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(database: Database) -> Store:
    initialize(database)
    return Store(database=database)


# -- schema -------------------------------------------------------------------


def test_initialize_creates_the_schema_and_is_idempotent(database: Database) -> None:
    initialize(database)
    initialize(database)

    row = database.connection().execute("SELECT version FROM schema_version").fetchone()
    assert int(row["version"]) == SCHEMA_VERSION
    assert database.path.exists()


def test_wal_mode_and_foreign_keys_are_on(database: Database) -> None:
    initialize(database)
    connection = database.connection()
    assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1


def test_an_unknown_schema_version_is_refused(database: Database) -> None:
    initialize(database)
    database.connection().execute("UPDATE schema_version SET version = 99")
    with pytest.raises(StorageError, match="schema version 99"):
        initialize(database)


def test_a_failed_transaction_rolls_back(store: Store) -> None:
    with pytest.raises(RuntimeError), store.database.transaction() as transaction:
        transaction.execute("INSERT INTO instances (name, plugin, enabled) VALUES ('A', 'p', 1)")
        raise RuntimeError("boom")
    assert store.instances() == []


def test_each_thread_gets_its_own_connection(store: Store) -> None:
    seen: list[int] = []

    def record() -> None:
        seen.append(id(store.database.connection()))
        store.database.close()

    main = id(store.database.connection())
    thread = threading.Thread(target=record, name="storage-test")
    thread.start()
    thread.join()

    assert len(seen) == 1
    assert seen[0] != main


# -- plugin key/value ---------------------------------------------------------


def test_plugin_kv_is_scoped_per_instance(store: Store) -> None:
    first = PluginKeyValueStore(database=store.database, instance_name="Porch Mail")
    second = PluginKeyValueStore(database=store.database, instance_name="Kitchen Mail")

    first.set("uidvalidity", "111")
    second.set("uidvalidity", "222")

    assert first.get("uidvalidity") == "111"
    assert second.get("uidvalidity") == "222"
    assert dict(first.items()) == {"uidvalidity": "111"}

    first.set("uidvalidity", "333")
    assert first.get("uidvalidity") == "333"

    first.delete("uidvalidity")
    assert first.get("uidvalidity") is None
    assert second.get("uidvalidity") == "222"


# -- timestamps ---------------------------------------------------------------


def test_iso_round_trip_is_utc_and_sorts_lexically() -> None:
    early = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)
    late = datetime.datetime(2026, 3, 1, 13, 0, tzinfo=datetime.UTC)
    assert to_iso(early) < to_iso(late)
    assert from_iso(to_iso(early)) == early

    other_zone = early.astimezone(datetime.timezone(datetime.timedelta(hours=-8)))
    assert to_iso(other_zone) == to_iso(early)


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_iso(datetime.datetime(2026, 3, 1, 12, 0))
