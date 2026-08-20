from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ziniao_automation.db import Base, utc_now
from ziniao_automation.models import ApprovalRequest, OperationGuard, Run, SiteRun, Store, StoreMarketplace
from ziniao_automation.notifications import NotificationKind
from ziniao_automation.notifications.database import DatabaseNoticeBuilder
from ziniao_automation.workflows.amazon_disbursement import AmazonDisbursementWorkflow
from ziniao_automation.workflows.engine import WorkflowEngine
from ziniao_automation.workflows.registry import WorkflowRegistry
from ziniao_automation.workflows.repository_sqlalchemy import SqlAlchemyWorkflowRepository
from ziniao_automation.workflows.types import (
    MarketplaceRef,
    MarketplaceSnapshot,
    PreflightResult,
    RunMode,
    RunStatus,
    SiteStatus,
    StoreRef,
    WorkflowRun,
)


class ZeroBalanceAdapter:
    async def preflight(self, page, run, marketplace, **kwargs):
        return PreflightResult(
            marketplace.code,
            run.store.expected_seller_id,
            "",
            marketplace.domain,
            "fixture",
        )

    async def read_snapshot(self, page, run, marketplace, **kwargs):
        return MarketplaceSnapshot(
            marketplace_code=marketplace.code,
            domain=marketplace.domain,
            seller_id=run.store.expected_seller_id,
            payment_account="",
            currency=marketplace.currency,
            payable_amount=Decimal("0.00"),
            delayed_amount=Decimal("12.34"),
            settlement_key="fixture-zero-cycle",
            can_submit=False,
            contract_version="fixture",
            page_fingerprint="fixture-dom",
            skip_reason="标准订单可用资金为 0",
        )

    async def capture_evidence(self, *args, **kwargs):
        return None


class Sessions:
    @asynccontextmanager
    async def financial_session(self, selector, store_key=None):
        yield type("Handle", (), {"page": object()})()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_site_status", "expected_plan_lines"),
    (
        (RunMode.DRY_RUN, SiteStatus.DRY_RUN_COMPLETE, 1),
        (RunMode.APPROVAL, SiteStatus.SKIPPED, 0),
        (RunMode.AUTO, SiteStatus.SKIPPED, 0),
    ),
)
async def test_all_modes_persist_read_financials_for_database_notice(
    mode: RunMode,
    expected_site_status: SiteStatus,
    expected_plan_lines: int,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    run_id = f"financial-report-{mode.value}"
    with factory() as session:
        store = Store(
            name="Store",
            selector_type="oauth",
            selector_value="selector",
            browser_oauth="selector",
            expected_seller_id="SELLER",
            identity_confirmed=True,
            enabled=True,
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
        session.add(
            Run(
                id=run_id,
                store_id=store.id,
                workflow="amazon_disbursement",
                mode=mode.value,
                status=RunStatus.QUEUED.value,
                trigger="manual",
            )
        )
        session.commit()
        store_id = store.id
        market_id = market.id

    run = WorkflowRun(
        id=run_id,
        workflow="amazon_disbursement",
        mode=mode,
        store=StoreRef(
            id=str(store_id),
            name="Store",
            selector_type="oauth",
            selector_value="selector",
            expected_seller_id="SELLER",
            identity_confirmed=True,
        ),
        marketplaces=(
            MarketplaceRef(
                id=str(market_id),
                code="CA",
                domain="sellercentral.amazon.ca",
                currency="CAD",
            ),
        ),
    )
    repository = SqlAlchemyWorkflowRepository(factory)
    workflow = AmazonDisbursementWorkflow(
        repository=repository,
        page_adapter=ZeroBalanceAdapter(),
    )
    service = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repository,
        browser_sessions=Sessions(),
    )

    result = await service.start(run)

    assert result.status is RunStatus.SUCCEEDED
    assert len(result.plan.lines) == expected_plan_lines
    with factory() as session:
        site = session.scalar(select(SiteRun).where(SiteRun.run_id == run_id))
        assert site is not None
        assert site.status == expected_site_status.value
        assert site.currency == "CAD"
        assert site.payable_amount == Decimal("0.00")
        assert site.delayed_amount == Decimal("12.34")
        assert session.scalar(
            select(ApprovalRequest).where(ApprovalRequest.run_id == run_id)
        ) is None
        assert session.scalar(
            select(OperationGuard).where(OperationGuard.run_id == run_id)
        ) is None

    notice = DatabaseNoticeBuilder(factory).build(
        run_id, NotificationKind.RUN_COMPLETED
    )
    assert notice is not None
    assert len(notice.sites) == 1
    assert notice.sites[0].code == "CA"
    assert notice.sites[0].currency == "CAD"
    assert notice.sites[0].payable == Decimal("0.00")
    assert notice.sites[0].delayed == Decimal("12.34")
    assert notice.sites[0].outcome == expected_site_status.value
