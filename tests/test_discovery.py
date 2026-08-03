import datetime

from noti_mapper.plugin import (
    ObservedEvent,
    PluginHealth,
    RemoteBelief,
    RemoteState,
    clamp_metadata,
)
from noti_mapper.storage import HealthStatus

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
