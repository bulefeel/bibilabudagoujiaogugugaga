from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import (
    ApprovalRequest,
    NotificationDelivery,
    OperationGuard,
    Run,
    RunEvent,
    Schedule,
    SiteRun,
    Store,
    StoreMarketplace,
)
from ziniao_automation.notifications import (
    DatabaseNoticeBuilder,
    DatabaseNotificationAdapter,
    FeishuNotifier,
    NotificationDeliveryService,
    NotificationKind,
    SafeRunNotice,
    notification_dedupe_key,
)
from ziniao_automation.workflows.types import RunStatus, WorkflowReport


# 09:00 Asia/Singapore. Amazon counts its 24-hour payout cap from the
# previous request, so schedules are an absolute anchor plus a period now.
SCHEDULE_ANCHOR = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
SCHEDULE_ANCHOR_ISO = "2026-01-01T01:00:00+00:00"


@pytest.fixture()
def database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'notifications.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


def seed(
    factory,
    *,
    status: str = "WAITING_APPROVAL",
    guard_state: str | None = None,
) -> tuple[str, str]:
    scheduled = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
    with factory() as session:
        store = Store(
            name="加拿大示例店",
            selector_type="oauth",
            selector_value="SECRET_BROWSER_OAUTH",
            browser_oauth="SECRET_BROWSER_OAUTH",
            expected_seller_id="FULL_SELLER_ID_SECRET",
            identity_confirmed=True,
            enabled=True,
            raw_profile={"cookie": "COOKIE_SECRET"},
        )
        session.add(store)
        session.flush()
        market = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        session.add(market)
        session.flush()
        schedule = Schedule(
            store_id=store.id,
            name="工作日余额检查",
            workflow="amazon_disbursement",
            mode="approval",
            first_run_at=SCHEDULE_ANCHOR,
            interval_minutes=1440,
            timezone="Asia/Singapore",
            marketplace_codes=["CA"],
            enabled=True,
        )
        session.add(schedule)
        session.flush()
        run = Run(
            store_id=store.id,
            schedule_id=schedule.id,
            workflow="amazon_disbursement",
            mode="approval",
            trigger="schedule",
            status=status,
            requested_by="scheduler",
            scheduled_for_at=scheduled,
            started_at=scheduled + timedelta(minutes=12),
            auth_deadline=scheduled + timedelta(minutes=42),
            error=(
                r"RuntimeError Token=TOKEN_SECRET D:\private\evidence.png"
                if status == "FAILED"
                else None
            ),
            result_summary={
                "cookie": "SUMMARY_COOKIE_SECRET",
                "token": "SUMMARY_TOKEN_SECRET",
                "seller_id": "SUMMARY_SELLER_SECRET",
            },
        )
        session.add(run)
        session.flush()
        site = SiteRun(
            run_id=run.id,
            marketplace_id=market.id,
            marketplace_code="CA",
            status="CONFIRMED" if guard_state == "CONFIRMED" else status,
            currency="CAD",
            payable_amount=197.63,
            delayed_amount=2295.78,
            settlement_key="SECRET_SETTLEMENT_KEY",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
            details={"cookie": "SITE_COOKIE_SECRET", "receipt_id": "FULL_RECEIPT_SECRET"},
        )
        session.add(site)
        session.flush()
        if guard_state:
            session.add(
                OperationGuard(
                    guard_key="SECRET_GUARD_KEY",
                    run_id=run.id,
                    site_run_id=site.id,
                    store_id=store.id,
                    workflow="amazon_disbursement",
                    marketplace_code="CA",
                    settlement_key="SECRET_SETTLEMENT_KEY",
                    state=guard_state,
                    amount=197.63,
                    currency="CAD",
                    plan_hash="a" * 64,
                    snapshot_hash="b" * 64,
                    metadata_json={
                        "receipt_id": "FULL_RECEIPT_SECRET",
                        "token": "GUARD_TOKEN_SECRET",
                    },
                )
            )
        session.add(
            ApprovalRequest(
                run_id=run.id,
                plan_hash="c" * 64,
                snapshot_hash="d" * 64,
                plan_json={"seller_id": "PLAN_SELLER_SECRET", "cookie": "PLAN_COOKIE_SECRET"},
                status="PENDING",
                expires_at=scheduled + timedelta(hours=1),
            )
        )
        session.add(
            RunEvent(
                run_id=run.id,
                site_run_id=site.id,
                event_type="STATUS_CHANGED",
                to_status=status,
                message="正常状态消息",
                details={"cookie": "EVENT_COOKIE_SECRET", "token": "EVENT_TOKEN_SECRET"},
            )
        )
        session.commit()
        return run.id, site.id




