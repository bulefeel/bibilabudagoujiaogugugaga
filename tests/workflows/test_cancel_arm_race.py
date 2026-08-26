from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ziniao_automation.db import Base
from ziniao_automation.models import (
    OperationGuard,
    Run,
    RunQueueEntry,
    Store,
    StoreMarketplace,
)
from ziniao_automation.workflows.amazon_disbursement import (
    AmazonDisbursementWorkflow,
    DisbursementPolicy,
)
from ziniao_automation.workflows.engine import WorkflowEngine
from ziniao_automation.workflows.errors import InvalidTransition
from ziniao_automation.workflows.registry import WorkflowRegistry
from ziniao_automation.workflows.repository_sqlalchemy import (
    SqlAlchemyWorkflowRepository,
)
from ziniao_automation.workflows.runtime import AutomationService
from ziniao_automation.workflows.types import (
    GuardState,
    MarketplaceRef,
    MarketplaceSnapshot,
    PreflightResult,
    ReconcileResult,
    ReconcileStatus,
    RunMode,
    RunStatus,
    StoreRef,
    SubmissionReceipt,
    WorkflowReport,
    WorkflowRun,
    utc_now,
)


class _Handle:
    page = object()


class _Sessions:
    @asynccontextmanager
    async def financial_session(self, selector, store_key=None):
        del selector, store_key
        yield _Handle()

    async def cancel_auth(self, run_id: str) -> bool:
        del run_id
        return False


class _Collector:
    def __init__(self) -> None:
        self.reports: list[WorkflowReport] = []

    async def send(self, report: WorkflowReport) -> None:
        self.reports.append(report)


class _PausingAdapter:
    """A local page fixture whose final submit is only an in-memory counter."""

    def __init__(self, repository: SqlAlchemyWorkflowRepository) -> None:
        self.repository = repository
        self.confirmation_reached = asyncio.Event()
        self.release_confirmation = asyncio.Event()
        self.pause_before_arm = True
        self.cancel_after_fake_submit = False
        self.submit_calls = 0
        self.snapshot = MarketplaceSnapshot(
            marketplace_code="CA",
            domain="sellercentral.amazon.ca",
            seller_id="SELLER-RACE",
            payment_account="",
            currency="CAD",
            payable_amount=Decimal("12.34"),
            delayed_amount=Decimal("1.00"),
            settlement_key="fixture-cycle",
            can_submit=True,
            contract_version="fixture-v1",
            page_fingerprint="fixture-dom",
        )

    async def preflight(self, page, run, marketplace):
        del page
        return PreflightResult(
            marketplace.code,
            run.store.expected_seller_id,
            "",
            marketplace.domain,
            "fixture-v1",
        )

    async def read_snapshot(self, page, run, marketplace):
        del page, run, marketplace
        return self.snapshot

    async def capture_evidence(self, *args, **kwargs):
        del args, kwargs
        return None

    async def lookup_existing(self, page, run, marketplace, expected):
        del page, run, marketplace, expected
        return ReconcileResult(ReconcileStatus.NOT_FOUND, utc_now())

    async def open_confirmation(self, page, run, marketplace, expected):
        del page, run, marketplace
        self.confirmation_reached.set()
        if self.pause_before_arm:
            await self.release_confirmation.wait()
        return replace(expected, payment_account="493")

    async def submit_once(self, page, run, marketplace, expected):
        del page, marketplace, expected
        self.submit_calls += 1
        if self.cancel_after_fake_submit:
            changed = await self.repository.set_run_status(
                run.id,
                RunStatus.CANCELLED,
                allowed_from=(RunStatus.RUNNING,),
            )
            assert changed is True  # explicitly simulates the old late race
        return SubmissionReceipt(utc_now(), receipt_id="fixture-submit")

    async def reconcile(self, page, run, marketplace, operation):
        del page, run, marketplace, operation
        return ReconcileResult(
            ReconcileStatus.CONFIRMED,
            utc_now(),
            platform_reference="fixture-reference",
            platform_status="initiated",
        )


