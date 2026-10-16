"""Inventory attestation reports carry counts, not payout or browser payloads."""

from dataclasses import asdict
import json

import pytest
from sqlalchemy import select

from ziniao_automation.db import utc_now
from ziniao_automation.models import InventoryGpsrItem, NotificationDelivery, Run, RunEvent, SiteRun
from ziniao_automation.notifications.adapter import DatabaseNotificationAdapter
from ziniao_automation.notifications.database import DatabaseNoticeBuilder, NotificationDeliveryService
from ziniao_automation.notifications.dto import (
    NotificationKind,
    SafeInventoryGpsrCounts,
    SafeRunNotice,
    SafeSiteNotice,
)
from ziniao_automation.notifications.feishu import _card_content
from ziniao_automation.workflows.types import RunStatus, WorkflowReport

from test_database_notifications import database, seed


WORKFLOW = "amazon_inventory_gpsr_attestation"
COUNTS = dict(
    scanned=13, pending=0, blocked=0, eligible=7, confirmed=5, already_submitted=1,
    no_violation_entry=1, no_attestation=2, missing=1, failed=1, uncertain=1,
)


def inventory_seed(database, *, status="SUCCEEDED", mode="auto", counts=None, scan_complete=True):
    run_id, site_id = seed(database, status=status)
    with database() as session:
        run = session.get(Run, run_id)
        run.workflow = WORKFLOW
        run.mode = mode
        run.error = "private SKU-SECRET and ASIN-SECRET Token=TOKEN_SECRET"
        site = session.get(SiteRun, site_id)
        site.marketplace_code = "DE"
        site.currency = "EUR"
        site.error = "Cookie=COOKIE_SECRET seller_id=SELLER_SECRET"
        if counts is not None:
            if scan_complete:
                session.add(RunEvent(run_id=run_id, site_run_id=site_id,
                                     event_type="inventory_gpsr_scan_complete"))
            session.add(RunEvent(
                run_id=run_id, site_run_id=site_id,
                event_type="inventory_gpsr_summary",
                message="SKU-SECRET ASIN-SECRET OTP-SECRET",
                details={**counts, "cookie": "COOKIE_SECRET", "token": "TOKEN_SECRET",
                         "sku": "SKU-SECRET", "asin": "ASIN-SECRET", "raw": {"otp": "OTP-SECRET"}},
            ))
        session.commit()
    return run_id, site_id


@pytest.mark.parametrize(("status", "kind", "mode"), [
    ("SUCCEEDED", NotificationKind.RUN_COMPLETED, "dry_run"),
    ("SUCCEEDED", NotificationKind.RUN_COMPLETED, "auto"),
    ("FAILED", NotificationKind.RUN_FAILED, "auto"),
    ("PARTIAL", NotificationKind.RUN_PARTIAL, "auto"),
    ("SKIPPED", NotificationKind.RUN_SKIPPED, "auto"),
    ("WAITING_AUTH", NotificationKind.WAITING_AUTH, "auto"),
    ("NEEDS_HUMAN_AUTH", NotificationKind.AUTH_TIMEOUT, "auto"),
])
def test_database_to_card_keeps_counts_and_omits_money_and_private_data(database, status, kind, mode):
    counts = {**COUNTS, "confirmed": 0, "uncertain": 0} if mode == "dry_run" else COUNTS
    run_id, _ = inventory_seed(database, status=status, mode=mode, counts=counts)
    notice = DatabaseNoticeBuilder(database).build(run_id, kind)
    assert notice is not None
    assert asdict(notice.inventory_gpsr_counts) == asdict(SafeInventoryGpsrCounts(**counts))
    assert notice.sites[0].payable is None
    assert notice.sites[0].delayed is None
    card = _card_content(notice)
    for expected in ("扫描 13 件", "可处理 7 件", "此前审核中 1 件", "无违规入口 1 件", "无安全证明 2 件", "清单商品未找到 1 件", "失败 1 件"):
        assert expected in card
    for forbidden in ("提现", "资金", "EUR", "0.00", "审核通过", "SKU-SECRET", "ASIN-SECRET", "OTP-SECRET", "COOKIE_SECRET", "TOKEN_SECRET", "SELLER_SECRET", "SECRET_BROWSER_OAUTH"):
        assert forbidden not in card
    if mode == "dry_run":
        assert "空跑完成（未提交）" in card
        assert "未勾选或保存" in card
        assert "无需操作" not in card
    elif status in {"FAILED", "PARTIAL"}:
        assert "结果不明" in card
        assert "不重复保存" in card


