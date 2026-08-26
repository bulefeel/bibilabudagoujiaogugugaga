from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import Run, Schedule, ScheduleBatch, Store, StoreMarketplace
from ziniao_automation.repositories import (
    ConflictError,
    ScheduleRepository,
    WorkflowRepository,
)
from ziniao_automation.queue import DurableRunQueue
from ziniao_automation.schedule_batch import (
    BatchScheduleService,
    BatchScheduleValidationError,
)
from ziniao_automation.workflows.amazon_disbursement import (
    build_amazon_disbursement_definition,
)
from ziniao_automation.workflows.registry import (
    WorkflowDefinition,
    WorkflowExecutionClass,
    WorkflowRegistry,
)
from ziniao_automation.workflows.types import RunMode


@pytest.fixture()
def database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'batch.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


def _seed_store(session, number: int, *, enabled: bool = True) -> Store:
    store = Store(
        name=f"Store {number}",
        selector_type="oauth",
        selector_value=f"oauth-{number}-{uuid4()}",
        browser_oauth=f"oauth-{number}-{uuid4()}",
        expected_seller_id=f"SELLER-{number}",
        identity_confirmed=True,
        enabled=enabled,
    )
    session.add(store)
    session.flush()
    session.add_all(
        [
            StoreMarketplace(
                store_id=store.id,
                code="CA",
                domain="sellercentral.amazon.ca",
                currency="CAD",
                enabled=True,
            ),
            StoreMarketplace(
                store_id=store.id,
                code="UK",
                domain="sellercentral.amazon.co.uk",
                currency="GBP",
                enabled=True,
            ),
        ]
    )
    return store


def _template(**changes):
    value = {
        "name": "工作日提现",
        "workflow": "amazon_disbursement",
        "mode": "dry_run",
        "workflow_config": {"marketplace_codes": ["CA", "UK"]},
        "local_time": "09:00",
        "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
        "timezone": "Asia/Singapore",
        "enabled": True,
    }
    value.update(changes)
    return value


def _registry() -> WorkflowRegistry:
    return WorkflowRegistry((build_amazon_disbursement_definition(),))


def test_batch_creates_one_independent_schedule_per_store_and_is_idempotent(database):
    with database() as session:
        stores = [_seed_store(session, index) for index in range(1, 6)]
        session.commit()
        ids = [store.id for store in stores]
        service = BatchScheduleService(session, _registry())

        preview = service.preview(store_ids=ids, template=_template())
        assert preview["eligible"] is True
        assert preview["created_count"] == 5
        assert [target["order"] for target in preview["targets"]] == [1, 2, 3, 4, 5]

        request_id = str(uuid4())
        result = service.create(
            request_id=request_id, store_ids=ids, template=_template()
        )
        session.commit()
        assert result["status"] == "created"
        assert result["created_count"] == 5

        schedules = list(
            session.scalars(
                select(Schedule).order_by(Schedule.batch_order, Schedule.id)
            )
        )
        assert [row.store_id for row in schedules] == ids
        assert [row.batch_order for row in schedules] == [1, 2, 3, 4, 5]
        assert all(row.workflow_config == {"marketplace_codes": ["CA", "UK"]} for row in schedules)
        assert all(row.marketplace_codes == ["CA", "UK"] for row in schedules)

        retry = service.create(
            request_id=request_id, store_ids=ids, template=_template()
        )
        session.commit()
        assert retry["status"] == "existing"
        assert retry["batch_id"] == result["batch_id"]
        assert session.scalar(select(func.count(ScheduleBatch.id))) == 1
        assert session.scalar(select(func.count(Schedule.id))) == 5

        with pytest.raises(ConflictError, match="request_id"):
            service.create(
                request_id=request_id,
                store_ids=ids,
                template=_template(name="另一份定义"),
            )


def test_one_invalid_store_rejects_the_whole_batch_without_writes(database):
    with database() as session:
        good = _seed_store(session, 1)
        disabled = _seed_store(session, 2, enabled=False)
        session.commit()
        service = BatchScheduleService(session, _registry())

        with pytest.raises(BatchScheduleValidationError) as caught:
            service.create(
                request_id=str(uuid4()),
                store_ids=[good.id, disabled.id],
                template=_template(),
            )
        assert caught.value.preview["eligible"] is False
        assert caught.value.preview["eligible_count"] == 1
        assert caught.value.preview["created_count"] == 0
        rejected = caught.value.preview["targets"][1]
        assert rejected["eligible"] is False
        assert "店铺尚未启用" in rejected["reasons"]
        assert session.scalar(select(func.count(ScheduleBatch.id))) == 0
        assert session.scalar(select(func.count(Schedule.id))) == 0


