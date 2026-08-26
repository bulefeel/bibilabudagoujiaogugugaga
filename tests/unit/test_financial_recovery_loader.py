from __future__ import annotations

from pathlib import Path

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import (
    OperationGuard,
    Run,
    RunEvent,
    RunQueueEntry,
    SiteRun,
    Store,
    StoreMarketplace,
)
from ziniao_automation.queue import DurableRunQueue
from ziniao_automation.workflows.amazon_disbursement import (
    build_amazon_disbursement_definition,
)
from ziniao_automation.workflows.registry import WorkflowRegistry
from ziniao_automation.workflows.runtime import AutomationService, DatabaseRunLoader


@pytest.fixture()
def recovery_database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'recovery.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    sessions = make_session_factory(engine)
    yield settings, sessions
    engine.dispose()


def _add_guarded_run(
    session,
    *,
    store: Store,
    marketplace: StoreMarketplace,
    workflow: str = "amazon_disbursement",
    config_version: int = 1,
    config: dict | None = None,
) -> str:
    run = Run(
        store_id=store.id,
        workflow=workflow,
        mode="approval",
        trigger="manual",
        status="RUNNING",
        requested_by="admin",
        workflow_config=config or {"marketplace_codes": ["CA"]},
        workflow_config_version=config_version,
    )
    session.add(run)
    session.flush()
    site = SiteRun(
        run_id=run.id,
        marketplace_id=marketplace.id,
        marketplace_code="CA",
        status="ARMED",
        currency="CAD",
        payable_amount=10,
        delayed_amount=0,
        settlement_key=f"cycle-{run.id}",
        plan_hash="a" * 64,
        snapshot_hash="b" * 64,
    )
    session.add(site)
    session.flush()
    session.add(
        OperationGuard(
            guard_key=f"guard-{run.id}",
            run_id=run.id,
            site_run_id=site.id,
            store_id=store.id,
            workflow=workflow,
            marketplace_code="CA",
            settlement_key=f"cycle-{run.id}",
            state="ARMED",
            amount=10,
            currency="CAD",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
    )
    session.flush()
    return run.id


@pytest.mark.asyncio
async def test_financial_recovery_uses_guards_and_isolates_bad_legacy_run(
    recovery_database,
) -> None:
    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store = Store(
            name="Recovery store",
            selector_type="oauth",
            selector_value="oauth-recovery",
            browser_oauth="oauth-recovery",
            expected_seller_id="SELLER-RECOVERY",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        marketplace = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        session.add(marketplace)
        session.flush()
        current_id = _add_guarded_run(
            session, store=store, marketplace=marketplace
        )
        # A contradictory terminal run status must not suppress an active
        # financial guard's platform read-back.
        session.get(Run, current_id).status = "SUCCEEDED"
        old_id = _add_guarded_run(
            session,
            store=store,
            marketplace=marketplace,
            config_version=99,
            config={"removed_field": "legacy"},
        )
        broken_id = _add_guarded_run(
            session,
            store=store,
            marketplace=marketplace,
            workflow="retired_financial_workflow",
            config_version=7,
            config={"anything": True},
        )
        settled_id = _add_guarded_run(
            session, store=store, marketplace=marketplace
        )
        session.get(Run, settled_id).status = "RECONCILING"
        session.query(OperationGuard).filter_by(run_id=settled_id).one().state = (
            "CONFIRMED"
        )
        session.query(SiteRun).filter_by(run_id=settled_id).one().status = "CONFIRMED"
        session.commit()

    queue = DurableRunQueue(sessions)
    current_entry, _ = queue.enqueue(current_id)
    old_entry, _ = queue.enqueue(old_id)
    broken_entry, _ = queue.enqueue(broken_id)
    settled_entry, _ = queue.enqueue(settled_id, action="RECONCILE")
    with sessions() as session:
        for entry_id in (current_entry, old_entry, broken_entry, settled_entry):
            entry = session.get(RunQueueEntry, entry_id)
            entry.state = "CLAIMED"
            entry.claim_token = f"claim-{entry_id}"
        session.commit()

    loader = DatabaseRunLoader(
        sessions,
        artifact_root=settings.evidence_dir,
        workflow_registry=registry,
    )
    plan = await loader.startup_recovery_plan()

    assert set(plan.financial_guarded_ids) == {current_id, old_id, broken_id}
    assert plan.financial_failed_ids == (broken_id,)
    assert plan.reconciling_ids == (settled_id,)
    assert {run.id for run in plan.financial} == {current_id, old_id}
    old_snapshot = next(run for run in plan.financial if run.id == old_id)
    assert old_snapshot.workflow_config == {"marketplace_codes": ["CA"]}
    assert old_snapshot.workflow_config_version == 99
    assert [market.code for market in old_snapshot.marketplaces] == ["CA"]

    class Engine:
        def __init__(self) -> None:
            self.registry = registry
            self.repository = object()
            self.reconciled: list[str] = []

        async def reconcile(self, run) -> None:
            self.reconciled.append(run.id)
            # Model the real engine's terminal transition after it observes
            # that every historical guard is already CONFIRMED.  This keeps
            # the test focused on startup queue routing while proving a
            # RECONCILING row cannot remain stuck forever after restart.
            with sessions() as session:
                row = session.get(Run, run.id)
                if row is not None and row.status == "RECONCILING":
                    row.status = "SUCCEEDED"
                    session.commit()

    engine = Engine()
    service = AutomationService(
        engine=engine,
        run_loader=loader,
        ziniao_controller=object(),
        session_factory=sessions,
    )
    await service.recover_startup()
    await service.wait_idle()
    await service.shutdown_worker()

    assert set(engine.reconciled) == {current_id, old_id, settled_id}
    with sessions() as session:
        assert session.get(RunQueueEntry, current_entry).state == "DONE"
        assert session.get(RunQueueEntry, old_entry).state == "DONE"
        assert session.get(RunQueueEntry, broken_entry).state == "CANCELLED"
        assert session.get(RunQueueEntry, settled_entry).state == "DONE"
        broken = session.get(Run, broken_id)
        assert broken.status == "UNCERTAIN_FINANCIAL"
        assert "人工核查" in broken.error
        assert session.get(Run, settled_id).status == "SUCCEEDED"
        assert session.query(RunEvent).filter_by(
            run_id=broken_id,
            event_type="FINANCIAL_RECOVERY_LOAD_FAILED",
        ).count() >= 1


@pytest.mark.parametrize(
    "crash_status",
    (
        "RUNNING",
        "QUEUED",
        "WAITING_APPROVAL",
        "WAITING_AUTH",
        "NEEDS_HUMAN_AUTH",
    ),
)
@pytest.mark.asyncio
async def test_confirmed_guard_crash_leftover_only_reconciles(
    recovery_database,
    crash_status: str,
) -> None:
    """A final CONFIRMED guard must still block ordinary startup replay."""

    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store = Store(
            name=f"Confirmed guard {crash_status}",
            selector_type="oauth",
            selector_value=f"oauth-{crash_status.lower()}",
            browser_oauth=f"oauth-{crash_status.lower()}",
            expected_seller_id="SELLER-CONFIRMED-GUARD",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        marketplace = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        session.add(marketplace)
        session.flush()
        run_id = _add_guarded_run(
            session,
            store=store,
            marketplace=marketplace,
        )
        session.get(Run, run_id).status = crash_status
        session.query(OperationGuard).filter_by(run_id=run_id).one().state = (
            "CONFIRMED"
        )
        session.query(SiteRun).filter_by(run_id=run_id).one().status = "CONFIRMED"
        session.commit()

    loader = DatabaseRunLoader(
        sessions,
        artifact_root=settings.evidence_dir,
        workflow_registry=registry,
    )
    plan = await loader.startup_recovery_plan()

    assert plan.financial_guarded_ids == ()
    assert plan.reconciling_ids == (run_id,)
    assert run_id not in plan.running_ids
    assert run_id not in plan.queued_ids
    assert run_id not in plan.waiting_auth_ids

    class Repository:
        async def set_run_status(self, *_args, **_kwargs) -> bool:
            # This is only reachable with the unsafe pre-fix RUNNING routing.
            return True

    class Engine:
        def __init__(self) -> None:
            self.registry = registry
            self.repository = Repository()
            self.start_calls: list[str] = []
            self.reconcile_calls: list[str] = []

        async def start(self, run) -> None:
            self.start_calls.append(run.id)

        async def reconcile(self, run) -> None:
            self.reconcile_calls.append(run.id)
            with sessions() as session:
                row = session.get(Run, run.id)
                row.status = "SUCCEEDED"
                session.commit()

    engine = Engine()
    service = AutomationService(
        engine=engine,
        run_loader=loader,
        ziniao_controller=object(),
        session_factory=sessions,
    )
    try:
        await service.recover_startup()
        await service.wait_idle()
    finally:
        await service.shutdown_worker()

    assert engine.start_calls == []
    assert engine.reconcile_calls == [run_id]
    with sessions() as session:
        run = session.get(Run, run_id)
        entry = session.query(RunQueueEntry).filter_by(run_id=run_id).one()
        assert run.status == "SUCCEEDED"
        assert entry.action == "RECONCILE"
        assert entry.state == "DONE"
        assert session.query(OperationGuard).filter_by(run_id=run_id).count() == 1


@pytest.mark.asyncio
async def test_cancel_does_not_remove_reconcile_queue_entry(recovery_database) -> None:
    """A restart-created RECONCILE item must survive a cancel request.

    Confirmed guards are no longer "active" in the narrow ARMED/SUBMITTED /
    UNCERTAIN sense, but the durable queue action still represents required
    financial read-back.  The runtime must reject cancellation before calling
    either the controller signal or ``cancel_ready``.
    """

    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store = Store(
            name="Reconcile cancellation store",
            selector_type="oauth",
            selector_value="oauth-reconcile-cancel",
            browser_oauth="oauth-reconcile-cancel",
            expected_seller_id="SELLER-RECONCILE-CANCEL",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        marketplace = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        session.add(marketplace)
        session.flush()
        run_id = _add_guarded_run(
            session, store=store, marketplace=marketplace
        )
        session.get(Run, run_id).status = "RECONCILING"
        session.query(OperationGuard).filter_by(run_id=run_id).one().state = (
            "CONFIRMED"
        )
        session.query(SiteRun).filter_by(run_id=run_id).one().status = "CONFIRMED"
        session.commit()

    queue = DurableRunQueue(sessions)
    entry_id, created = queue.enqueue(run_id, action="RECONCILE")
    assert created is True

    class Controller:
        def __init__(self) -> None:
            self.cancel_calls = 0

        async def cancel_auth(self, _run_id: str) -> bool:
            self.cancel_calls += 1
            return True

    class Engine:
        def __init__(self) -> None:
            self.cancel_calls = 0

        async def cancel(self, _run) -> None:
            self.cancel_calls += 1

    controller = Controller()
    engine = Engine()
    loader = DatabaseRunLoader(
        sessions,
        artifact_root=settings.evidence_dir,
        workflow_registry=registry,
    )
    service = AutomationService(
        engine=engine,
        run_loader=loader,
        ziniao_controller=controller,
        session_factory=sessions,
    )

    with pytest.raises(RuntimeError, match="回读队列"):
        await service.cancel_run(run_id)

    assert controller.cancel_calls == 0
    assert engine.cancel_calls == 0
    with sessions() as session:
        assert session.get(RunQueueEntry, entry_id).state == "READY"