def test_only_latest_summary_is_used_after_recovery(database):
    run_id, site_id = inventory_seed(database, counts=COUNTS)
    replacement = {**COUNTS, "confirmed": 6, "uncertain": 0}
    with database() as session:
        session.add(RunEvent(run_id=run_id, site_run_id=site_id,
                            event_type="inventory_gpsr_summary", details=replacement))
        session.add(RunEvent(run_id=run_id, site_run_id=site_id,
                            event_type="inventory_gpsr_item", details={**COUNTS, "scanned": 999}))
        session.commit()
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_COMPLETED)
    assert asdict(notice.inventory_gpsr_counts) == asdict(SafeInventoryGpsrCounts(**replacement))
    assert "已确认提交 6 件" in notice.summary


def test_failure_before_scanning_does_not_invent_zero_counts(database):
    run_id, _ = inventory_seed(database, status="FAILED")
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_FAILED)
    assert notice.inventory_gpsr_counts is None
    assert "尚未完成全部分页扫描" in notice.summary
    card = _card_content(notice)
    assert "扫描 0 件" not in card
    assert "SKU-SECRET" not in card


def test_zero_finally_summary_without_snapshot_does_not_claim_empty_inventory(database):
    counts = dict.fromkeys(COUNTS, 0)
    run_id, _ = inventory_seed(database, status="FAILED", counts=counts, scan_complete=False)
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_FAILED)
    assert notice.inventory_gpsr_counts is None
    card = _card_content(notice)
    assert "尚未完成全部分页扫描" in card
    assert "扫描 0 件" not in card


def test_committed_empty_snapshot_reports_a_verified_zero_candidate_list(database):
    run_id, _ = inventory_seed(database, status="SKIPPED", counts=dict.fromkeys(COUNTS, 0))
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_SKIPPED)
    assert notice.inventory_gpsr_counts.scanned == 0
    card = _card_content(notice)
    assert "扫描 0 件" in card
    assert "尚未完成全部分页扫描" not in card


def test_prior_unsettled_submission_before_scan_has_a_fixed_safe_reason(database):
    run_id, site_id = inventory_seed(database, status="FAILED", counts=dict.fromkeys(COUNTS, 0), scan_complete=False)
    with database() as session:
        session.add(RunEvent(run_id=run_id, site_run_id=site_id,
                            event_type="inventory_gpsr_history_blocked",
                            message="SKU-SECRET ASIN-SECRET historical item", details={"token": "TOKEN_SECRET"}))
        session.commit()
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_FAILED)
    card = _card_content(notice)
    assert "历史合规信息提交结果待核对" in card
    assert "本轮未进行新的提交" in card
    assert all(word not in card for word in ("扫描 0 件", "SKU-SECRET", "ASIN-SECRET", "TOKEN_SECRET"))


@pytest.mark.parametrize("bad", [True, -1, 0.5, "123", {"token": "private"}, 10_000_001])
def test_counter_dto_rejects_non_integer_or_unbounded_event_data(bad):
    assert SafeInventoryGpsrCounts.from_event({**COUNTS, "confirmed": bad}) is None
    with pytest.raises(ValueError):
        SafeInventoryGpsrCounts(confirmed=bad)


def test_missing_fields_do_not_become_a_fake_complete_summary():
    assert SafeInventoryGpsrCounts.from_event({"scanned": 13}) is None
    assert SafeInventoryGpsrCounts.from_event({
        key: value for key, value in COUNTS.items() if key not in {"pending", "blocked"}
    }) is None
    with pytest.raises(TypeError):
        SafeRunNotice(kind=NotificationKind.RUN_COMPLETED, title="test", summary="test",
                      run_short_id="test", store_name="test", inventory_gpsr_counts=COUNTS)


