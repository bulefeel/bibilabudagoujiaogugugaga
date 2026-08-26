from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import asyncio

import pytest
from sqlalchemy.orm import Session

from ziniao_automation.composition import RuntimeComposition
from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import (
    OperationGuard,
    Run,
    Schedule,
    ScheduleBatch,
    SiteRun,
    Store,
    StoreMarketplace,
)
from ziniao_automation.repositories import ScheduleRepository, WorkflowRepository
from ziniao_automation.scheduler import ScheduleManager
from ziniao_automation.workflows.registry import (
    EmptyWorkflowConfig,
    WorkflowDefinition,
    WorkflowExecutionClass,
    WorkflowRegistry,
)
from ziniao_automation.workflows.types import RunMode


class FakeAutomation:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.events: list[str] = []

    async def enqueue_run(self, run_id: str) -> None:
        self.enqueued.append(run_id)

    async def recover_startup(self) -> None:
        self.events.append("recover")

    async def wait_idle(self) -> None:
        self.events.append("idle")


class FakeJob:
    def __init__(self, job_id: str, kwargs: dict) -> None:
        self.id = job_id
        self.kwargs = kwargs
        self.next_run_time = datetime(2026, 8, 13, 1, 30, tzinfo=timezone.utc)


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.started_paused = False
        self.resumed = False
        self.shutdown_called = False

    def start(self, *, paused: bool = False) -> None:
        self.started_paused = paused

    def resume(self) -> None:
        self.resumed = True

    def shutdown(self, *, wait: bool = False) -> None:
        self.shutdown_called = True

    def add_job(self, func, **kwargs):
        job = FakeJob(kwargs["id"], {"func": func, **kwargs})
        self.jobs[job.id] = job
        return job

    def get_jobs(self):
        return list(self.jobs.values())

    def remove_job(self, job_id: str):
        self.jobs.pop(job_id, None)