def test_payment_confirmation_requires_confirmed_guard(database) -> None:
    run_id, _ = seed(database, status="SUCCEEDED")
    builder = DatabaseNoticeBuilder(database)
    assert builder.build(run_id, NotificationKind.PAYMENT_CONFIRMED) is None

    with database() as session:
        run = session.get(Run, run_id)
        site = session.scalar(select(SiteRun).where(SiteRun.run_id == run_id))
        session.add(
            OperationGuard(
                guard_key="confirmed-guard",
                run_id=run_id,
                site_run_id=site.id,
                store_id=run.store_id,
                workflow=run.workflow,
                marketplace_code="CA",
                settlement_key="confirmed-cycle",
                state="CONFIRMED",
                amount=197.63,
                currency="CAD",
                plan_hash="e" * 64,
                snapshot_hash="f" * 64,
                metadata_json={"receipt_id": "SHOULD_NOT_LEAVE"},
            )
        )
        session.commit()

    confirmed = builder.build(run_id, NotificationKind.PAYMENT_CONFIRMED)
    assert confirmed is not None
    assert confirmed.sites[0].outcome == "CONFIRMED"
    assert confirmed.sites[0].reference_suffix == ""
    assert "SHOULD_NOT_LEAVE" not in repr(confirmed)


@pytest.mark.asyncio
async def test_delivery_marks_sent_and_same_key_is_a_permanent_noop(database) -> None:
    run_id, site_id = seed(database, status="SUCCEEDED", guard_state="CONFIRMED")

    class Sender:
        def __init__(self) -> None:
            self.notices: list[SafeRunNotice] = []

        async def send(self, notice: SafeRunNotice) -> None:
            self.notices.append(notice)

    sender = Sender()
    service = NotificationDeliveryService(database, sender)
    key = notification_dedupe_key(
        run_id, NotificationKind.PAYMENT_CONFIRMED, site_run_id=site_id
    )
    assert await service.deliver(
        run_id,
        NotificationKind.PAYMENT_CONFIRMED,
        dedupe_key=key,
        site_run_id=site_id,
    )
    assert not await service.deliver(
        run_id,
        NotificationKind.PAYMENT_CONFIRMED,
        dedupe_key=key,
        site_run_id=site_id,
    )
    assert len(sender.notices) == 1
    with database() as session:
        row = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.dedupe_key == key)
        )
        assert row.status == "SENT"
        assert row.attempts == 1
        assert row.sent_at is not None


@pytest.mark.asyncio
async def test_failed_notification_can_retry_without_changing_business_state(database) -> None:
    run_id, site_id = seed(database, status="FAILED")

    class Sender:
        calls = 0

        async def send(self, notice: SafeRunNotice) -> None:
            del notice
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Token=NETWORK_TOKEN_SECRET")

    sender = Sender()
    service = NotificationDeliveryService(database, sender)
    key = "failed-retry-key"
    assert not await service.deliver(
        run_id, NotificationKind.RUN_FAILED, dedupe_key=key, site_run_id=site_id
    )
    with database() as session:
        row = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.dedupe_key == key)
        )
        assert row.status == "FAILED"
        assert row.attempts == 1
        assert "NETWORK_TOKEN_SECRET" not in row.last_error
        assert session.get(Run, run_id).status == "FAILED"
        assert session.get(SiteRun, site_id).status == "FAILED"

    assert await service.deliver(
        run_id, NotificationKind.RUN_FAILED, dedupe_key=key, site_run_id=site_id
    )
    with database() as session:
        row = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.dedupe_key == key)
        )
        assert row.status == "SENT"
        assert row.attempts == 2
        assert session.get(Run, run_id).status == "FAILED"
        assert session.get(SiteRun, site_id).status == "FAILED"


