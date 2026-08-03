"""The reconciliation matrix.

The cross product of {latch persisted set/clear} x {input saw an event during
downtime: yes/no} x {output reports active/cleared/unreachable} x {event before
or after the clear timestamp}. This is where the real bugs live and it is cheap
to cover exhaustively.
"""

import datetime
import itertools

import pytest

from noti_mapper.plugin import RemoteBelief, RemoteState
from noti_mapper.reconcile import resolve_rule
from noti_mapper.storage import LatchRecord

NOW = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)
BEFORE_CLEAR = NOW - datetime.timedelta(hours=3)
CLEAR_TIME = NOW - datetime.timedelta(hours=2)
AFTER_CLEAR = NOW - datetime.timedelta(hours=1)


def _latch(*, state: bool, set_at: datetime.datetime | None = None) -> LatchRecord:
    return LatchRecord(
        rule_name="R",
        state=state,
        set_at=set_at,
        cleared_at=None,
        trigger_count=0,
        last_cause=None,
    )


def _resolve(
    *,
    persisted_state: bool,
    event: datetime.datetime | None,
    reports: list[RemoteState],
) -> bool:
    outcome = resolve_rule(
        now=NOW,
        persisted=_latch(state=persisted_state, set_at=BEFORE_CLEAR if persisted_state else None),
        latest_downtime_event=event,
        output_reports=reports,
    )
    return outcome.state


ACTIVE = RemoteState(belief=RemoteBelief.ACTIVE)
CLEARED = RemoteState(belief=RemoteBelief.CLEARED, cleared_at=CLEAR_TIME)
CLEARED_UNDATED = RemoteState(belief=RemoteBelief.CLEARED)
UNKNOWN = RemoteState(belief=RemoteBelief.UNKNOWN)


# -- the full matrix ----------------------------------------------------------

# (persisted, event, report) -> expected state
_MATRIX: dict[tuple[bool, str, str], bool] = {
    # No event during downtime.
    (True, "none", "active"): True,
    (True, "none", "cleared"): False,
    (True, "none", "unreachable"): True,
    (False, "none", "active"): False,
    (False, "none", "cleared"): False,
    (False, "none", "unreachable"): False,
    # An event that predates the clear: the clear is the later word.
    (True, "before", "active"): True,
    (True, "before", "cleared"): False,
    (True, "before", "unreachable"): True,
    (False, "before", "active"): True,
    (False, "before", "cleared"): False,
    (False, "before", "unreachable"): True,
    # An event after the clear: this is the case that gets silently dropped.
    (True, "after", "active"): True,
    (True, "after", "cleared"): True,
    (True, "after", "unreachable"): True,
    (False, "after", "active"): True,
    (False, "after", "cleared"): True,
    (False, "after", "unreachable"): True,
}

_EVENTS = {"none": None, "before": BEFORE_CLEAR, "after": AFTER_CLEAR}
_REPORTS = {"active": [ACTIVE], "cleared": [CLEARED], "unreachable": [UNKNOWN]}


@pytest.mark.parametrize(
    ("persisted", "event_key", "report_key"),
    list(itertools.product([True, False], _EVENTS, _REPORTS)),
)
def test_the_reconciliation_matrix(persisted: bool, event_key: str, report_key: str) -> None:
    expected = _MATRIX[(persisted, event_key, report_key)]
    actual = _resolve(
        persisted_state=persisted,
        event=_EVENTS[event_key],
        reports=list(_REPORTS[report_key]),
    )
    assert actual is expected, (
        f"persisted={persisted} event={event_key} report={report_key}: "
        f"expected {expected}, got {actual}"
    )


def test_the_matrix_is_complete() -> None:
    assert len(_MATRIX) == 2 * len(_EVENTS) * len(_REPORTS)


# -- the case that matters most -----------------------------------------------


def test_cleared_remotely_then_a_new_event_re_sets_the_latch() -> None:
    outcome = resolve_rule(
        now=NOW,
        persisted=_latch(state=True, set_at=BEFORE_CLEAR),
        latest_downtime_event=AFTER_CLEAR,
        output_reports=[CLEARED],
    )
    assert outcome.state is True
    assert outcome.set_at == AFTER_CLEAR
    assert outcome.cleared_at == CLEAR_TIME
    assert "later event" in outcome.reason


def test_cleared_remotely_after_the_event_leaves_it_cleared() -> None:
    outcome = resolve_rule(
        now=NOW,
        persisted=_latch(state=True, set_at=BEFORE_CLEAR),
        latest_downtime_event=BEFORE_CLEAR,
        output_reports=[CLEARED],
    )
    assert outcome.state is False
    assert outcome.cleared_at == CLEAR_TIME


def test_an_event_exactly_at_the_clear_time_does_not_re_set() -> None:
    outcome = resolve_rule(
        now=NOW,
        persisted=_latch(state=True),
        latest_downtime_event=CLEAR_TIME,
        output_reports=[CLEARED],
    )
    assert outcome.state is False


def test_a_clear_with_no_timestamp_loses_to_any_event() -> None:
    outcome = resolve_rule(
        now=NOW,
        persisted=_latch(state=True),
        latest_downtime_event=BEFORE_CLEAR,
        output_reports=[CLEARED_UNDATED],
    )
    assert outcome.state is True
    assert outcome.set_at == BEFORE_CLEAR


def test_a_clear_with_no_timestamp_and_no_event_clears() -> None:
    outcome = resolve_rule(
        now=NOW,
        persisted=_latch(state=True),
        latest_downtime_event=None,
        output_reports=[CLEARED_UNDATED],
    )
    assert outcome.state is False
    assert outcome.cleared_at == NOW