@pytest.mark.parametrize("scan_complete", [True, False])
def test_legacy_summary_uses_current_ledger_and_distinguishes_unchecked_and_blocked(database, scan_complete):
    legacy = {key: 0 for key in COUNTS if key not in {"pending", "blocked"}}
    legacy.update(scanned=13, failed=1)
    run_id, _ = inventory_seed(database, status="FAILED", counts=legacy, scan_complete=scan_complete)
    with database() as session:
        store_id = session.get(Run, run_id).store_id
        for index in range(13):
            session.add(InventoryGpsrItem(
                run_id=run_id, store_id=store_id, marketplace_code="DE",
                sku=f"PRIVATE-SKU-{index}", asin="B123456789", page_number=1,
                position=index, state="BLOCKED" if index == 0 else "PENDING", eligible=False,
            ))
        session.commit()
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_FAILED)
    card = _card_content(notice)
    if scan_complete:
        counts = notice.inventory_gpsr_counts
        assert (counts.scanned, counts.pending, counts.blocked, counts.eligible, counts.confirmed, counts.failed) == (13, 12, 1, 0, 0, 0)
        for label in ("候选扫描 13 件", "尚未检查 12 件", "历史记录待核对 1 件", "已核验可处理 0 件", "本轮提交确认 0 件", "逐商品核验尚未完成"):
            assert label in card
    else:
        assert notice.inventory_gpsr_counts is None
        assert "候选扫描 13 件" not in card
    assert "PRIVATE-SKU" not in card and "B123456789" not in card
    with database() as session:
        original = session.scalar(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "inventory_gpsr_summary"))
        assert original.details["failed"] == 1
        assert "pending" not in original.details


def test_completed_legacy_summary_without_ledger_does_not_invent_unchecked_zero(database):
    legacy = {key: value for key, value in COUNTS.items() if key not in {"pending", "blocked"}}
    run_id, _ = inventory_seed(database, status="FAILED", counts=legacy)
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_FAILED)
    assert notice.inventory_gpsr_counts is None
    assert "尚未检查 0 件" not in _card_content(notice)


def test_ledger_summary_excludes_other_runs_unsettled_history(database):
    run_id, _ = inventory_seed(database, status="FAILED", counts=COUNTS)
    with database() as session:
        store_id = session.get(Run, run_id).store_id
        session.add(InventoryGpsrItem(
            run_id=run_id, store_id=store_id, marketplace_code="DE",
            sku="CURRENT-PRIVATE", asin="B123456789", page_number=1, position=0,
            state="PENDING", eligible=False,
        ))
        # A pruned run's unresolved intent is retained in its ledger, not
        # included in this run's counts or sent as a new submission failure.
        session.add(InventoryGpsrItem(
            run_id=None, store_id=store_id, marketplace_code="DE",
            sku="HISTORICAL-PRIVATE", asin="B123456789", page_number=1, position=0,
            state="UNCERTAIN", eligible=True, intent_at=utc_now(),
        ))
        session.commit()
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_FAILED)
    counts = notice.inventory_gpsr_counts
    assert (counts.scanned, counts.pending, counts.eligible, counts.confirmed, counts.uncertain) == (1, 1, 0, 0, 0)
    card = _card_content(notice)
    assert "本次提交结果不明 0 件" in card
    assert "HISTORICAL-PRIVATE" not in card and "CURRENT-PRIVATE" not in card


@pytest.mark.parametrize("key", ["pending", "blocked"])
@pytest.mark.parametrize("bad", [True, -1, "2", {"token": "secret"}, 10_000_001])
def test_new_counter_fields_keep_strict_numeric_whitelist(key, bad):
    assert SafeInventoryGpsrCounts.from_event({**COUNTS, key: bad}) is None


def test_renderer_cannot_turn_legacy_numeric_site_values_into_inventory_money():
    notice = SafeRunNotice(
        kind=NotificationKind.RUN_COMPLETED, title="库存安全证明", summary="处理完成",
        run_short_id="test", store_name="test", workflow=WORKFLOW,
        sites=(SafeSiteNotice(code="DE", currency="EUR", payable=0, delayed=0,
                              account_tail="1234", outcome="SUCCEEDED"),),
    )
    card = json.loads(_card_content(notice))
    text = json.dumps(card, ensure_ascii=False)
    assert "处理完成" in text
    assert all(word not in text for word in ("EUR", "0.00", "提现", "账户", "1234"))


