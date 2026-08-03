import datetime
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from noti_mapper.config import PluginDirection
from noti_mapper.discovery import (
    DiscoveryResult,
    default_search_path,
    discover,
    known_plugins,
    source_checkout_plugin_directory,
)
from noti_mapper.plugin import (
    ObservedEvent,
    PluginHealth,
    RemoteBelief,
    RemoteState,
    clamp_metadata,
)
from noti_mapper.storage import HealthStatus

_INPUT_SOURCE = """
import datetime
from collections.abc import Mapping

from noti_mapper.plugin import InputPlugin, ObservedEvent, PluginHealth
from noti_mapper.storage import HealthStatus

PLUGIN_NAME = "{plugin_name}"


class Reader(InputPlugin):
    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        if "host" in settings:
            return []
        return ['"host" is required']

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def health(self) -> PluginHealth:
        return PluginHealth(status=HealthStatus.OK)

    def catch_up(self, since: datetime.datetime | None) -> list[ObservedEvent]:
        return []


INPUT_PLUGIN = Reader
"""

_OUTPUT_SOURCE = """
from collections.abc import Mapping

from noti_mapper.plugin import OutputPlugin, PluginHealth, RemoteBelief, RemoteState
from noti_mapper.storage import HealthStatus

PLUGIN_NAME = "{plugin_name}"


class Switch(OutputPlugin):
    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        return []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def apply(self, state: bool) -> None:
        pass

    def query(self) -> RemoteState:
        return RemoteState(belief=RemoteBelief.UNKNOWN)

    def health(self) -> PluginHealth:
        return PluginHealth(status=HealthStatus.OK)


OUTPUT_PLUGIN = Switch
"""


@pytest.fixture(autouse=True)
def clean_module_table() -> Iterator[None]:
    """Plugins land in sys.modules; do not let one test leak into the next."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("noti_mapper_plugin_"):
            del sys.modules[name]


def _write_plugin(directory: Path, name: str, source: str) -> Path:
    plugin_directory = directory / name
    plugin_directory.mkdir(parents=True, exist_ok=True)
    (plugin_directory / "__init__.py").write_text(source, encoding="utf-8")
    return plugin_directory


def _scan(*directories: Path) -> DiscoveryResult:
    return discover(search_path=list(directories), logger=logging.getLogger("test.discovery"))


# -- happy path ---------------------------------------------------------------


def test_a_directory_with_an_input_class_is_found(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "reader", _INPUT_SOURCE.format(plugin_name="test-input"))
    result = _scan(tmp_path)

    assert result.failures == ()
    assert result.names() == ["test-input"]
    plugin = result.plugins["test-input"]
    assert plugin.input_class is not None
    assert plugin.output_class is None
    assert plugin.directions() == frozenset({PluginDirection.INPUT})


def test_input_and_output_plugins_are_distinguished(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "reader", _INPUT_SOURCE.format(plugin_name="test-input"))
    _write_plugin(tmp_path, "switch", _OUTPUT_SOURCE.format(plugin_name="test-output"))
    result = _scan(tmp_path)

    assert result.names() == ["test-input", "test-output"]
    assert result.plugins["test-output"].directions() == frozenset({PluginDirection.OUTPUT})


def test_a_missing_search_directory_is_not_an_error(tmp_path: Path) -> None:
    result = _scan(tmp_path / "absent")
    assert result.plugins == {}
    assert result.failures == ()


def test_directories_are_scanned_in_order_and_the_first_wins(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_plugin(first, "reader_a", _INPUT_SOURCE.format(plugin_name="shared"))
    _write_plugin(second, "reader_b", _INPUT_SOURCE.format(plugin_name="shared"))

    result = _scan(first, second)
    assert result.names() == ["shared"]
    assert result.plugins["shared"].directory == first / "reader_a"
    assert len(result.failures) == 1
    assert "already provided by" in result.failures[0].message


def test_non_plugin_directories_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "not-a-package").mkdir()
    (tmp_path / "not-a-package" / "readme.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "loose.py").write_text("PLUGIN_NAME = 'nope'", encoding="utf-8")

    result = _scan(tmp_path)
    assert result.plugins == {}
    assert result.failures == ()


# -- failures are survivable --------------------------------------------------


def test_a_plugin_that_raises_on_import_is_logged_and_skipped(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "broken", "raise RuntimeError('no thanks')\n")
    _write_plugin(tmp_path, "working", _INPUT_SOURCE.format(plugin_name="test-input"))

    result = _scan(tmp_path)
    assert result.names() == ["test-input"]
    assert len(result.failures) == 1
    assert "RuntimeError: no thanks" in result.failures[0].message
    assert "no thanks" in result.failures[0].traceback_text


def test_a_plugin_importing_a_missing_library_is_skipped(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "needy", "import a_library_that_does_not_exist\n")
    result = _scan(tmp_path)
    assert result.plugins == {}
    assert "ModuleNotFoundError" in result.failures[0].message


def test_a_plugin_without_a_name_is_rejected(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "anon", "INPUT_PLUGIN = None\n")
    result = _scan(tmp_path)
    assert "does not define PLUGIN_NAME" in result.failures[0].message


def test_a_plugin_with_an_empty_name_is_rejected(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "blank", "PLUGIN_NAME = '   '\nINPUT_PLUGIN = None\n")
    result = _scan(tmp_path)
    assert "non-empty string" in result.failures[0].message


def test_a_plugin_with_neither_class_is_rejected(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "useless", "PLUGIN_NAME = 'useless'\n")
    result = _scan(tmp_path)
    assert "neither INPUT_PLUGIN nor OUTPUT_PLUGIN" in result.failures[0].message


def test_a_class_not_subclassing_the_base_is_rejected(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        "wrong",
        "PLUGIN_NAME = 'wrong'\n\n\nclass NotAPlugin:\n    pass\n\n\nINPUT_PLUGIN = NotAPlugin\n",
    )
    result = _scan(tmp_path)
    assert "does not subclass InputPlugin" in result.failures[0].message


def test_a_non_class_reference_is_rejected(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "notaclass", "PLUGIN_NAME = 'x'\nINPUT_PLUGIN = 42\n")
    result = _scan(tmp_path)
    assert "is not a class" in result.failures[0].message


def test_an_incomplete_subclass_names_the_missing_methods(tmp_path: Path) -> None:
    source = (
        "from noti_mapper.plugin import InputPlugin\n\n"
        "PLUGIN_NAME = 'partial'\n\n\n"
        "class Partial(InputPlugin):\n"
        "    def start(self) -> None:\n"
        "        pass\n\n\n"
        "INPUT_PLUGIN = Partial\n"
    )
    _write_plugin(tmp_path, "partial", source)
    result = _scan(tmp_path)
    message = result.failures[0].message
    assert "does not implement" in message
    assert "catch_up" in message
    assert "health" in message
    assert "stop" in message


# -- multi-file plugins -------------------------------------------------------


def test_a_plugin_may_import_its_own_submodules(tmp_path: Path) -> None:
    directory = _write_plugin(
        tmp_path,
        "multi",
        "from noti_mapper_plugin_multi.helper import NAME\n"
        "from noti_mapper.plugin import InputPlugin\n"
        "import datetime\n"
        "from noti_mapper.plugin import ObservedEvent, PluginHealth\n"
        "from noti_mapper.storage import HealthStatus\n\n"
        "PLUGIN_NAME = NAME\n\n\n"
        "class Reader(InputPlugin):\n"
        "    def start(self) -> None:\n        pass\n"
        "    def stop(self) -> None:\n        pass\n"
        "    def health(self) -> PluginHealth:\n"
        "        return PluginHealth(status=HealthStatus.OK)\n"
        "    def catch_up(self, since: datetime.datetime | None) -> list[ObservedEvent]:\n"
        "        return []\n\n\n"
        "INPUT_PLUGIN = Reader\n",
    )
    (directory / "helper.py").write_text("NAME = 'multi-file'\n", encoding="utf-8")

    result = _scan(tmp_path)
    assert result.failures == ()
    assert result.names() == ["multi-file"]


# -- handing off to config validation -----------------------------------------


def test_known_plugins_carries_directions_and_the_settings_validator(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "reader", _INPUT_SOURCE.format(plugin_name="test-input"))
    known = known_plugins(_scan(tmp_path))

    assert list(known) == ["test-input"]
    entry = known["test-input"]
    assert entry.directions == frozenset({PluginDirection.INPUT})
    assert entry.validate_settings is not None
    assert entry.validate_settings({}) == ['"host" is required']
    assert entry.validate_settings({"host": "mail.example.net"}) == []


def test_a_plugin_providing_both_directions_runs_both_validators(tmp_path: Path) -> None:
    source = (
        _INPUT_SOURCE.format(plugin_name="both")
        + "\n\n"
        + _OUTPUT_SOURCE.format(plugin_name="both").split("PLUGIN_NAME", 1)[0]
        + """