@pytest.mark.asyncio
async def test_mismatched_database_fact_is_not_sent(database) -> None:
    run_id, _ = seed(database, status="SUCCEEDED")

    class Sender:
        called = False

        async def send(self, notice: SafeRunNotice) -> None:
            del notice
            self.called = True

    sender = Sender()
    service = NotificationDeliveryService(database, sender)
    assert not await service.deliver(run_id, NotificationKind.PAYMENT_CONFIRMED)
    assert sender.called is False
    with database() as session:
        assert session.scalar(select(NotificationDelivery)) is None
        assert session.get(Run, run_id).status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_failed_custom_key_cannot_be_reused_for_a_different_run(database) -> None:
    first_id, _ = seed(database, status="FAILED")
    with database() as session:
        first = session.get(Run, first_id)
        second = Run(
            store_id=first.store_id,
            workflow=first.workflow,
            mode="approval",
            trigger="manual",
            status="FAILED",
            requested_by="admin",
            error="another failure",
        )
        session.add(second)
        session.commit()
        second_id = second.id

    class Sender:
        called = 0

        async def send(self, notice: SafeRunNotice) -> None:
            del notice
            self.called += 1
            raise RuntimeError("network")

    sender = Sender()
    service = NotificationDeliveryService(database, sender)
    assert not await service.deliver(
        first_id, NotificationKind.RUN_FAILED, dedupe_key="shared-key"
    )
    assert not await service.deliver(
        second_id, NotificationKind.RUN_FAILED, dedupe_key="shared-key"
    )
    assert sender.called == 1
    with database() as session:
        row = session.scalar(select(NotificationDelivery))
        assert row.run_id == first_id
        assert row.attempts == 1


@pytest.mark.asyncio
async def test_engine_adapter_uses_only_run_id_and_status_then_database_facts(database) -> None:
    run_id, _ = seed(database, status="WAITING_APPROVAL")

    class Sender:
        notices: list[SafeRunNotice] = []

        async def send(self, notice: SafeRunNotice) -> None:
            self.notices.append(notice)

    sender = Sender()
    adapter = DatabaseNotificationAdapter(
        NotificationDeliveryService(database, sender)
    )
    report = WorkflowReport(
        run_id=run_id,
        status=RunStatus.WAITING_APPROVAL,
        title="MALICIOUS_TITLE_TOKEN=REPORT_SECRET",
        summary="Cookie=REPORT_COOKIE_SECRET",
        fields={
            "store": "FAKE_STORE",
            "operations": [{"receipt": "FULL_REPORT_RECEIPT_SECRET"}],
            "token": "REPORT_TOKEN_SECRET",
        },
    )
    await adapter.send(report)
    assert len(sender.notices) == 1
    safe = repr(sender.notices[0])
    assert "加拿大示例店" in safe
    for forbidden in (
        "MALICIOUS_TITLE",
        "REPORT_SECRET",
        "REPORT_COOKIE_SECRET",
        "FAKE_STORE",
        "FULL_REPORT_RECEIPT_SECRET",
        "REPORT_TOKEN_SECRET",
    ):
        assert forbidden not in safe