def seed_cancel_item(database, run_id, state):
    with database() as session:
        run = session.get(Run, run_id)
        row = InventoryGpsrItem(
            run_id=run_id, store_id=run.store_id, marketplace_code="DE",
            sku="PRIVATE-SKU-SECRET", asin="B123456789", page_number=1, position=0,
            state=state, eligible=True,
            intent_at=utc_now() if state in {"SUBMITTING", "UNCERTAIN", "CONFIRMED"} else None,
        )
        session.add(row)
        session.commit()
        return row.id


class CollectInventoryNotices:
    def __init__(self, *, fail=False):
        self.notices = []
        self.fail = fail

    async def send(self, notice):
        self.notices.append(notice)
        if self.fail:
            raise RuntimeError("fixture delivery failure")


def cancelled_report(run_id):
    return WorkflowReport(
        run_id=run_id, status=RunStatus.CANCELLED, title="UNTRUSTED-REPORT-TITLE",
        summary="UNTRUSTED-REPORT-SUMMARY", fields={"uncertain": 999, "token": "TOKEN_SECRET"},
    )


@pytest.mark.parametrize("state", ["SUBMITTING", "UNCERTAIN"])
async def test_cancelled_intent_notifies_once_from_ledger_despite_stale_summary(database, state):
    run_id, _ = inventory_seed(database, status="CANCELLED", counts=dict.fromkeys(COUNTS, 0))
    seed_cancel_item(database, run_id, state)
    sender = CollectInventoryNotices()
    adapter = DatabaseNotificationAdapter(NotificationDeliveryService(database, sender))
    await adapter.send(cancelled_report(run_id))
    # A new adapter/service stands in for process restart; de-duplication lives in SQLite.
    adapter = DatabaseNotificationAdapter(NotificationDeliveryService(database, sender))
    await adapter.send(cancelled_report(run_id))
    assert len(sender.notices) == 1
    notice = sender.notices[0]
    assert notice.kind is NotificationKind.INVENTORY_SUBMISSION_UNCERTAIN
    assert notice.inventory_gpsr_counts.scanned == 1
    assert notice.inventory_gpsr_counts.uncertain == 1
    card = _card_content(notice)
    assert "已取消，提交结果待核对" in card
    assert "仍有 1 项" in card
    assert "不重复提交" in card
    assert json.loads(card)["header"]["template"] == "red"
    assert all(word not in card for word in (
        "提现", "资金", "EUR", "0.00", "PRIVATE-SKU-SECRET", "B123456789",
        "TOKEN_SECRET", "UNTRUSTED-REPORT", "FULL_SELLER_ID_SECRET",
    ))
    with database() as session:
        delivery = session.scalar(select(NotificationDelivery))
        assert delivery.status == "SENT" and delivery.attempts == 1


@pytest.mark.parametrize("state", [None, "ELIGIBLE", "CONFIRMED", "ALREADY_SUBMITTED"])
async def test_ordinary_cancel_without_unsettled_intent_remains_silent(database, state):
    run_id, _ = inventory_seed(database, status="CANCELLED", counts=COUNTS)
    if state:
        seed_cancel_item(database, run_id, state)
    sender = CollectInventoryNotices()
    await DatabaseNotificationAdapter(NotificationDeliveryService(database, sender)).send(cancelled_report(run_id))
    assert not sender.notices
    with database() as session:
        assert session.scalar(select(NotificationDelivery)) is None


@pytest.mark.parametrize("workflow", ["amazon_disbursement", "amazon_gpsr_attestation", "amazon_feedback_removal"])
async def test_other_workflow_cancel_cannot_trigger_inventory_notice(database, workflow):
    run_id, _ = inventory_seed(database, status="CANCELLED", counts=COUNTS)
    seed_cancel_item(database, run_id, "UNCERTAIN")
    with database() as session:
        session.get(Run, run_id).workflow = workflow
        session.commit()
    sender = CollectInventoryNotices()
    await DatabaseNotificationAdapter(NotificationDeliveryService(database, sender)).send(cancelled_report(run_id))
    assert not sender.notices


