"""HomeKit output.

The driver and accessory are constructed for real, so the characteristic
wiring is exercised rather than mocked, but the mDNS advertisement is not
brought up: HAP-python takes several seconds to start and stop a driver, and a
test suite that pays that per test stops being run. Starting the network side
is covered end-to-end by the runtime tests with the probe plugins.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from homekit_output import PERSIST_FILENAME, HomeKitOutput
from noti_mapper.plugin import OutputUpdate, PluginError
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