@pytest.mark.asyncio
async def test_engine_adapter_silent_statuses_and_routes_success_by_database_guard(database) -> None:
    zero_run_id, _ = seed(database, status="SUCCEEDED")

    class Sender:
        notices: list[SafeRunNotice] = []

        async def send(self, notice: SafeRunNotice) -> None:
            self.notices.append(notice)

    sender = Sender()
    adapter = DatabaseNotificationAdapter(
        NotificationDeliveryService(database, sender)
    )
    # A cancellation is the operator's own doing and a queued run has not
    # finished, so both stay silent.  SKIPPED is no longer in this list — it is
    # now reported — but it is still gated on the database agreeing, and this
    # run is SUCCEEDED, so the report below must still produce nothing.
    for status in (RunStatus.SKIPPED, RunStatus.CANCELLED, RunStatus.QUEUED):
        await adapter.send(WorkflowReport(zero_run_id, status, "ignored", "ignored"))
    await adapter.send(
        WorkflowReport(zero_run_id, RunStatus.SUCCEEDED, "ignored", "ignored")
    )
    assert len(sender.notices) == 1
    assert sender.notices[0].kind is NotificationKind.RUN_COMPLETED
    assert "不代表提现成功" in sender.notices[0].summary

    with database() as session:
        zero = session.get(Run, zero_run_id)
        zero_site = session.scalar(select(SiteRun).where(SiteRun.run_id == zero_run_id))
        confirmed_run = Run(
            store_id=zero.store_id,
            workflow=zero.workflow,
            mode=zero.mode,
            trigger="manual",
            status="SUCCEEDED",
            requested_by="admin",
        )
        session.add(confirmed_run)
        session.flush()
        confirmed_site = SiteRun(
            run_id=confirmed_run.id,
            marketplace_id=zero_site.marketplace_id,
            marketplace_code="CA",
            status="CONFIRMED",
            currency="CAD",
            payable_amount=10,
            delayed_amount=0,
            settlement_key="confirmed-adapter-cycle",
            plan_hash="1" * 64,
            snapshot_hash="2" * 64,
        )
        session.add(confirmed_site)
        session.flush()
        session.add(
            OperationGuard(
                guard_key="confirmed-adapter-guard",
                run_id=confirmed_run.id,
                site_run_id=confirmed_site.id,
                store_id=confirmed_run.store_id,
                workflow=confirmed_run.workflow,
                marketplace_code="CA",
                settlement_key="confirmed-adapter-cycle",
                state="CONFIRMED",
                amount=10,
                currency="CAD",
                plan_hash="1" * 64,
                snapshot_hash="2" * 64,
            )
        )
        session.commit()
        confirmed_run_id = confirmed_run.id
    await adapter.send(
        WorkflowReport(confirmed_run_id, RunStatus.SUCCEEDED, "ignored", "ignored")
    )
    assert len(sender.notices) == 2
    assert sender.notices[1].kind is NotificationKind.PAYMENT_CONFIRMED


@pytest.mark.asyncio
async def test_engine_adapter_now_routes_a_genuinely_skipped_run_to_a_card(database) -> None:
    """The end-to-end half of the fix: engine status in, Feishu notice out.

    Both halves are needed and only one of them was broken.  The database
    builder could already describe a skip; the adapter's status map had no
    entry for ``SKIPPED`` at all, so it returned before the builder ever ran.
    """

    run_id, _ = seed(database, status="SKIPPED")

    class Sender:
        def __init__(self) -> None:
            self.notices: list[SafeRunNotice] = []

        async def send(self, notice: SafeRunNotice) -> None:
            self.notices.append(notice)

    sender = Sender()
    adapter = DatabaseNotificationAdapter(
        NotificationDeliveryService(database, sender)
    )
    await adapter.send(WorkflowReport(run_id, RunStatus.SKIPPED, "ignored", "ignored"))

    assert [notice.kind for notice in sender.notices] == [
        NotificationKind.RUN_SKIPPED
    ]


@pytest.mark.asyncio
async def test_scheduled_zero_balance_and_manual_no_data_get_neutral_details(database) -> None:
    scheduled_id, scheduled_site_id = seed(database, status="SUCCEEDED")
    with database() as session:
        scheduled = session.get(Run, scheduled_id)
        scheduled.mode = "dry_run"
        scheduled_site = session.get(SiteRun, scheduled_site_id)
        scheduled_site.payable_amount = 0
        scheduled_site.delayed_amount = 0

        manual = Run(
            store_id=scheduled.store_id,
            workflow=scheduled.workflow,
            mode="dry_run",
            trigger="manual",
            status="SUCCEEDED",
            requested_by="admin",
            started_at=scheduled.started_at,
        )
        session.add(manual)
        session.flush()
        manual_site = SiteRun(
            run_id=manual.id,
            marketplace_id=scheduled_site.marketplace_id,
            marketplace_code="CA",
            status="SUCCEEDED",
            currency="CAD",
            payable_amount=None,
            delayed_amount=None,
            settlement_key="manual-no-data",
            plan_hash="3" * 64,
            snapshot_hash="4" * 64,
        )
        session.add(manual_site)
        session.commit()
        manual_id = manual.id

    class Sender:
        def __init__(self) -> None:
            self.notices: list[SafeRunNotice] = []

        async def send(self, notice: SafeRunNotice) -> None:
            self.notices.append(notice)

    sender = Sender()
    adapter = DatabaseNotificationAdapter(
        NotificationDeliveryService(database, sender)
    )
    await adapter.send(
        WorkflowReport(scheduled_id, RunStatus.SUCCEEDED, "ignored", "ignored")
    )
    await adapter.send(
        WorkflowReport(manual_id, RunStatus.SUCCEEDED, "ignored", "ignored")
    )

    assert [item.kind for item in sender.notices] == [
        NotificationKind.RUN_COMPLETED,
        NotificationKind.RUN_COMPLETED,
    ]
    scheduled_notice, manual_notice = sender.notices
    assert scheduled_notice.trigger == "schedule"
    assert scheduled_notice.schedule_name == "工作日余额检查"
    assert scheduled_notice.sites[0].payable == Decimal("0.00")
    assert "当前无可提现资金" in scheduled_notice.summary
    assert "未执行提现提交" in scheduled_notice.summary

    assert manual_notice.trigger == "manual"
    assert manual_notice.schedule_name == ""
    assert manual_notice.scheduled_for is None
    assert manual_notice.sites[0].payable is None
    assert "未读取到站点付款数据" in manual_notice.summary
    assert "未执行提现提交" in manual_notice.summary


