import datetime

from noti_mapper.plugin import (
    ObservedEvent,
    clamp_metadata,
)

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
