"""HomeKit output.

The driver and accessory are constructed for real, so the characteristic
wiring is exercised rather than mocked, but the mDNS advertisement is not
brought up: HAP-python takes several seconds to start and stop a driver, and a
test suite that pays that per test stops being run. Starting the network side
is covered end-to-end by the runtime tests with the probe plugins.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from homekit_output import PERSIST_FILENAME, HomeKitOutput
from noti_mapper.plugin import OutputUpdate, PluginError, RemoteBelief
from noti_mapper.storage import Database, HealthStatus, database_path, initialize
from tests.support import make_context


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    opened = Database(path=database_path(tmp_path / "state"))
    initialize(opened)
    try:
        yield opened
    finally:
        opened.close()


def _plugin(
    tmp_path: Path,
    database: Database,
    unlatches: list[str],
    **settings: object,
) -> HomeKitOutput:
    merged: dict[str, object] = {"port": 0}
    merged.update(settings)
    return HomeKitOutput(
        context=make_context(
            instance_name="Porch Lamp",
            database=database,
            settings=merged,
            state_directory=tmp_path / "state",
        ),
        request_unlatch=unlatches.append,
    )


@pytest.fixture
def started(tmp_path: Path, database: Database) -> Iterator[tuple[HomeKitOutput, list[str]]]:
    unlatches: list[str] = []
    plugin = _plugin(tmp_path, database, unlatches)
    plugin.build_accessory()
    try:
        yield (plugin, unlatches)
    finally:
        plugin.stop()


# -- starting -----------------------------------------------------------------


def test_building_the_accessory_creates_the_per_instance_state_directory(
    started: tuple[HomeKitOutput, list[str]], tmp_path: Path
) -> None:
    plugin, _ = started
    del plugin
    expected = tmp_path / "state" / "plugins" / "Porch Lamp" / PERSIST_FILENAME
    assert expected.parent.is_dir()


def test_pairing_state_persists_across_restarts(tmp_path: Path, database: Database) -> None:
    unlatches: list[str] = []

    first = _plugin(tmp_path, database, unlatches)
    first.build_accessory()
    driver = first._driver  # noqa: SLF001
    assert driver is not None
    first_id = driver.state.mac
    first_pincode = driver.state.pincode
    driver.persist()
    first.stop()

    persist_file = tmp_path / "state" / "plugins" / "Porch Lamp" / PERSIST_FILENAME
    assert persist_file.exists()

    second = _plugin(tmp_path, database, unlatches)
    second.build_accessory()
    try:
        restored = second._driver  # noqa: SLF001
        assert restored is not None
        # Re-pairing on restart is a defect; the identity has to survive.
        assert restored.state.mac == first_id
        assert restored.state.pincode == first_pincode
    finally:
        second.stop()


def test_applying_before_start_raises_so_the_push_retries(
    tmp_path: Path, database: Database
) -> None:
    plugin = _plugin(tmp_path, database, [])
    with pytest.raises(PluginError, match="not running"):
        plugin.apply(OutputUpdate(state=True))


# -- pairing ------------------------------------------------------------------


def test_an_unpaired_accessory_says_so_after_the_persist_file_exists(
    tmp_path: Path, database: Database
) -> None:
    # The persist file is written when the driver starts, not when a controller
    # pairs, so its existence says nothing about pairing. Inferring one from
    # the other loses the setup code after the very first start.
    first = _plugin(tmp_path, database, [])
    first.build_accessory()
    driver = first._driver  # noqa: SLF001
    assert driver is not None
    driver.persist()
    first.stop()

    persist_file = tmp_path / "state" / "plugins" / "Porch Lamp" / PERSIST_FILENAME
    assert persist_file.exists()

    second = _plugin(tmp_path, database, [])
    second.build_accessory()
    try:
        assert second.paired() is False
    finally:
        second.stop()


def test_a_paired_accessory_says_so(started: tuple[HomeKitOutput, list[str]]) -> None:
    plugin, _ = started
    driver = plugin._driver  # noqa: SLF001
    assert driver is not None
    assert plugin.paired() is False

    # What HAP-python records once a controller finishes pairing: a client id,
    # its public key, and the admin permission byte.
    driver.state.add_paired_client(str(uuid.uuid4()).encode("utf-8"), b"public-key", b"\x01")
    assert plugin.paired() is True


def test_pairing_is_unknown_before_the_accessory_is_built(
    tmp_path: Path, database: Database
) -> None:
    assert _plugin(tmp_path, database, []).paired() is False


# -- the write direction ------------------------------------------------------


def test_apply_drives_the_switch_characteristic(
    started: tuple[HomeKitOutput, list[str]],
) -> None:
    plugin, _ = started
    accessory = plugin._accessory  # noqa: SLF001
    assert accessory is not None

    plugin.apply(OutputUpdate(state=True, detail="Delivered: box", trigger_count=1))
    assert accessory.current_state() is True

    plugin.apply(OutputUpdate(state=False))
    assert accessory.current_state() is False


def test_apply_is_idempotent(started: tuple[HomeKitOutput, list[str]]) -> None:
    plugin, _ = started
    accessory = plugin._accessory  # noqa: SLF001
    assert accessory is not None

    for _ in range(3):
        plugin.apply(OutputUpdate(state=True))
    assert accessory.current_state() is True


# -- the reverse direction ----------------------------------------------------


def test_writing_false_requests_an_unlatch(
    started: tuple[HomeKitOutput, list[str]],
) -> None:
    plugin, unlatches = started
    plugin.apply(OutputUpdate(state=True))

    plugin.characteristic_written(False)

    assert unlatches == ["HomeKit switch written false"]


def test_writing_true_is_accepted_and_then_corrected(
    started: tuple[HomeKitOutput, list[str]],
) -> None:
    plugin, unlatches = started
    accessory = plugin._accessory  # noqa: SLF001
    assert accessory is not None

    plugin.apply(OutputUpdate(state=False))
    plugin.characteristic_written(True)

    # Only an input can set a latch, so the switch snaps back rather than the
    # write being refused outright.
    assert unlatches == []
    assert accessory.current_state() is False


def test_writing_true_while_latched_leaves_it_latched(
    started: tuple[HomeKitOutput, list[str]],
) -> None:
    plugin, unlatches = started
    accessory = plugin._accessory  # noqa: SLF001
    assert accessory is not None

    plugin.apply(OutputUpdate(state=True))
    plugin.characteristic_written(True)

    assert unlatches == []
    assert accessory.current_state() is True


def test_a_write_through_the_characteristic_setter_reaches_the_plugin(
    started: tuple[HomeKitOutput, list[str]],
) -> None:
    plugin, unlatches = started
    accessory = plugin._accessory  # noqa: SLF001
    assert accessory is not None

    # This is the path a real controller takes: HAP-python calls the setter
    # callback the accessory registered.
    accessory._written(False)  # noqa: SLF001
    assert unlatches == ["HomeKit switch written false"]


def test_query_reports_unknown_because_homekit_holds_no_state(
    started: tuple[HomeKitOutput, list[str]],
) -> None:
    plugin, _ = started
    state = plugin.query()
    assert state.belief is RemoteBelief.UNKNOWN
    assert state.cleared_at is None


# -- naming and settings ------------------------------------------------------


def test_the_accessory_name_defaults_to_the_instance_name(
    started: tuple[HomeKitOutput, list[str]],
) -> None:
    plugin, _ = started
    accessory = plugin._accessory  # noqa: SLF001
    assert accessory is not None
    assert accessory.display_name == "Porch Lamp"


def test_display_name_overrides_it(tmp_path: Path, database: Database) -> None:
    plugin = _plugin(tmp_path, database, [], display_name="Package Waiting")
    plugin.build_accessory()
    try:
        accessory = plugin._accessory  # noqa: SLF001
        assert accessory is not None
        assert accessory.display_name == "Package Waiting"
    finally:
        plugin.stop()


def test_an_empty_configuration_validates() -> None:
    assert HomeKitOutput.validate_settings({}) == []


def test_unknown_settings_are_reported() -> None:
    assert HomeKitOutput.validate_settings({"name": "Lamp"}) == ['unknown setting "name"']


@pytest.mark.parametrize("port", [-1, 70000, True, "51826"])
def test_a_bad_port_is_reported(port: object) -> None:
    problems = HomeKitOutput.validate_settings({"port": port})
    assert any("between 0 and 65535" in problem for problem in problems)


def test_a_good_port_validates() -> None:
    assert HomeKitOutput.validate_settings({"port": 51827}) == []


def test_string_settings_must_be_non_empty() -> None:
    problems = HomeKitOutput.validate_settings({"display_name": "  "})
    assert any("non-empty string" in problem for problem in problems)


def test_stopping_without_starting_is_safe(tmp_path: Path, database: Database) -> None:
    plugin = _plugin(tmp_path, database, [])
    plugin.stop()
    assert plugin.health().status is HealthStatus.STARTING