@pytest.mark.asyncio
async def test_missing_credentials_marks_completion_delivery_failed_not_sent(database) -> None:
    run_id, _ = seed(database, status="SUCCEEDED")
    service = NotificationDeliveryService(database, FeishuNotifier(lambda: None))

    assert not await service.deliver(run_id, NotificationKind.RUN_COMPLETED)

    with database() as session:
        row = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.run_id == run_id)
        )
        assert row is not None
        assert row.kind == NotificationKind.RUN_COMPLETED.value
        assert row.status == "FAILED"
        assert row.attempts == 1
        assert row.last_error == "RuntimeError: 飞书通知发送失败"
        assert session.get(Run, run_id).status == "SUCCEEDED"




@pytest.mark.asyncio
async def test_delivery_rejects_a_site_that_does_not_belong_to_the_run(database) -> None:
    run_id, site_id = seed(database, status="FAILED")
    with database() as session:
        first = session.get(Run, run_id)
        other = Run(
            store_id=first.store_id,
            workflow=first.workflow,
            mode=first.mode,
            trigger="manual",
            status="FAILED",
            requested_by="admin",
        )
        session.add(other)
        session.commit()
        other_id = other.id

    class Sender:
        called = False

        async def send(self, notice: SafeRunNotice) -> None:
            del notice
            self.called = True

    sender = Sender()
    service = NotificationDeliveryService(database, sender)
    assert not await service.deliver(
        other_id,
        NotificationKind.RUN_FAILED,
        site_run_id=site_id,
    )
    assert sender.called is False
    with database() as session:
        assert session.scalar(select(NotificationDelivery)) is None


@pytest.mark.asyncio
async def test_delivery_retry_rebuilds_fresh_database_facts(database) -> None:
    run_id, site_id = seed(database, status="FAILED")

    class Sender:
        notices: list[SafeRunNotice] = []

        async def send(self, notice: SafeRunNotice) -> None:
            self.notices.append(notice)
            if len(self.notices) == 1:
                raise RuntimeError("network")

    sender = Sender()
    service = NotificationDeliveryService(database, sender)
    assert not await service.deliver(
        run_id,
        NotificationKind.RUN_FAILED,
        dedupe_key="fresh-retry-key",
        site_run_id=site_id,
    )
    with database() as session:
        site = session.get(SiteRun, site_id)
        site.payable_amount = 321
        session.commit()

    assert await service.deliver(
        run_id,
        NotificationKind.RUN_FAILED,
        dedupe_key="fresh-retry-key",
        site_run_id=site_id,
    )
    assert sender.notices[0].sites[0].payable == Decimal("197.63")
    assert sender.notices[1].sites[0].payable == Decimal("321.00")


def test_site_skip_reason_travels_from_the_event_stream_to_the_notice(database) -> None:
    """A benign skip records why only as an event, so the card must read it.

    Built from SiteRun alone the card said 「已跳过」 and stopped there, which is
    why an operator seeing AU skipped for Amazon's 24-hour throttle and UK
    blocked for a payout-account mismatch reasonably concluded they were the
    same thing.
    """

    run_id, site_id = seed(database, status="SUCCEEDED")
    with database() as session:
        session.add(
            RunEvent(
                run_id=run_id,
                site_run_id=site_id,
                event_type="site_skipped",
                message="亚马逊限制该账户 24 小时内仅可请求一次提现，约 6 小时 47 分钟后可再次请求",
                details={"reason_code": "payout_rate_limited"},
            )
        )
        session.query(SiteRun).filter(SiteRun.id == site_id).update(
            {"status": "SKIPPED"}
        )
        session.commit()

    notice = DatabaseNoticeBuilder(database).build(
        run_id=run_id, kind=NotificationKind.RUN_COMPLETED
    )

    assert notice is not None
    assert "24 小时内仅可请求一次提现" in notice.sites[0].reason