async def test_failed_cancel_notice_does_not_change_run_or_item_status(database):
    run_id, _ = inventory_seed(database, status="CANCELLED", counts=dict.fromkeys(COUNTS, 0))
    item_id = seed_cancel_item(database, run_id, "SUBMITTING")
    sender = CollectInventoryNotices(fail=True)
    await DatabaseNotificationAdapter(NotificationDeliveryService(database, sender)).send(cancelled_report(run_id))
    assert len(sender.notices) == 1
    with database() as session:
        assert session.get(Run, run_id).status == "CANCELLED"
        assert session.get(InventoryGpsrItem, item_id).state == "SUBMITTING"
        assert session.scalar(select(NotificationDelivery)).status == "FAILED"


def test_manufacturer_counts_are_separate_and_only_numeric_data_is_exported(database):
    counts={**COUNTS,"requirements":39,"safety_confirmed":2,"responsible_confirmed":1,
            "manufacturer_confirmed":2,"confirmed":5,"manufacturer_contact":"private@example.test"}
    run_id,_=inventory_seed(database,counts=counts)
    notice=DatabaseNoticeBuilder(database).build(run_id,NotificationKind.RUN_COMPLETED)
    assert notice.inventory_gpsr_counts.manufacturer_confirmed==2
    card=_card_content(notice)
    assert "制造商已提交、审核中 2 项" in card
    assert "负责人已提交、审核中 1 项" in card
    assert "安全证明已提交、审核中 2 项" in card
    assert "private@example.test" not in card
    assert "审核通过" not in card and "提现" not in card
    assert SafeInventoryGpsrCounts.from_event({**counts,"manufacturer_confirmed":"SECRET"}) is None


def test_mixed_requirement_summary_never_calls_all_confirmations_safety(database):
    counts = {**COUNTS, "scanned": 458, "requirements": 916, "confirmed": 71,
              "safety_confirmed": 0, "responsible_confirmed": 71, "manufacturer_confirmed": 0,
              "pending": 661, "uncertain": 0, "blocked": 0, "failed": 105}
    run_id, site_id = inventory_seed(database, status="PARTIAL", counts=counts)
    with database() as session:
        session.add(RunEvent(run_id=run_id, site_run_id=site_id,
            event_type="inventory_gpsr_stopped",
            message="PRIVATE URL Cookie=SECRET",
            details={"stage": "search_or_location", "reason_code": "read_timeout",
                     "remaining_marketplaces": ["IT", "PL", "SECRET"], "token": "SECRET"}))
        session.commit()
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_PARTIAL)
    assert "已确认提交 71 项合规要求" in notice.summary
    assert "安全证明 0 项、负责人 71 项、制造商 0 项" in notice.summary
    assert "尚未检查 661 项" in notice.summary
    card = _card_content(notice)
    assert "商品定位或 SKU 搜索" in card and "后续 IT、PL 未执行" in card
    assert all(t not in card for t in ("71 件安全证明", "661 件", "PRIVATE", "SECRET", "审核通过"))


@pytest.mark.parametrize("reason,label", [
    ("inventory_scan_unstable", "库存数量持续变化"),
    ("inventory_no_progress", "连续读取失败"),
])
def test_pause_reason_without_http_does_not_invent_http_error(database, reason, label):
    run_id, site_id = inventory_seed(database, status="PAUSED", counts={**COUNTS, "requirements": 26})
    with database() as session:
        session.add(RunEvent(run_id=run_id, site_run_id=site_id, event_type="inventory_gpsr_paused",
            details={"http_status": None, "reason_code": reason, "raw": "SECRET"}))
        session.commit()
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.INVENTORY_PAGE_PAUSED)
    assert label in notice.summary
    assert "HTTP" not in notice.summary and "SECRET" not in _card_content(notice)


@pytest.mark.parametrize("bad", ["TOKEN-SECRET", {"token": "SECRET"}, None])
def test_stop_details_are_enum_only(database, bad):
    run_id, site_id = inventory_seed(database, status="FAILED", counts=COUNTS)
    with database() as session:
        session.add(RunEvent(run_id=run_id, site_run_id=site_id, event_type="inventory_gpsr_stopped",
            message="SECRET", details={"stage": bad, "reason_code": bad, "remaining_marketplaces": bad}))
        session.commit()
    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_FAILED)
    assert "SECRET" not in _card_content(notice)