def test_child_insert_failure_rolls_back_batch_parent(database):
    with database() as session:
        store = _seed_store(session, 1)
        session.commit()
        session.connection().exec_driver_sql(
            "CREATE TRIGGER reject_schedule BEFORE INSERT ON schedules "
            "BEGIN SELECT RAISE(ABORT, 'forced child failure'); END"
        )
        session.commit()

        with pytest.raises(IntegrityError):
            BatchScheduleService(session, _registry()).create(
                request_id=str(uuid4()),
                store_ids=[store.id],
                template=_template(),
            )
        session.rollback()
        assert session.scalar(select(func.count(ScheduleBatch.id))) == 0
        assert session.scalar(select(func.count(Schedule.id))) == 0


class _ReadOnlyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    limit: int = 10


class _EmptyReadOnlyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def test_registered_non_financial_workflow_can_omit_marketplaces_and_identity(database):
    definition = WorkflowDefinition(
        key="inventory_report",
        display_name="库存报表",
        description="test",
        workflow=None,
        supported_modes=(RunMode.DRY_RUN,),
        default_mode=RunMode.DRY_RUN,
        config_version=3,
        config_model=_ReadOnlyConfig,
        requires_marketplace_targets=False,
        requires_confirmed_identity=False,
        requires_financial_lock=False,
        execution_class=WorkflowExecutionClass.READ_ONLY,
        business_priority=20,
    )
    with database() as session:
        store = _seed_store(session, 1)
        store.expected_seller_id = None
        store.identity_confirmed = False
        store.enabled = True
        session.commit()
        registry = WorkflowRegistry((definition,))

        result = BatchScheduleService(session, registry).create(
            request_id=str(uuid4()),
            store_ids=[store.id],
            template={
                **_template(),
                "workflow": "inventory_report",
                "mode": "dry_run",
                "workflow_config": {"limit": 25},
            },
        )
        session.commit()
        schedule = session.get(Schedule, result["schedules"][0]["id"])
        assert schedule.marketplace_codes == []
        assert schedule.workflow_config == {"limit": 25}
        assert schedule.workflow_config_version == 3


def test_stale_schedule_cannot_be_enabled_until_config_is_resaved(database):
    definition = WorkflowDefinition(
        key="inventory_report",
        display_name="库存报表",
        description="test",
        workflow=None,
        supported_modes=(RunMode.DRY_RUN,),
        default_mode=RunMode.DRY_RUN,
        config_version=3,
        config_model=_ReadOnlyConfig,
        requires_marketplace_targets=False,
        requires_confirmed_identity=False,
        execution_class=WorkflowExecutionClass.READ_ONLY,
        business_priority=20,
    )
    registry = WorkflowRegistry((definition,))
    with database() as session:
        store = _seed_store(session, 1)
        store.expected_seller_id = None
        store.identity_confirmed = False
        store.enabled = True
        schedule = Schedule(
            store_id=store.id,
            name="old report",
            workflow="inventory_report",
            mode="dry_run",
            workflow_config={"limit": 10},
            workflow_config_version=2,
            marketplace_codes=[],
            enabled=True,
        )
        session.add(schedule)
        session.commit()

        repository = ScheduleRepository(session, registry)
        disabled = repository.update(schedule.id, enabled=False)
        assert disabled.enabled is False
        assert disabled.workflow_config == {"limit": 10}
        assert disabled.workflow_config_version == 2

        with pytest.raises(ValueError, match="重新保存流程参数"):
            repository.update(schedule.id, enabled=True)
        with pytest.raises(ValueError, match="重新保存流程参数"):
            repository.update(schedule.id, name="still stale")
        with pytest.raises(ValueError, match="重新保存流程参数"):
            repository.update(schedule.id, enabled=False, name="not disable-only")
        with pytest.raises(ValueError, match="重新保存流程参数"):
            repository.update(schedule.id, marketplace_codes=[])

        upgraded = repository.update(
            schedule.id,
            workflow_config={"limit": 25},
            enabled=True,
        )
        assert upgraded.workflow_config_version == 3
        assert upgraded.workflow_config == {"limit": 25}
        assert upgraded.enabled is True


