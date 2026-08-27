"""The interval grid itself: anchor + k × period, and nothing else.

Amazon counts its once-per-24-hours payout cap from the previous request, not
from midnight, so a daily wall-clock schedule fired a few seconds short of the
window every time and Seller Central refused it — the rule only ever succeeded
every other day. The fix is a period the operator can push past 24 hours, which
means the firing time has to be allowed to walk forward.

These cover the arithmetic that replaced the cron trigger. The old
``_latest_occurrence`` walked an APScheduler trigger forward from an eight-day
cursor; an evenly spaced grid gives the same answer by division, and without
that window quietly capping how far back crash recovery could see.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ziniao_automation.scheduler import (
    _interval_trigger,
    _latest_occurrence,
    _validated_interval,
)


ANCHOR = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)  # 09:00 Asia/Singapore


class _Row:
    """Just the two attributes the grid helpers read."""

    def __init__(self, first_run_at, interval_minutes):
        self.first_run_at = first_run_at
        self.interval_minutes = interval_minutes


def test_nothing_is_due_before_the_anchor() -> None:
    row = _Row(ANCHOR, 1500)
    assert _latest_occurrence(row, now=ANCHOR - timedelta(seconds=1)) is None


def test_the_anchor_itself_is_the_first_occurrence() -> None:
    row = _Row(ANCHOR, 1500)
    assert _latest_occurrence(row, now=ANCHOR) == ANCHOR


@pytest.mark.parametrize(
    ("elapsed", "expected_k"),
    [
        (timedelta(minutes=1499), 0),   # one minute short of the second point
        (timedelta(minutes=1500), 1),   # exactly on it
        (timedelta(minutes=1501), 1),
        (timedelta(days=30), 28),       # 30d = 43200min; 43200 // 1500 = 28
    ],
)
def test_latest_occurrence_lands_on_the_grid(elapsed, expected_k) -> None:
    row = _Row(ANCHOR, 1500)
    assert _latest_occurrence(row, now=ANCHOR + elapsed) == ANCHOR + expected_k * timedelta(
        minutes=1500
    )


def test_a_long_outage_recovers_one_point_not_a_backlog() -> None:
    """Crash recovery asks for the LATEST missed point, never every missed point.

    The cron version enforced this with an eight-day lookback cursor, which also
    meant an outage longer than eight days recovered nothing at all. Arithmetic
    has no such horizon.
    """

    row = _Row(ANCHOR, 1440)
    occurrence = _latest_occurrence(row, now=ANCHOR + timedelta(days=400))
    assert occurrence == ANCHOR + timedelta(days=400)
    assert occurrence is not None


def test_a_naive_anchor_is_read_as_utc() -> None:
    """SQLite hands back naive datetimes; the grid must not shift because of it."""

    row = _Row(ANCHOR.replace(tzinfo=None), 1440)
    assert _latest_occurrence(row, now=ANCHOR + timedelta(hours=1)) == ANCHOR


@pytest.mark.parametrize("bad", [0, -1, None, "", "abc"])
def test_an_unusable_period_is_rejected_rather_than_guessed(bad) -> None:
    """A malformed row must fail loudly and alone.

    The projection loop catches per-row exceptions so one bad rule cannot stop
    every other store's schedule; that only works if a bad rule actually raises.
    """

    with pytest.raises(ValueError):
        _validated_interval(bad)
    with pytest.raises(ValueError):
        _latest_occurrence(_Row(ANCHOR, bad), now=ANCHOR + timedelta(days=1))


def test_a_missing_anchor_raises_instead_of_defaulting_to_now() -> None:
    with pytest.raises(ValueError):
        _latest_occurrence(_Row(None, 1440), now=ANCHOR)


def test_an_anchor_in_the_past_fires_next_on_the_grid_not_immediately() -> None:
    """Editing a running rule leaves its anchor in the past — that is normal.

    APScheduler must advance to the next future point on the same grid rather
    than replaying everything since the anchor.
    """

    trigger = _interval_trigger(interval_minutes=1500, first_run_at=ANCHOR)
    now = ANCHOR + timedelta(minutes=1500 * 3 + 10)
    following = trigger.get_next_fire_time(None, now)

    assert following is not None
    assert following > now
    offset = following.astimezone(timezone.utc) - ANCHOR
    assert offset % timedelta(minutes=1500) == timedelta(0), (
        f"下次触发 {following} 不在 anchor + k×1500 分钟的网格上"
    )
    assert following.astimezone(timezone.utc) == ANCHOR + 4 * timedelta(minutes=1500)


def test_the_period_is_exact_so_a_25_hour_rule_always_clears_24_hours() -> None:
    """The whole point: consecutive fires must never be under the cap.

    A daily wall-clock schedule produced gaps of 24h minus a few seconds and was
    refused. 1500 minutes is the project default precisely because every gap it
    produces is 25h.
    """

    trigger = _interval_trigger(interval_minutes=1500, first_run_at=ANCHOR)
    cursor = None
    previous = None
    for _ in range(5):
        fire = trigger.get_next_fire_time(cursor, cursor or ANCHOR)
        assert fire is not None
        if previous is not None:
            assert fire - previous == timedelta(hours=25)
        previous, cursor = fire, fire


def test_a_two_minute_period_is_usable_end_to_end() -> None:
    """Testing must not require waiting a real hour.

    25 hours is the right default in production, but it makes every manual check
    of "did it actually fire again" a day long. Minutes have to survive the whole
    chain — API validation, the grid, and the trigger — or the only way to see
    the timer work is to wait for it.
    """

    from ziniao_automation.schemas import ScheduleCreate

    payload = ScheduleCreate.model_validate(
        {
            "store_id": 1,
            "name": "两分钟一次",
            "first_run_at": ANCHOR.isoformat(),
            "interval_minutes": 2,
            "marketplace_codes": ["CA"],
        }
    )
    assert payload.interval_minutes == 2
    assert payload.first_run_at == ANCHOR

    row = _Row(ANCHOR, 2)
    assert _latest_occurrence(row, now=ANCHOR + timedelta(minutes=7)) == ANCHOR + timedelta(
        minutes=6
    )

    trigger = _interval_trigger(interval_minutes=2, first_run_at=ANCHOR)
    now = ANCHOR + timedelta(minutes=7)
    following = trigger.get_next_fire_time(None, now)
    assert following is not None
    assert following.astimezone(timezone.utc) == ANCHOR + timedelta(minutes=8)


def test_the_form_can_express_minutes_as_well_as_hours() -> None:
    """The unit selector is the only way to reach a sub-hour period.

    Without it the smallest schedule an operator could build is 60 minutes, and
    the interval feature could not be exercised without a very long wait.
    """

    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    template = (root / "src/ziniao_automation/templates/schedules.html").read_text(
        encoding="utf-8"
    )
    script = (root / "src/ziniao_automation/static/app.js").read_text(encoding="utf-8")

    assert '<option value="1">分钟</option>' in template
    assert '<option value="60" selected>小时</option>' in template
    assert 'min="1"' in template
    # value × unit, so picking 分钟 yields the number itself.
    assert "return Math.round(value*unit);" in script
