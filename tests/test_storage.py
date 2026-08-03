import datetime

import pytest

from noti_mapper.clock import from_iso, to_iso

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