@pytest.fixture()
def db_env(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'scheduler.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    yield settings, factory
    engine.dispose()


def seed_store(factory, *, qualified: bool = False) -> tuple[int, int]:
    with factory() as db:
        store = Store(
            name="Schedule Store",
            selector_type="oauth",
            selector_value="oauth-schedule",
            browser_oauth="oauth-schedule",
            expected_seller_id="SELLER-SCHEDULE",
            identity_confirmed=True,
            enabled=True,
        )
        db.add(store)
        db.flush()
        market = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        db.add(market)
        if qualified:
            for index in range(2):
                db.add(
                    Run(
                        store_id=store.id,
                        workflow="amazon_disbursement",
                        mode="approval",
                        trigger="manual",
                        status="SUCCEEDED",
                        requested_by="admin",
                        started_at=datetime(2026, 8, 10 + index, tzinfo=timezone.utc),
                        finished_at=datetime(2026, 8, 10 + index, 1, tzinfo=timezone.utc),
                    )
                )
        db.commit()
        return store.id, market.id


def test_schedule_rebuild_options_and_api_refresh_projection(db_env):
    asyncio.run(_schedule_rebuild_options_and_api_refresh_projection(db_env))


async def _schedule_rebuild_options_and_api_refresh_projection(db_env):
    _, factory = db_env
    store_id, _ = seed_store(factory)
    with factory() as db:
        schedule = ScheduleRepository(db).create(
            store_id=store_id,
            name="工作日检查",
            workflow="amazon_disbursement",
            mode="dry_run",
            local_time="09:30",
            days_of_week="mon,tue,wed,thu,fri",
            timezone="Asia/Singapore",
            marketplace_codes=["CA"],
            enabled=True,
            misfire_grace_seconds=1800,
        )
        db.commit()
        schedule_id = schedule.id
    fake_scheduler = FakeScheduler()
    manager = ScheduleManager(factory, FakeAutomation(), scheduler=fake_scheduler)
    await manager.start()
    assert fake_scheduler.started_paused is True
    assert fake_scheduler.resumed is True
    job = fake_scheduler.jobs[f"db-schedule:{schedule_id}"]
    assert job.kwargs["coalesce"] is True
    assert job.kwargs["max_instances"] == 1
    assert job.kwargs["misfire_grace_time"] == 1800
    with factory() as db:
        next_run_at = db.get(Schedule, schedule_id).next_run_at
        assert next_run_at is not None
        assert next_run_at == datetime(2026, 8, 13, 1, 30)

    with factory() as db:
        db.get(Schedule, schedule_id).enabled = False
        db.commit()
    projection = await manager.refresh_schedule(schedule_id)
    assert projection.projected_schedule_ids == frozenset()
    assert projection.failed_schedule_ids == frozenset()
    assert fake_scheduler.jobs == {}
    with factory() as db:
        assert db.get(Schedule, schedule_id).next_run_at is None
    await manager.shutdown()
    assert fake_scheduler.shutdown_called


def test_schedule_projection_normalizes_zoned_occurrence_to_utc(db_env):
    asyncio.run(_schedule_projection_normalizes_zoned_occurrence_to_utc(db_env))


async def _schedule_projection_normalizes_zoned_occurrence_to_utc(db_env):
    _, factory = db_env
    store_id, _ = seed_store(factory)
    with factory() as db:
        schedule = ScheduleRepository(db).create(
            store_id=store_id,
            name="UTC projection",
            workflow="amazon_disbursement",
            mode="dry_run",
            local_time="09:30",
            days_of_week="mon,tue,wed,thu,fri",
            timezone="Asia/Singapore",
            marketplace_codes=["CA"],
            enabled=True,
            misfire_grace_seconds=1800,
        )
        db.commit()
        schedule_id = schedule.id

    scheduler = FakeScheduler()
    manager = ScheduleManager(factory, FakeAutomation(), scheduler=scheduler)
    # Mirror CronTrigger: it exposes 09:30 in the configured +08:00 zone.
    original_add_job = scheduler.add_job

    def add_zoned_job(func, **kwargs):
        job = original_add_job(func, **kwargs)
        job.next_run_time = datetime(
            2026, 8, 14, 9, 30, tzinfo=timezone(timedelta(hours=8))
        )
        return job

    scheduler.add_job = add_zoned_job
    await manager.start()
    with factory() as db:
        # SQLite stores UTC without tzinfo; 09:30 +08:00 is 01:30 UTC.
        assert db.get(Schedule, schedule_id).next_run_at == datetime(2026, 8, 14, 1, 30)
    await manager.shutdown()


def test_schedule_trigger_is_single_instance(db_env):
    asyncio.run(_schedule_trigger_is_single_instance(db_env))


async def _schedule_trigger_is_single_instance(db_env):
    _, factory = db_env
    store_id, _ = seed_store(factory)
    with factory() as db:
        schedule = ScheduleRepository(db).create(
            store_id=store_id,
            name="每日检查",
            workflow="amazon_disbursement",
            mode="dry_run",
            local_time="09:00",
            days_of_week="*",
            timezone="Asia/Singapore",
            marketplace_codes=["CA"],
            enabled=True,
            misfire_grace_seconds=1800,
        )
        db.commit()
        schedule_id = schedule.id
    automation = FakeAutomation()
    manager = ScheduleManager(factory, automation, scheduler=FakeScheduler())
    first = await manager.trigger_schedule(schedule_id)
    second = await manager.trigger_schedule(schedule_id)
    assert first is not None
    assert second is None
    assert automation.enqueued == [first]
    with factory() as db:
        rows = db.query(Run).filter(Run.schedule_id == schedule_id).all()
        assert len(rows) == 1
        assert rows[0].result_summary["requested_marketplaces"] == ["CA"]


class BatchAutomation(FakeAutomation):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[tuple[str, ...]] = []

    async def enqueue_runs(self, run_ids) -> None:
        self.batches.append(tuple(run_ids))


def _seed_two_store_batch(factory):
    with factory() as db:
        batch = ScheduleBatch(
            request_id="00000000-0000-4000-8000-000000000001",
            definition_hash="b" * 64,
            definition_json={},
            schedule_count=2,
        )
        db.add(batch)
        db.flush()
        schedule_ids: list[int] = []
        for order in (1, 2):
            store = Store(
                name=f"Batch Store {order}",
                selector_type="oauth",
                selector_value=f"batch-oauth-{order}",
                browser_oauth=f"batch-oauth-{order}",
                expected_seller_id=f"SELLER-{order}",
                identity_confirmed=True,
                enabled=True,
            )
            db.add(store)
            db.flush()
            db.add(
                StoreMarketplace(
                    store_id=store.id,
                    code="CA",
                    domain="sellercentral.amazon.ca",
                    currency="CAD",
                    enabled=True,
                )
            )
            schedule = Schedule(
                store_id=store.id,
                batch_id=batch.id,
                batch_order=order,
                name="same batch",
                workflow="amazon_disbursement",
                mode="dry_run",
                local_time="09:00",
                days_of_week="*",
                timezone="Asia/Singapore",
                marketplace_codes=["CA"],
                workflow_config={"marketplace_codes": ["CA"]},
                enabled=True,
            )
            db.add(schedule)
            db.flush()
            schedule_ids.append(schedule.id)
        db.commit()
        return schedule_ids


def test_same_occurrence_batch_is_created_and_enqueued_once(db_env):
    _, factory = db_env
    schedule_ids = _seed_two_store_batch(factory)
    automation = BatchAutomation()
    manager = ScheduleManager(factory, automation, scheduler=FakeScheduler())
    occurrence = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)

    first = asyncio.run(
        manager.trigger_schedule(schedule_ids[0], scheduled_for_at=occurrence)
    )

    assert first is not None
    assert len(automation.batches) == 1
    assert len(automation.batches[0]) == 2
    with factory() as db:
        runs = list(
            db.query(Run)
            .filter(Run.schedule_id.in_(schedule_ids))
            .order_by(Run.schedule_id)
        )
        assert len(runs) == 2
    # The second APScheduler callback observes both persisted occurrences and
    # must not wake/enqueue a second time.
    assert (
        asyncio.run(
            manager.trigger_schedule(schedule_ids[1], scheduled_for_at=occurrence)
        )
        is None
    )
    assert len(automation.batches) == 1


