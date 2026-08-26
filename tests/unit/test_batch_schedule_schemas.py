from __future__ import annotations

import pytest
from pydantic import ValidationError

from ziniao_automation.schemas import (
    BatchScheduleCreateInput,
    BatchSchedulePreviewInput,
)


def _template() -> dict:
    return {
        "name": "工作日提现",
        "workflow": "amazon_disbursement",
        "mode": "dry_run",
        "workflow_config": {"marketplace_codes": ["CA", "UK"]},
        "local_time": "09:00",
        "days_of_week": ["fri", "mon", "wed"],
        "timezone": "Asia/Singapore",
        "enabled": True,
    }


def test_batch_payload_normalises_weekdays_and_accepts_uuid() -> None:
    payload = BatchScheduleCreateInput.model_validate(
        {
            "request_id": "12345678-1234-4234-9234-123456789abc",
            "store_ids": [3, 8],
            "template": _template(),
        }
    )

    assert payload.template.days_of_week == "mon,wed,fri"
    assert payload.store_ids == [3, 8]


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
