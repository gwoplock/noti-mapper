"""The reconciliation matrix.

The cross product of {latch persisted set/clear} x {input saw an event during
downtime: yes/no} x {output reports active/cleared/unreachable} x {event before
or after the clear timestamp}. This is where the real bugs live and it is cheap
to cover exhaustively.
"""

import datetime

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