def test_scheduler_identity_gate_follows_generic_workflow_definition(db_env):
    _, factory = db_env
    with factory() as db:
        store = Store(
            name="Read only store",
            selector_type="id",
            selector_value="generic-profile",
            expected_seller_id=None,
            identity_confirmed=False,
            enabled=True,
        )
        db.add(store)
        db.flush()
        schedule = Schedule(
            store_id=store.id,
            name="read-only",
            workflow="future_report",
            mode="dry_run",
            local_time="09:00",
            days_of_week="*",
            timezone="Asia/Singapore",
            marketplace_codes=[],
            workflow_config={},
            enabled=True,
        )
        db.add(schedule)
        db.commit()
        schedule_id = schedule.id

    registry = WorkflowRegistry(
        [
            WorkflowDefinition(
                key="future_report",
                display_name="Future report",
                description="test",
                workflow=None,
                supported_modes=(RunMode.DRY_RUN,),
                default_mode=RunMode.DRY_RUN,
                config_version=1,
                config_model=EmptyWorkflowConfig,
                requires_confirmed_identity=False,
                requires_financial_lock=False,
                execution_class=WorkflowExecutionClass.STANDARD,
            )
        ]
    )
    automation = FakeAutomation()
    manager = ScheduleManager(
        factory,
        automation,
        scheduler=FakeScheduler(),
        workflow_registry=registry,
    )
    run_id = asyncio.run(
        manager.trigger_schedule(
            schedule_id,
            scheduled_for_at=datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
        )
    )
    assert run_id is not None
    with factory() as db:
        assert db.get(Run, run_id).status == "QUEUED"


def test_auto_mode_no_longer_requires_prior_approval_successes(db_env):
    """The two-approval prerequisite was removed on the operator's instruction.

    A store with no disbursement history at all, and a store whose most recent
    run was a PARTIAL, may both enable automatic mode.  Confirmed identity is
    still the gate, and every per-run financial guard is untouched.
    """

    _, factory = db_env
    store_id, _ = seed_store(factory)
    with factory() as db:
        schedule = ScheduleRepository(db).create(
            store_id=store_id,
            name="无历史也可自动",
            workflow="amazon_disbursement",
            mode="auto",
            local_time="10:00",
            marketplace_codes=["CA"],
            enabled=True,
        )
        assert schedule.mode == "auto"
        db.rollback()
    with factory() as db:
        db.add(
            Run(
                store_id=store_id,
                workflow="amazon_disbursement",
                mode="approval",
                trigger="manual",
                status="PARTIAL",
                requested_by="admin",
                finished_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
            )
        )
        db.commit()
    with factory() as db:
        ScheduleRepository(db).create(
            store_id=store_id,
            name="上一轮 PARTIAL 也不再阻挡",
            workflow="amazon_disbursement",
            mode="auto",
            local_time="10:00",
            marketplace_codes=["CA"],
            enabled=True,
        )
        draft = ScheduleRepository(db).create(
            store_id=store_id,
            name="草稿可以直接启用",
            workflow="amazon_disbursement",
            mode="auto",
            local_time="10:00",
            marketplace_codes=["CA"],
            enabled=False,
        )
        enabled = ScheduleRepository(db).update(draft.id, enabled=True)
        assert enabled.enabled is True


