from __future__ import annotations
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from ziniao_automation.schemas import (
    BatchScheduleCreateInput,
    BatchSchedulePreviewInput,
)


# 09:00 Asia/Singapore. Amazon counts its 24-hour payout cap from the
# previous request, so schedules are an absolute anchor plus a period now.
SCHEDULE_ANCHOR = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
SCHEDULE_ANCHOR_ISO = "2026-01-01T01:00:00+00:00"


def _template(**changes) -> dict:
    value = {
        "name": "工作日提现",
        "workflow": "amazon_disbursement",
        "mode": "dry_run",
        "workflow_config": {"marketplace_codes": ["CA", "UK"]},
        "first_run_at": SCHEDULE_ANCHOR_ISO,
        "interval_minutes": 1440,
        "timezone": "Asia/Singapore",
        "enabled": True,
    }
    value.update(changes)
    return value


def test_batch_payload_parses_the_interval_anchor_and_accepts_uuid() -> None:
    """The anchor arrives as JSON text and has to come back as a real instant.

    It replaced the weekday list: Amazon counts its 24-hour payout cap from the
    previous request, so a schedule is now an absolute anchor plus a period.
    """

    payload = BatchScheduleCreateInput.model_validate(
        {
            "request_id": "12345678-1234-4234-9234-123456789abc",
            "store_ids": [3, 8],
            "template": _template(),
        }
    )

    assert payload.template.first_run_at == SCHEDULE_ANCHOR
    assert payload.template.interval_minutes == 1440
    assert payload.store_ids == [3, 8]


def test_batch_payload_refuses_a_period_that_could_never_fire() -> None:
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            BatchSchedulePreviewInput.model_validate(
                {"store_ids": [1], "template": _template(interval_minutes=bad)}
            )


def test_batch_payload_rejects_duplicate_stores_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="不可重复"):
        BatchSchedulePreviewInput.model_validate(
            {"store_ids": [1, 1], "template": _template()}
        )

    dirty = _template()
    dirty["script"] = "arbitrary-code"
    with pytest.raises(ValidationError, match="Extra inputs"):
        BatchSchedulePreviewInput.model_validate(
            {"store_ids": [1], "template": dirty}
        )
