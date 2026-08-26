from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from ziniao_automation.config import Settings
from ziniao_automation.db import (
    create_sqlite_engine,
    init_database,
    make_session_factory,
)
from ziniao_automation.models import Run, RunEvent
from ziniao_automation.repositories import StoreRepository, WorkflowRepository
from ziniao_automation.workflows import (
    AutomationService,
    DatabaseRunLoader,
    RunMode,
    WorkflowDefinition,
    WorkflowExecutionClass,
    WorkflowRegistry,
)


class _ReadOnlyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = 10


@pytest.mark.asyncio
async def test_store_level_workflow_loads_without_marketplace_or_identity(
    tmp_path: Path,
) -> None:
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'generic.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    sessions = make_session_factory(engine)
    registry = WorkflowRegistry(
        (
            WorkflowDefinition(
                key="store_report",
                display_name="店铺只读报表",
                description="测试用店铺级流程",
                workflow=None,
                supported_modes=(RunMode.DRY_RUN,),
                default_mode=RunMode.DRY_RUN,
                config_version=2,
                config_model=_ReadOnlyConfig,
                requires_marketplace_targets=False,
                requires_confirmed_identity=False,
                execution_class=WorkflowExecutionClass.READ_ONLY,
                business_priority=2,
            ),
        )
    )
    with sessions() as db:
        store = StoreRepository(db).create(
            name="No marketplace store",
            selector_type="id",
            selector_value="profile-generic",
            browser_id="profile-generic",
        )
        store.enabled = True
        run = WorkflowRepository(db, registry).create_run(
            store_id=store.id,
            workflow="store_report",
            mode="dry_run",
            workflow_config={"limit": 25},
            workflow_config_version=2,
        )
        run_id = run.id
        db.commit()

    loaded = await DatabaseRunLoader(
        sessions, workflow_registry=registry
    )(run_id)

    assert loaded.workflow == "store_report"
    assert loaded.marketplaces == ()
    assert loaded.workflow_config == {"limit": 25}
    assert loaded.workflow_config_version == 2
    assert loaded.store.identity_confirmed is False
    engine.dispose()


@pytest.mark.asyncio
async def test_unknown_queued_workflow_becomes_controlled_failure(
    tmp_path: Path,
) -> None:
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'unknown.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    sessions = make_session_factory(engine)
    registry = WorkflowRegistry(
        (
            WorkflowDefinition(
                key="store_report",
                display_name="店铺只读报表",
                description="test",
                workflow=None,
                supported_modes=(RunMode.DRY_RUN,),
                default_mode=RunMode.DRY_RUN,
                config_version=1,
                config_model=_ReadOnlyConfig,
                execution_class=WorkflowExecutionClass.READ_ONLY,
            ),
        )
    )
    with sessions() as db:
        store = StoreRepository(db).create(
            name="Unknown workflow store",
            selector_type="id",
            selector_value="unknown-workflow-store",
        )
        store.enabled = True
        run = Run(
            store_id=store.id,
            workflow="removed_workflow",
            mode="dry_run",
            trigger="manual",
            status="QUEUED",
            requested_by="admin",
            workflow_config={},
            workflow_config_version=1,
        )
        db.add(run)
        db.commit()
        run_id = run.id

    loader = DatabaseRunLoader(sessions, workflow_registry=registry)
    service = AutomationService(
        engine=SimpleNamespace(registry=registry, repository=object()),
        run_loader=loader,
        ziniao_controller=object(),
        session_factory=sessions,
    )
    await service.recover_startup()
    await service.wait_idle()
    await service.shutdown_worker()

    with sessions() as db:
        saved = db.get(Run, run_id)
        assert saved.status == "FAILED"
        assert saved.error == "任务配置无法加载：ValueError"
        assert db.query(RunEvent).filter_by(
            run_id=run_id, event_type="QUEUE_SNAPSHOT_REJECTED"
        ).count() == 1
    engine.dispose()