def test_auto_mode_still_requires_a_confirmed_seller_identity(db_env):
    """Removing the approval prerequisite must not open the identity gate."""

    _, factory = db_env
    store_id, _ = seed_store(factory)
    with factory() as db:
        store = db.get(Store, store_id)
        # Store availability is workflow-neutral; the financial definition
        # must still reject this unconfirmed seller independently.
        store.enabled = True
        store.identity_confirmed = False
        db.commit()
    with factory() as db:
        with pytest.raises(ValueError, match="未确认身份"):
            ScheduleRepository(db).create(
                store_id=store_id,
                name="身份未确认",
                workflow="amazon_disbursement",
                mode="auto",
                local_time="10:00",
                marketplace_codes=["CA"],
                enabled=True,
            )
        with pytest.raises(ValueError, match="未绑定并确认卖家身份"):
            WorkflowRepository(db).create_run(
                store_id=store_id,
                workflow="amazon_disbursement",
                mode="auto",
            )


def test_manual_auto_run_no_longer_needs_prior_approvals(db_env):
    _, factory = db_env
    store_id, _ = seed_store(factory)
    with factory() as db:
        run = WorkflowRepository(db).create_run(
            store_id=store_id,
            workflow="amazon_disbursement",
            mode="auto",
        )
        assert run.mode == "auto"


def test_runtime_recovers_before_scheduler_and_closes_in_order(db_env):
    asyncio.run(_runtime_recovers_before_scheduler_and_closes_in_order(db_env))


async def _runtime_recovers_before_scheduler_and_closes_in_order(db_env):
    settings, factory = db_env
    events: list[str] = []

    class Automation(FakeAutomation):
        async def recover_startup(self):
            events.append("recover")
        async def wait_idle(self): events.append("idle")
        async def shutdown_identity_probes(self): events.append("probe-stop")

    class Manager:
        async def start(self): events.append("scheduler-start")
        async def shutdown(self, wait=False): events.append("scheduler-stop")

    class Controller:
        async def close(self): events.append("controller-close")

    composition = RuntimeComposition(
        settings=settings,
        session_factory=factory,
        controller=Controller(),
        workflow_repository=object(),
        workflow_engine=object(),
        run_loader=object(),
        automation_service=Automation(),
        schedule_manager=Manager(),
    )
    await composition.start()
    await composition.close()
    assert events == [
        "recover",
        "scheduler-start",
        "scheduler-stop",
        "idle",
        "probe-stop",
        "controller-close",
    ]


def test_recovery_loader_only_selects_financial_guards(db_env):
    asyncio.run(_recovery_loader_only_selects_financial_guards(db_env))