class Switch2(OutputPlugin):
    @classmethod
    def validate_settings(cls, settings):
        return ['"port" is required']

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def apply(self, state: bool) -> None:
        pass

    def query(self) -> RemoteState:
        return RemoteState(belief=RemoteBelief.UNKNOWN)

    def health(self) -> PluginHealth:
        return PluginHealth(status=HealthStatus.OK)


OUTPUT_PLUGIN = Switch2
"""
    )
    _write_plugin(tmp_path, "both", source)
    known = known_plugins(_scan(tmp_path))
    entry = known["both"]

    assert entry.directions == frozenset({PluginDirection.INPUT, PluginDirection.OUTPUT})
    assert entry.validate_settings is not None
    assert entry.validate_settings({}) == ['"host" is required', '"port" is required']


# -- the default search path --------------------------------------------------


def test_the_default_search_path_is_in_tree_then_usr_then_etc() -> None:
    path = default_search_path()
    assert path[-2:] == [
        Path("/usr/lib/noti-mapper/plugins"),
        Path("/etc/noti-mapper/plugins"),
    ]
    in_tree = source_checkout_plugin_directory()
    assert in_tree is not None, "the test suite runs from a source checkout"
    assert path[0] == in_tree
    assert in_tree.name == "plugins"


# -- plugin-side value types --------------------------------------------------


def test_observed_event_summary_is_stable_and_sorted() -> None:
    event = ObservedEvent(
        occurred_at=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC),
        metadata={"subject": "Delivered: box", "sender": "ups.com"},
    )
    assert event.summary() == "sender='ups.com' subject='Delivered: box'"
    assert ObservedEvent(occurred_at=event.occurred_at).summary() == "(no metadata)"


def test_clamp_metadata_truncates_only_what_is_too_long() -> None:
    clamped = clamp_metadata({"short": "ok", "long": "x" * 5000})
    assert clamped["short"] == "ok"
    assert clamped["long"].endswith("...[truncated]")
    assert len(clamped["long"]) < 5000


def test_remote_state_defaults_to_no_clear_time() -> None:
    state = RemoteState(belief=RemoteBelief.ACTIVE)
    assert state.cleared_at is None
    assert PluginHealth(status=HealthStatus.OK).detail == ""
