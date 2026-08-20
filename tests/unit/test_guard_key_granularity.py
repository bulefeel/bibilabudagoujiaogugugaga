"""One payout per store/site/day — not one per settlement cycle.

A settlement cycle stays open for weeks while funds keep accumulating, so
keying the duplicate guard on the cycle alone let the first payout through and
then failed every later run of a weekday schedule with "资金防重复记录已存在".
Per-day also matches the platform-side idempotency check, which already uses
``require_today=True``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ziniao_automation.workflows.types import (
    disbursement_day_key,
    operation_guard_key,
)


def _key(*, day: str, settlement: str = "2026/8/11 - 至今") -> str:
    return operation_guard_key(
        workflow="amazon_disbursement",
        store_id="2",
        marketplace_code="CA",
        settlement_key=settlement,
        disbursement_date=day,
    )


def test_same_day_repeats_still_collide() -> None:
    """The core duplicate protection must survive the granularity change."""

    assert _key(day="2026-08-17") == _key(day="2026-08-17")


def test_two_days_in_one_settlement_cycle_do_not_collide() -> None:
    """This is the case that used to block every run after the first."""

    assert _key(day="2026-08-17") != _key(day="2026-08-18")


def test_marketplace_and_store_still_separate_keys() -> None:
    same_day = {"disbursement_date": "2026-08-17", "settlement_key": "cycle"}
    base = operation_guard_key(
        workflow="amazon_disbursement", store_id="2", marketplace_code="CA", **same_day
    )
    other_site = operation_guard_key(
        workflow="amazon_disbursement", store_id="2", marketplace_code="UK", **same_day
    )
    other_store = operation_guard_key(
        workflow="amazon_disbursement", store_id="3", marketplace_code="CA", **same_day
    )
    assert len({base, other_site, other_store}) == 3


def test_a_new_settlement_cycle_still_separates() -> None:
    assert _key(day="2026-08-17", settlement="2026/8/11 - 至今") != _key(
        day="2026-08-17", settlement="2026/8/25 - 至今"
    )


def test_day_key_uses_the_operator_timezone_not_utc() -> None:
    """The UTC boundary falls at 08:00 local, between the two morning schedules.

    Both 09:00 and 09:10 Beijing must belong to the same local day even though
    they straddle nothing in UTC — and a 07:00 local run must NOT be filed under
    the previous UTC day.
    """

    # 2026-08-17 23:30 UTC == 2026-08-18 07:30 in UTC+8.
    late_utc = datetime(2026, 8, 17, 23, 30, tzinfo=timezone.utc)
    assert disbursement_day_key(late_utc) == "2026-08-18"

    # 2026-08-18 01:00 UTC == 2026-08-18 09:00 in UTC+8: the same local day.
    morning_utc = datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)
    assert disbursement_day_key(morning_utc) == disbursement_day_key(late_utc)

    # Ten minutes later (the second schedule) is still the same day.
    assert disbursement_day_key(morning_utc + timedelta(minutes=10)) == "2026-08-18"
