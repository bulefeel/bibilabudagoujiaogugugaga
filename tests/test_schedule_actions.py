from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import Run, RunEvent, Schedule, Store, StoreMarketplace
from ziniao_automation.repositories import ConflictError, ScheduleRepository
from ziniao_automation.scheduler import ScheduleManager
from ziniao_automation.web import create_app


class Automation:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue_run(self, run_id: str) -> None:
        self.enqueued.append(run_id)

    async def sync_ziniao(self):
        return {"created": 0, "updated": 0}


@pytest.fixture()
def database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'actions.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    yield settings, factory
    engine.dispose()


def seed(factory, *, mode: str = "dry_run", enabled: bool = False) -> int:
    with factory() as db:
        selector = f"oauth-actions-{uuid4()}"
        store = Store(
            name="Action Store",
            selector_type="oauth",
            selector_value=selector,
            browser_oauth=selector,
            expected_seller_id="SELLER-ACTIONS",
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
        rule = Schedule(
            store_id=store.id,
            name="Saved test rule",
            mode=mode,
            local_time="09:00",
            marketplace_codes=["CA"],
            enabled=enabled,
        )
        db.add(rule)
        db.commit()
        return rule.id


def bootstrap(client: TestClient) -> str:
    response = client.post(
        "/auth/bootstrap",
        json={
            "username": "operator",
            "password": "Local-Ledger-2026!",
            "confirm_password": "Local-Ledger-2026!",
        },
    )
    assert response.status_code == 200
    return client.cookies["ziniao_csrf"]


def test_run_now_allows_disabled_rule_but_rejects_second_active_instance(database):
    _, factory = database
    schedule_id = seed(factory, enabled=False)
    automation = Automation()
    manager = ScheduleManager(factory, automation)

    run_id = asyncio.run(manager.run_now(schedule_id))

    assert automation.enqueued == [run_id]
    with factory() as db:
        run = db.get(Run, run_id)
        assert run is not None
        assert run.schedule_id == schedule_id
        assert run.trigger == "manual"
        assert run.requested_by == "admin"
        assert run.result_summary["requested_marketplaces"] == ["CA"]
        assert run.result_summary["schedule_trigger"] == "run_now"
    with pytest.raises(ConflictError, match="已有未完成实例"):
        asyncio.run(manager.run_now(schedule_id))


def test_run_now_creates_an_auto_run_without_prior_approvals(database):
    """Automatic mode no longer needs two prior approval-mode successes.

    That prerequisite was removed on the operator's instruction.  Run-now must
    therefore create the run, while every per-run financial guard stays where
    it is — this test only pins that the prerequisite is gone, not that any
    money check was relaxed.
    """

    _, factory = database
    schedule_id = seed(factory, mode="auto", enabled=False)
    manager = ScheduleManager(factory, Automation())

    asyncio.run(manager.run_now(schedule_id))

    with factory() as db:
        runs = db.query(Run).filter(Run.schedule_id == schedule_id).all()
        assert len(runs) == 1
        assert runs[0].mode == "auto"


def test_schedule_requires_an_explicit_marketplace_selection(database):
    _, factory = database
    schedule_id = seed(factory)
    with factory() as db:
        store_id = db.get(Schedule, schedule_id).store_id
        with pytest.raises(ValueError, match="至少一个站点"):
            ScheduleRepository(db).create(
                store_id=store_id,
                name="Ambiguous rule",
                workflow="amazon_disbursement",
                mode="dry_run",
                local_time="09:00",
                marketplace_codes=[],
                enabled=False,
            )
        with pytest.raises(ValueError, match="至少一个站点"):
            ScheduleRepository(db).update(schedule_id, marketplace_codes=[])

    # Protect legacy rows created before the explicit-selection rule existed:
    # run-now must stop rather than silently expanding an empty list to all
    # enabled marketplaces.
    with factory() as db:
        db.get(Schedule, schedule_id).marketplace_codes = []
        db.commit()
    with pytest.raises(ValueError, match="没有明确选择站点"):
        asyncio.run(ScheduleManager(factory, Automation()).run_now(schedule_id))






def test_delete_detaches_terminal_history_but_rejects_active_run(database):
    _, factory = database
    schedule_id = seed(factory)
    with factory() as db:
        schedule = db.get(Schedule, schedule_id)
        terminal = Run(
            store_id=schedule.store_id,
            schedule_id=schedule.id,
            workflow=schedule.workflow,
            mode=schedule.mode,
            trigger="schedule",
            status="SUCCEEDED",
            requested_by="scheduler",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        db.add(terminal)
        db.flush()
        db.add(RunEvent(run_id=terminal.id, event_type="DONE", message="kept"))
        db.commit()
        terminal_id = terminal.id

    with factory() as db:
        ScheduleRepository(db).delete(schedule_id)
        db.commit()
    with factory() as db:
        assert db.get(Schedule, schedule_id) is None
        assert db.get(Run, terminal_id).schedule_id is None
        assert db.query(RunEvent).filter(RunEvent.run_id == terminal_id).count() == 1

    active_schedule_id = seed(factory)
    with factory() as db:
        schedule = db.get(Schedule, active_schedule_id)
        db.add(
            Run(
                store_id=schedule.store_id,
                schedule_id=schedule.id,
                workflow=schedule.workflow,
                mode=schedule.mode,
                trigger="manual",
                status="WAITING_AUTH",
                requested_by="admin",
            )
        )
        db.commit()
    with factory() as db:
        with pytest.raises(ConflictError, match="未完成任务"):
            ScheduleRepository(db).delete(active_schedule_id)
        db.rollback()
    with factory() as db:
        assert db.get(Schedule, active_schedule_id) is not None


def test_schedule_action_http_contract(database):
    settings, factory = database
    schedule_id = seed(factory, enabled=False)
    automation = Automation()
    app = create_app(settings, automation_service=automation)
    manager = ScheduleManager(app.state.sessions, automation)
    app.state.schedule_manager = manager

    with TestClient(app) as client:
        csrf = bootstrap(client)
        headers = {"X-CSRF-Token": csrf}
        response = client.post(
            f"/api/schedules/{schedule_id}/run-now", json={}, headers=headers
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body == {
            "status": "queued",
            "run_id": automation.enqueued[0],
            "redirect": f"/runs/{automation.enqueued[0]}",
        }
        blocked = client.delete(f"/api/schedules/{schedule_id}", headers=headers)
        assert blocked.status_code == 409

        with app.state.sessions() as db:
            run = db.get(Run, body["run_id"])
            run.status = "SUCCEEDED"
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        deleted = client.delete(f"/api/schedules/{schedule_id}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {"status": "deleted", "schedule_id": schedule_id}
