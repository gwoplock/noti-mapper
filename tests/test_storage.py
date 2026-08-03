import datetime
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from noti_mapper.clock import from_iso, to_iso
from noti_mapper.storage import (
    SCHEMA_VERSION,
    Database,
    InstanceRecord,
    LatchRecord,
    PluginKeyValueStore,
    RuleRecord,
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


def _rule(name: str, inputs: tuple[str, ...], outputs: tuple[str, ...]) -> RuleRecord:
    return RuleRecord(name=name, enabled=True, orphaned=False, inputs=inputs, outputs=outputs)


def _instance(name: str, plugin: str = "imap-input") -> InstanceRecord:
    return InstanceRecord(name=name, plugin=plugin, enabled=True, orphaned=False)


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


# -- configuration mirror -----------------------------------------------------


def test_sync_rules_creates_rules_latches_and_edges(store: Store) -> None:
    store.sync_instances([_instance("Mail"), _instance("Lamp", "homekit-output")])
    orphaned, readopted = store.sync_rules([_rule("R", ("Mail",), ("Lamp",))])

    assert orphaned == []
    assert readopted == []

    rules = store.rules()
    assert len(rules) == 1
    assert rules[0].inputs == ("Mail",)
    assert rules[0].outputs == ("Lamp",)

    latch = store.latch("R")
    assert latch is not None
    assert latch.state is False
    assert latch.trigger_count == 0


def test_sync_rules_preserves_an_existing_latch(store: Store) -> None:
    store.sync_rules([_rule("R", ("Mail",), ("Lamp",))])
    store.write_latch(
        LatchRecord(
            rule_name="R",
            state=True,
            set_at=MOMENT,
            cleared_at=None,
            trigger_count=3,
            last_cause="Porch Mail",
        )
    )

    store.sync_rules([_rule("R", ("Mail", "Webhook"), ("Lamp",))])

    latch = store.latch("R")
    assert latch is not None
    assert latch.state is True
    assert latch.trigger_count == 3
    assert latch.set_at == MOMENT
    assert store.rules()[0].inputs == ("Mail", "Webhook")


def test_removing_a_rule_orphans_it_and_keeps_the_latch(store: Store) -> None:
    store.sync_rules([_rule("R", ("Mail",), ("Lamp",)), _rule("S", ("Mail",), ("Lamp",))])
    store.write_latch(
        LatchRecord(
            rule_name="R",
            state=True,
            set_at=MOMENT,
            cleared_at=None,
            trigger_count=1,
            last_cause="Mail",
        )
    )

    orphaned, readopted = store.sync_rules([_rule("S", ("Mail",), ("Lamp",))])
    assert orphaned == ["R"]
    assert readopted == []

    by_name = {rule.name: rule for rule in store.rules()}
    assert by_name["R"].orphaned is True
    assert by_name["S"].orphaned is False

    latch = store.latch("R")
    assert latch is not None
    assert latch.state is True


def test_a_returning_rule_readopts_its_latch(store: Store) -> None:
    store.sync_rules([_rule("R", ("Mail",), ("Lamp",))])
    store.write_latch(
        LatchRecord(
            rule_name="R",
            state=True,
            set_at=MOMENT,
            cleared_at=None,
            trigger_count=7,
            last_cause="Mail",
        )
    )
    store.sync_rules([])
    orphaned, readopted = store.sync_rules([_rule("R", ("Mail",), ("Lamp",))])

    assert orphaned == []
    assert readopted == ["R"]
    latch = store.latch("R")
    assert latch is not None
    assert latch.state is True
    assert latch.trigger_count == 7


def test_reload_does_not_disturb_unrelated_latches(store: Store) -> None:
    store.sync_rules([_rule("Keep", ("A",), ("B",)), _rule("Change", ("A",), ("B",))])
    store.write_latch(
        LatchRecord(
            rule_name="Keep",
            state=True,
            set_at=MOMENT,
            cleared_at=None,
            trigger_count=2,
            last_cause="A",
        )
    )
    store.sync_rules(
        [
            _rule("Keep", ("A",), ("B",)),
            _rule("Change", ("A",), ("B", "C")),
            _rule("New", ("A",), ("B",)),
        ]
    )

    keep = store.latch("Keep")
    assert keep is not None
    assert keep.state is True
    assert keep.trigger_count == 2


def test_removing_an_instance_orphans_it_but_keeps_scratch_storage(store: Store) -> None:
    store.sync_instances([_instance("Mail"), _instance("Lamp", "homekit-output")])
    kv = PluginKeyValueStore(database=store.database, instance_name="Mail")
    kv.set("last_uid", "4242")

    newly_orphaned = store.sync_instances([_instance("Lamp", "homekit-output")])
    assert newly_orphaned == ["Mail"]

    by_name = {instance.name: instance for instance in store.instances()}
    assert by_name["Mail"].orphaned is True
    assert kv.get("last_uid") == "4242"


def test_orphaning_is_reported_only_once(store: Store) -> None:
    store.sync_rules([_rule("R", ("A",), ("B",))])
    assert store.sync_rules([])[0] == ["R"]
    assert store.sync_rules([])[0] == []


# -- latches ------------------------------------------------------------------


def test_latch_round_trips(store: Store) -> None:
    store.sync_rules([_rule("R", ("A",), ("B",))])
    cleared = MOMENT + datetime.timedelta(hours=2)
    store.write_latch(
        LatchRecord(
            rule_name="R",
            state=False,
            set_at=MOMENT,
            cleared_at=cleared,
            trigger_count=5,
            last_cause="Porch Lamp",
        )
    )

    latch = store.latch("R")
    assert latch == LatchRecord(
        rule_name="R",
        state=False,
        set_at=MOMENT,
        cleared_at=cleared,
        trigger_count=5,
        last_cause="Porch Lamp",
    )


def test_latch_for_an_unknown_rule_is_none(store: Store) -> None:
    assert store.latch("nope") is None


def test_writing_a_latch_for_an_unknown_rule_is_refused(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.write_latch(
            LatchRecord(
                rule_name="ghost",
                state=True,
                set_at=MOMENT,
                cleared_at=None,
                trigger_count=1,
                last_cause=None,
            )
        )


# -- rename -------------------------------------------------------------------


def test_rename_migrates_the_latch_and_the_edges(store: Store) -> None:
    store.sync_rules([_rule("Package On Porch", ("Mail",), ("Lamp",))])
    store.write_latch(
        LatchRecord(
            rule_name="Package On Porch",
            state=True,
            set_at=MOMENT,
            cleared_at=None,
            trigger_count=9,
            last_cause="Mail",
        )
    )

    store.rename_rule(old_name="Package On Porch", new_name="Porch Package")

    assert store.latch("Package On Porch") is None
    latch = store.latch("Porch Package")
    assert latch is not None
    assert latch.state is True
    assert latch.trigger_count == 9

    rules = store.rules()
    assert [rule.name for rule in rules] == ["Porch Package"]
    assert rules[0].inputs == ("Mail",)
    assert rules[0].outputs == ("Lamp",)


def test_rename_clears_the_orphaned_flag(store: Store) -> None:
    store.sync_rules([_rule("Old", ("A",), ("B",))])
    store.sync_rules([])
    store.rename_rule(old_name="Old", new_name="New")
    assert store.rules()[0].orphaned is False


def test_rename_refuses_unknown_and_colliding_names(store: Store) -> None:
    store.sync_rules([_rule("A", ("i",), ("o",)), _rule("B", ("i",), ("o",))])
    with pytest.raises(StorageError, match="no rule named"):
        store.rename_rule(old_name="Nope", new_name="X")
    with pytest.raises(StorageError, match="already exists"):
        store.rename_rule(old_name="A", new_name="B")


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