def test_empty_store_workflow_config_stays_empty_in_run_snapshot(database):
    definition = WorkflowDefinition(
        key="health_check",
        display_name="店铺健康检查",
        description="test",
        workflow=None,
        supported_modes=(RunMode.DRY_RUN,),
        default_mode=RunMode.DRY_RUN,
        config_version=1,
        config_model=_EmptyReadOnlyConfig,
        requires_marketplace_targets=False,
        requires_confirmed_identity=False,
        execution_class=WorkflowExecutionClass.READ_ONLY,
        business_priority=20,
    )
    with database() as session:
        store = _seed_store(session, 1)
        session.commit()
        registry = WorkflowRegistry((definition,))
        result = BatchScheduleService(session, registry).create(
            request_id=str(uuid4()),
            store_ids=[store.id],
            template={
                **_template(),
                "workflow": "health_check",
                "workflow_config": {},
            },
        )
        run = ScheduleRepository(session, registry).create_run_from_schedule(
            result["schedules"][0]["id"],
            require_enabled=True,
            trigger="schedule",
            requested_by="scheduler",
        )
        assert run.workflow_config == {}


def test_repositories_normalize_unregistered_workflow_errors(database):
    with database() as session:
        store = _seed_store(session, 1)
        session.commit()
        registry = WorkflowRegistry()

        with pytest.raises(ValueError, match="代码白名单"):
            ScheduleRepository(session, registry).create(
                store_id=store.id,
                name="removed schedule",
                workflow="removed_workflow",
                mode="dry_run",
                workflow_config={},
                marketplace_codes=[],
            )

        with pytest.raises(ValueError, match="代码白名单"):
            WorkflowRepository(session, registry).create_run(
                store_id=store.id,
                workflow="removed_workflow",
                mode="dry_run",
                workflow_config={},
            )


def test_run_keeps_an_immutable_copy_of_schedule_configuration(database):
    with database() as session:
        store = _seed_store(session, 1)
        session.commit()
        service = BatchScheduleService(session, _registry())
        created = service.create(
            request_id=str(uuid4()),
            store_ids=[store.id],
            template=_template(),
        )
        schedule_id = created["schedules"][0]["id"]
        run = ScheduleRepository(session).create_run_from_schedule(
            schedule_id,
            require_enabled=True,
            trigger="schedule",
            requested_by="scheduler",
        )
        session.flush()
        assert run.workflow_config == {"marketplace_codes": ["CA", "UK"]}

        schedule = session.get(Schedule, schedule_id)
        schedule.workflow_config = {"marketplace_codes": ["CA"]}
        schedule.marketplace_codes = ["CA"]
        session.flush()
        session.expire(run)
        persisted = session.get(Run, run.id)
        assert persisted.workflow_config == {"marketplace_codes": ["CA", "UK"]}


def test_every_day_marker_is_accepted_and_normalized(database):
    with database() as session:
        store = _seed_store(session, 1)
        session.commit()
        preview = BatchScheduleService(session, _registry()).preview(
            store_ids=[store.id], template=_template(days_of_week="*")
        )
        assert preview["normalized_template"]["days_of_week"] == "*"


def test_queue_snapshots_workflow_priority_then_batch_target_order(database):
    with database() as session:
        stores = [_seed_store(session, index) for index in range(1, 4)]
        session.commit()
        created = BatchScheduleService(session, _registry()).create(
            request_id=str(uuid4()),
            store_ids=[store.id for store in stores],
            template=_template(),
        )
        schedules = [session.get(Schedule, item["id"]) for item in created["schedules"]]
        due = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
        runs = []
        for index, schedule in enumerate(schedules):
            run = Run(
                store_id=schedule.store_id,
                schedule_id=schedule.id,
                workflow="inventory_report" if index == 2 else "amazon_disbursement",
                mode="dry_run",
                trigger="schedule",
                scheduled_for_at=due,
            )
            session.add(run)
            runs.append(run)
        session.commit()

    queue = DurableRunQueue(database)
    queue.enqueue(runs[1].id, business_priority=1)  # batch target 2, first enqueue
    queue.enqueue(runs[2].id, business_priority=2)  # target 3, lower business rank
    queue.enqueue(runs[0].id, business_priority=1)  # batch target 1, last enqueue

    claimed = []
    snapshots = []
    for _ in range(3):
        entry = queue.claim_next()
        assert entry is not None
        claimed.append(entry.run_id)
        snapshots.append((entry.business_priority, entry.target_order))
        assert queue.finish(entry.id)
    assert claimed == [runs[0].id, runs[1].id, runs[2].id]
    assert snapshots == [(1, 1), (1, 2), (2, 3)]