def _environment(tmp_path):
    database = tmp_path / "cancel-arm-race.db"
    sql_engine = create_engine(
        f"sqlite+pysqlite:///{database}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(sql_engine)
    sessions = sessionmaker(sql_engine, expire_on_commit=False)
    with sessions() as session:
        store = Store(
            name="Race store",
            selector_type="oauth",
            selector_value="fixture-oauth",
            browser_oauth="fixture-oauth",
            expected_seller_id="SELLER-RACE",
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
        db_run = Run(
            id="cancel-arm-race",
            store_id=store.id,
            workflow="amazon_disbursement",
            mode="auto",
            status="QUEUED",
            workflow_config={"marketplace_codes": ["CA"]},
            workflow_config_version=1,
        )
        session.add(db_run)
        session.add(
            RunQueueEntry(
                run_id=db_run.id,
                action="START",
                state="CLAIMED",
                priority=10,
                business_priority=1,
                target_order=1,
                claim_token="fixture-claim",
                claimed_at=utc_now(),
            )
        )
        session.commit()
        run = WorkflowRun(
            id=db_run.id,
            workflow=db_run.workflow,
            mode=RunMode.AUTO,
            store=StoreRef(
                id=str(store.id),
                name=store.name,
                selector_type="oauth",
                selector_value=store.selector_value,
                expected_seller_id=store.expected_seller_id or "",
                enabled=True,
                identity_confirmed=True,
            ),
            marketplaces=(
                MarketplaceRef(
                    id=str(market.id),
                    code=market.code,
                    domain=market.domain,
                    currency=market.currency,
                ),
            ),
            workflow_config={"marketplace_codes": ["CA"]},
        )

    repository = SqlAlchemyWorkflowRepository(sessions)
    adapter = _PausingAdapter(repository)
    workflow = AmazonDisbursementWorkflow(
        repository=repository,
        page_adapter=adapter,
        policy=DisbursementPolicy(
            reconcile_attempts=1,
            reconcile_interval_seconds=0,
        ),
    )
    collector = _Collector()
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repository,
        browser_sessions=_Sessions(),
        notifier=collector,
    )

    async def load_run(run_id: str) -> WorkflowRun:
        assert run_id == run.id
        return run

    service = AutomationService(
        engine=engine,
        run_loader=load_run,
        ziniao_controller=_Sessions(),
        session_factory=sessions,
    )
    return sessions, repository, adapter, collector, engine, service, run


@pytest.mark.asyncio
async def test_claimed_running_cancel_is_rejected_before_arm(tmp_path) -> None:
    sessions, repository, adapter, _, engine, service, run = _environment(tmp_path)
    task = asyncio.create_task(engine.start(run))
    await asyncio.wait_for(adapter.confirmation_reached.wait(), timeout=2)

    with pytest.raises(RuntimeError, match="尚未到可安全取消"):
        await service.cancel_run(run.id)

    assert await repository.get_run_status(run.id) is RunStatus.RUNNING
    assert await repository.list_operations(run.id) == ()
    assert adapter.submit_calls == 0

    adapter.release_confirmation.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert result.status is RunStatus.SUCCEEDED
    assert adapter.submit_calls == 1
    with sessions() as session:
        assert session.scalar(
            select(RunQueueEntry.state).where(RunQueueEntry.run_id == run.id)
        ) == "CLAIMED"


@pytest.mark.asyncio
async def test_cancelled_run_cannot_arm_or_reach_fake_submit(tmp_path) -> None:
    sessions, repository, adapter, _, engine, _, run = _environment(tmp_path)
    task = asyncio.create_task(engine.start(run))
    await asyncio.wait_for(adapter.confirmation_reached.wait(), timeout=2)

    assert await repository.set_run_status(
        run.id,
        RunStatus.CANCELLED,
        allowed_from=(RunStatus.RUNNING,),
    )
    adapter.release_confirmation.set()

    with pytest.raises(InvalidTransition, match="RUNNING"):
        await asyncio.wait_for(task, timeout=2)
    assert adapter.submit_calls == 0
    assert await repository.get_run_status(run.id) is RunStatus.CANCELLED
    with sessions() as session:
        assert session.scalar(
            select(OperationGuard.id).where(OperationGuard.run_id == run.id)
        ) is None


@pytest.mark.asyncio
async def test_late_legacy_cancel_is_not_rewritten_as_success(tmp_path) -> None:
    _, repository, adapter, collector, engine, _, run = _environment(tmp_path)
    adapter.pause_before_arm = False
    adapter.cancel_after_fake_submit = True

    result = await engine.start(run)

    assert adapter.submit_calls == 1
    assert result.status is RunStatus.CANCELLED
    assert await repository.get_run_status(run.id) is RunStatus.CANCELLED
    records = await repository.list_operations(run.id)
    assert [record.state for record in records] == [GuardState.CONFIRMED]
    assert collector.reports == []
    assert await repository.set_run_status(
        run.id,
        RunStatus.SUCCEEDED,
        allowed_from=(RunStatus.CANCELLED,),
    ) is False
