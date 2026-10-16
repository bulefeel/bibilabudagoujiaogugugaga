"""Persistent page-pause notices use only allow-listed database facts."""

import json

import pytest
from sqlalchemy import select

from ziniao_automation.models import NotificationDelivery, Run, RunEvent, SiteRun
from ziniao_automation.notifications.adapter import DatabaseNotificationAdapter
from ziniao_automation.notifications.database import DatabaseNoticeBuilder, NotificationDeliveryService
from ziniao_automation.notifications.dto import NotificationKind
from ziniao_automation.notifications.feishu import _card_content
from ziniao_automation.workflows.types import RunStatus, WorkflowReport

from test_inventory_gpsr_notifications import (
    COUNTS, CollectInventoryNotices, database, inventory_seed,
)


def paused_event(database, run_id, site_id, status=403):
    with database() as session:
        session.get(SiteRun, site_id).status = "PAUSED"
        session.add(RunEvent(
            run_id=run_id, site_run_id=site_id,
            event_type="inventory_gpsr_paused", to_status="PAUSED",
            message="PRIVATE-RAW-MESSAGE", details={
                "http_status": status, "cookie": "COOKIE-SECRET", "token": "TOKEN-SECRET",
            },
        ))
        session.commit()


def paused_report(run_id):
    return WorkflowReport(
        run_id=run_id, status=RunStatus.PAUSED, title="PRIVATE-REPORT",
        summary="PRIVATE-REPORT", fields={"token": "TOKEN-SECRET", "confirmed": 999},
    )


@pytest.mark.parametrize("http_status", [403, 429, 503])
def test_page_pause_card_preserves_progress_and_masks_private_data(database, http_status):
    counts = {**COUNTS, "pending": 9, "confirmed": 1}
    run_id, site_id = inventory_seed(database, status="PAUSED", counts=counts)
    paused_event(database, run_id, site_id, http_status)
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.INVENTORY_PAGE_PAUSED)
    card = _card_content(notice)
    assert json.loads(card)["header"]["template"] == "orange"
    assert "PAUSED" not in card
    for text in (f"HTTP {http_status}", "尚未检查 9 件", "本轮已确认提交 1 件", "页面已恢复，继续剩余商品", "不会自动重试"):
        assert text in card
    for text in ("PRIVATE", "SECRET", "提现", "EUR", "本轮未进行新的提交", "审核通过"):
        assert text not in card


@pytest.mark.parametrize("status", [True, "403 TOKEN-SECRET", {"cookie": "COOKIE-SECRET"}, 200, 999])
def test_invalid_http_status_never_leaves_database(database, status):
    run_id, site_id = inventory_seed(database, status="PAUSED", counts=COUNTS)
    paused_event(database, run_id, site_id, status)
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.INVENTORY_PAGE_PAUSED)
    assert "HTTP" not in notice.summary
    assert "SECRET" not in _card_content(notice)


def test_pause_before_snapshot_does_not_invent_zero_candidates(database):
    run_id, site_id = inventory_seed(database, status="PAUSED")
    paused_event(database, run_id, site_id)
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.INVENTORY_PAGE_PAUSED)
    assert notice.inventory_gpsr_counts is None
    assert "尚未完成全部分页扫描" in notice.summary
    assert "扫描 0 件" not in _card_content(notice)


@pytest.mark.parametrize("status,workflow", [
    ("FAILED", "amazon_inventory_gpsr_attestation"),
    ("SUCCEEDED", "amazon_inventory_gpsr_attestation"),
    ("PAUSED", "amazon_disbursement"),
    ("PAUSED", "amazon_gpsr_attestation"),
])
def test_notice_requires_matching_database_status_and_workflow(database, status, workflow):
    run_id, _ = inventory_seed(database, status=status, counts=COUNTS)
    with database() as session:
        session.get(Run, run_id).workflow = workflow
        session.commit()
    assert DatabaseNoticeBuilder(database).build(run_id, NotificationKind.INVENTORY_PAGE_PAUSED) is None


async def test_same_pause_is_deduped_but_second_pause_sends_again(database):
    run_id, site_id = inventory_seed(database, status="PAUSED", counts=COUNTS)
    paused_event(database, run_id, site_id)
    sender = CollectInventoryNotices()
    for _ in range(2):
        # Separate service instances model process restart; the ledger persists.
        await DatabaseNotificationAdapter(NotificationDeliveryService(database, sender)).send(paused_report(run_id))
    assert len(sender.notices) == 1
    paused_event(database, run_id, site_id, 429)
    await DatabaseNotificationAdapter(NotificationDeliveryService(database, sender)).send(paused_report(run_id))
    assert len(sender.notices) == 2
    assert "HTTP 429" in sender.notices[-1].summary
    with database() as session:
        assert len(session.scalars(select(NotificationDelivery)).all()) == 2
        assert session.get(Run, run_id).status == "PAUSED"


async def test_notification_failure_does_not_change_pause(database):
    run_id, site_id = inventory_seed(database, status="PAUSED", counts=COUNTS)
    paused_event(database, run_id, site_id)
    await DatabaseNotificationAdapter(
        NotificationDeliveryService(database, CollectInventoryNotices(fail=True))
    ).send(paused_report(run_id))
    with database() as session:
        assert session.get(Run, run_id).status == "PAUSED"
        assert session.scalar(select(NotificationDelivery)).status == "FAILED"
