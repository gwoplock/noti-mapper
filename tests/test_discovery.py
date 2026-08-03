import datetime
from pathlib import Path

from noti_mapper.discovery import (
    default_search_path,
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