async def _recovery_loader_only_selects_financial_guards(db_env):
    from ziniao_automation.workflows.runtime import DatabaseRunLoader

    settings, factory = db_env
    store_id, market_id = seed_store(factory)
    with factory() as db:
        repo = WorkflowRepository(db)
        guarded = repo.create_run(store_id=store_id, workflow="amazon_disbursement", mode="approval")
        site = repo.save_site_plan(
            run_id=guarded.id,
            marketplace_id=market_id,
            marketplace_code="CA",
            currency="CAD",
            payable_amount=10,
            delayed_amount=0,
            settlement_key="cycle-1",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
        repo.arm_operation(
            guard_key="guard-cycle-1",
            run_id=guarded.id,
            site_run_id=site.id,
            store_id=store_id,
            workflow="amazon_disbursement",
            marketplace_code="CA",
            settlement_key="cycle-1",
            amount=10,
            currency="CAD",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
        normal = repo.create_run(store_id=store_id, workflow="amazon_disbursement", mode="dry_run")
        db.commit()
    loader = DatabaseRunLoader(factory, artifact_root=settings.evidence_dir)
    recovered = await loader.recovery_runs()
    assert [run.id for run in recovered] == [guarded.id]
    assert normal.id not in [run.id for run in recovered]


def test_startup_crash_recovery_classification_and_actions(db_env):
    asyncio.run(_startup_crash_recovery_classification_and_actions(db_env))


async def _startup_crash_recovery_classification_and_actions(db_env):
    from ziniao_automation.workflows.runtime import AutomationService, DatabaseRunLoader

    settings, factory = db_env
    store_id, market_id = seed_store(factory)
    with factory() as db:
        def add_run(status: str, mode: str = "dry_run") -> Run:
            row = Run(
                store_id=store_id,
                workflow="amazon_disbursement",
                mode=mode,
                trigger="manual",
                status=status,
                requested_by="admin",
            )
            db.add(row)
            db.flush()
            return row

        queued = add_run("QUEUED")
        running = add_run("RUNNING")
        waiting_auth = add_run("WAITING_AUTH")
        waiting_approval = add_run("WAITING_APPROVAL", "approval")
        financial = add_run("RUNNING", "approval")
        financial_auth = add_run("WAITING_AUTH", "approval")
        site = SiteRun(
            run_id=financial.id,
            marketplace_id=market_id,
            marketplace_code="CA",
            status="ARMED",
            currency="CAD",
            payable_amount=10,
            delayed_amount=0,
            settlement_key="financial-cycle",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
        db.add(site)
        db.flush()
        db.add(
            OperationGuard(
                guard_key="financial-guard",
                run_id=financial.id,
                site_run_id=site.id,
                store_id=store_id,
                workflow="amazon_disbursement",
                marketplace_code="CA",
                settlement_key="financial-cycle",
                state="ARMED",
                amount=10,
                currency="CAD",
                plan_hash="a" * 64,
                snapshot_hash="b" * 64,
            )
        )
        auth_site = SiteRun(
            run_id=financial_auth.id,
            marketplace_id=market_id,
            marketplace_code="CA",
            status="WAITING_AUTH",
            currency="CAD",
            payable_amount=12,
            delayed_amount=0,
            settlement_key="financial-auth-cycle",
            plan_hash="c" * 64,
            snapshot_hash="d" * 64,
        )
        db.add(auth_site)
        db.flush()
        db.add(
            OperationGuard(
                guard_key="financial-auth-guard",
                run_id=financial_auth.id,
                site_run_id=auth_site.id,
                store_id=store_id,
                workflow="amazon_disbursement",
                marketplace_code="CA",
                settlement_key="financial-auth-cycle",
                state="SUBMITTED",
                amount=12,
                currency="CAD",
                plan_hash="c" * 64,
                snapshot_hash="d" * 64,
            )
        )
        db.commit()
        ids = SimpleNamespace(
            queued=queued.id,
            running=running.id,
            waiting_auth=waiting_auth.id,
            waiting_approval=waiting_approval.id,
            financial=financial.id,
            financial_auth=financial_auth.id,
        )

    loader = DatabaseRunLoader(factory, artifact_root=settings.evidence_dir)
    plan = await loader.startup_recovery_plan()
    assert {run.id for run in plan.financial} == {ids.financial, ids.financial_auth}
    assert plan.queued_ids == (ids.queued,)
    assert plan.running_ids == (ids.running,)
    assert plan.waiting_auth_ids == (ids.waiting_auth,)
    assert ids.waiting_approval not in {
        *plan.queued_ids, *plan.running_ids, *plan.waiting_auth_ids,
    }

    class Repository:
        def __init__(self): self.cas: list[tuple] = []
        async def set_run_status(self, run_id, status, *, allowed_from=None, error=None):
            self.cas.append((run_id, status.value, tuple(x.value for x in allowed_from or ())))
            with factory() as db:
                row = db.get(Run, run_id)
                if row.status not in {x.value for x in allowed_from or ()}:
                    return False
                row.status = status.value
                db.commit()
                return True

    class Engine:
        def __init__(self):
            self.repository = Repository()
            self.started: list[str] = []
            self.reconciled: list[str] = []
            self.expired: list[str] = []
        async def start(self, run):
            self.started.append(run.id)
        async def reconcile(self, run):
            self.reconciled.append(run.id)
        async def expire_auth(self, run):
            self.expired.append(run.id)
            return await self.repository.set_run_status(
                run.id,
                __import__("ziniao_automation.workflows.types", fromlist=["RunStatus"]).RunStatus.NEEDS_HUMAN_AUTH,
                allowed_from=(__import__("ziniao_automation.workflows.types", fromlist=["RunStatus"]).RunStatus.WAITING_AUTH,),
            )

    class Controller:
        pass

    engine = Engine()
    service = AutomationService(
        engine=engine,
        run_loader=loader,
        ziniao_controller=Controller(),
        recovery_loader=loader.recovery_runs,
        session_factory=factory,
    )
    await service.recover_startup()
    await service.wait_idle()
    assert set(engine.reconciled) == {ids.financial, ids.financial_auth}
    assert set(engine.started) == {ids.queued, ids.running}
    assert len(engine.started) == 2
    # DatabaseRunLoader uses its direct durable marker; no browser/engine
    # preflight is needed just to record that the old auth lease vanished.
    assert engine.expired == []
    with factory() as db:
        assert db.get(Run, ids.running).status == "QUEUED"
        assert db.get(Run, ids.waiting_auth).status == "NEEDS_HUMAN_AUTH"
        assert db.get(Run, ids.waiting_approval).status == "WAITING_APPROVAL"
        # Guarded RUNNING was reconciled, never reset/re-enqueued.
        assert db.get(Run, ids.financial).status == "RUNNING"
        assert db.get(Run, ids.financial_auth).status == "WAITING_AUTH"
