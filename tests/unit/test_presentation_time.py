from datetime import datetime, timedelta, timezone

from ziniao_automation.presentation_time import (
    format_display_datetime,
    to_display_datetime,
)


def test_naive_sqlite_utc_is_displayed_as_singapore_time() -> None:
    stored = datetime(2026, 8, 14, 1, 12, 0)

    assert format_display_datetime(stored) == "2026-08-14 09:12:00"


def test_aware_timestamp_preserves_the_instant_before_formatting() -> None:
    stored = datetime(2026, 8, 14, 9, 12, tzinfo=timezone(timedelta(hours=8)))

    displayed = to_display_datetime(stored)

    assert displayed is not None
    assert displayed.isoformat() == "2026-08-14T09:12:00+08:00"


def test_custom_schedule_timezone_and_empty_value_are_supported() -> None:
    stored = datetime(2026, 8, 14, 1, 12, tzinfo=timezone.utc)

    assert format_display_datetime(stored, "%H:%M", "Europe/London") == "02:12"
    assert format_display_datetime(None, empty="尚未开始") == "尚未开始"


def test_unknown_timezone_falls_back_to_singapore() -> None:
    stored = datetime(2026, 8, 14, 1, 12)

    assert format_display_datetime(stored, "%H:%M", "Not/A-Timezone") == "09:12"
