"""Startup reconciliation: deciding what a latch should be after downtime.

This is the part that gets skipped and then silently drops an event. The
scenario: the daemon was down, you resolved the incident from your phone, and
*then* a new package-delivered mail arrived. A naive cursor advance swallows
the second event, and the user never learns their package is on the porch.

The resolution rules, in the order they are applied:

1. If any mapped output reports the state was cleared while the daemon was
   down, the latch clears -- unless an input event is timestamped after that
   clear, in which case the latch re-sets to the event's time.
2. Otherwise, an event observed during downtime sets the latch.
3. Otherwise, an output reporting "still active" confirms a set latch, but
   never resurrects a cleared one. A latch we deliberately cleared stays
   cleared, and step 5 of reconciliation pushes that clear to the remote that
   missed it.
4. Otherwise -- every output unreachable, nothing observed -- the persisted
   state stands.

An output reporting CLEARED without a timestamp is treated as "cleared at some
unknown time in the past", so it loses to any input event. That is the safe
direction: a spurious notification is recoverable, a dropped one is not.

This module is pure so that the whole cross product of
{persisted set/clear} x {downtime event yes/no} x {output active/cleared/
unreachable} x {event before/after the clear} can be table-tested.
"""

import datetime
from collections.abc import Sequence
from dataclasses import dataclass

from noti_mapper.plugin import RemoteBelief, RemoteState
from noti_mapper.storage import LatchRecord


@dataclass(frozen=True)
class ReconcileOutcome:
    """What a rule's latch should be, and why."""

    state: bool
    set_at: datetime.datetime | None
    cleared_at: datetime.datetime | None
    reason: str

    def changed_from(self, persisted: LatchRecord) -> bool:
        return self.state != persisted.state


def resolve_rule(
    *,
    now: datetime.datetime,
    persisted: LatchRecord,
    latest_downtime_event: datetime.datetime | None,
    output_reports: Sequence[RemoteState],
) -> ReconcileOutcome:
    """Decide one rule's latch state from persisted state and what was observed."""
    cleared_reports: list[RemoteState] = []
    active_reported = False
    for report in output_reports:
        if report.belief is RemoteBelief.CLEARED:
            cleared_reports.append(report)
        elif report.belief is RemoteBelief.ACTIVE:
            active_reported = True

    if cleared_reports:
        return _resolve_with_clear(
            now=now,
            persisted=persisted,
            latest_downtime_event=latest_downtime_event,
            cleared_reports=cleared_reports,
        )

    if latest_downtime_event is not None:
        if persisted.state:
            set_at = _latest(persisted.set_at, latest_downtime_event)
            return ReconcileOutcome(
                state=True,
                set_at=set_at,
                cleared_at=persisted.cleared_at,
                reason="latch was already set and an event arrived during downtime",
            )
        return ReconcileOutcome(
            state=True,
            set_at=latest_downtime_event,
            cleared_at=persisted.cleared_at,
            reason="an event arrived during downtime",
        )

    if active_reported and persisted.state:
        return ReconcileOutcome(
            state=True,
            set_at=persisted.set_at,
            cleared_at=persisted.cleared_at,
            reason="an output reports the state is still active",
        )

    if active_reported and not persisted.state:
        # The remote is stale: we cleared this latch and the resolve never
        # landed. Our record wins, and forcing outputs into agreement pushes
        # the clear the remote missed.
        return ReconcileOutcome(
            state=False,
            set_at=persisted.set_at,
            cleared_at=persisted.cleared_at,
            reason="an output still shows active, but the latch was deliberately cleared",
        )

    return ReconcileOutcome(
        state=persisted.state,
        set_at=persisted.set_at,
        cleared_at=persisted.cleared_at,
        reason="no output could be reached; persisted state stands",
    )


def _resolve_with_clear(
    *,
    now: datetime.datetime,
    persisted: LatchRecord,
    latest_downtime_event: datetime.datetime | None,
    cleared_reports: Sequence[RemoteState],
) -> ReconcileOutcome:
    known_clear_times: list[datetime.datetime] = []
    for report in cleared_reports:
        if report.cleared_at is not None:
            known_clear_times.append(report.cleared_at)

    clear_time = max(known_clear_times) if known_clear_times else None

    event_is_newer = latest_downtime_event is not None and (
        clear_time is None or latest_downtime_event > clear_time
    )
    if event_is_newer:
        assert latest_downtime_event is not None
        return ReconcileOutcome(
            state=True,
            set_at=latest_downtime_event,
            cleared_at=clear_time if clear_time is not None else persisted.cleared_at,
            reason="cleared remotely during downtime, then a later event arrived",
        )

    return ReconcileOutcome(
        state=False,
        set_at=persisted.set_at,
        cleared_at=clear_time if clear_time is not None else now,
        reason="an output reports it was cleared during downtime",
    )


def _latest(first: datetime.datetime | None, second: datetime.datetime) -> datetime.datetime:
    if first is None or second > first:
        return second
    return first