def test_a_fully_skipped_run_is_reported_with_the_throttle_wait_up_front(database) -> None:
    """The silence this replaces looked exactly like a crash.

    Every site skipped used to produce ``RunStatus.SKIPPED``, which the engine
    adapter mapped to nothing at all: no card, no log line, no status the
    operator would ever see.  Field case 9130f315 read AU, CA and UK, found two
    of them inside Amazon's rolling 24-hour cap, closed the browser and told
    nobody — which is indistinguishable from the automation crashing.

    The wait belongs in the summary, not only in the per-site list: that is the
    one line a phone notification shows.
    """

    run_id, site_id = seed(database, status="SKIPPED")
    with database() as session:
        session.add(
            RunEvent(
                run_id=run_id,
                site_run_id=site_id,
                event_type="site_skipped",
                message=(
                    "亚马逊限制该账户 24 小时内仅可请求一次提现，"
                    "约 2 小时 40 分钟 后可再次请求"
                ),
                details={
                    "reason_code": "payout_rate_limited",
                    "retry_after": "2 小时 40 分钟",
                },
            )
        )
        session.query(SiteRun).filter(SiteRun.id == site_id).update(
            {"status": "SKIPPED"}
        )
        session.commit()

    notice = DatabaseNoticeBuilder(database).build(
        run_id=run_id, kind=NotificationKind.RUN_SKIPPED
    )

    assert notice is not None
    assert "24 小时内仅可请求一次提现" in notice.summary
    assert "2 小时 40 分钟" in notice.summary
    assert "24 小时内仅可请求一次提现" in notice.sites[0].reason
    # Not a failure: the money is still there and nothing needs fixing.
    assert "失败" not in notice.title


def test_a_zero_balance_run_still_reports_rather_than_going_quiet(database) -> None:
    """The operator asked for every skip to be announced, not just odd ones."""

    run_id, site_id = seed(database, status="SKIPPED")
    with database() as session:
        session.add(
            RunEvent(
                run_id=run_id,
                site_run_id=site_id,
                event_type="site_skipped",
                message="标准订单可用资金为 0",
                details={"reason_code": "zero_or_not_submittable"},
            )
        )
        session.query(SiteRun).filter(SiteRun.id == site_id).update(
            {"status": "SKIPPED"}
        )
        session.commit()

    notice = DatabaseNoticeBuilder(database).build(
        run_id=run_id, kind=NotificationKind.RUN_SKIPPED
    )

    assert notice is not None
    assert "标准订单可用资金为 0" in notice.summary


def test_run_skipped_card_is_refused_for_a_run_that_did_not_skip(database) -> None:
    """The kind still has to match the database, like every other kind."""

    run_id, _ = seed(database, status="PARTIAL")

    assert (
        DatabaseNoticeBuilder(database).build(
            run_id=run_id, kind=NotificationKind.RUN_SKIPPED
        )
        is None
    )


def test_site_error_beats_the_skip_event_and_loses_its_class_name(database) -> None:
    """A recorded error is the precise cause; its exception name is noise."""

    run_id, site_id = seed(database, status="PARTIAL")
    with database() as session:
        session.query(SiteRun).filter(SiteRun.id == site_id).update(
            {
                "status": "FAILED",
                "error": "PreflightRejected: 确认页收款账户与建档基线不一致：建档 尾号 003",
            }
        )
        session.commit()

    notice = DatabaseNoticeBuilder(database).build(
        run_id=run_id, kind=NotificationKind.RUN_PARTIAL
    )

    assert notice is not None
    assert notice.sites[0].reason.startswith("确认页收款账户与建档基线不一致")
    assert "PreflightRejected" not in notice.sites[0].reason
    assert "PreflightRejected" not in notice.summary
