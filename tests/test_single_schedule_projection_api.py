from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from ziniao_automation.config import Settings
from ziniao_automation.models import Schedule, StoreMarketplace
from ziniao_automation.repositories import ScheduleRepository, StoreRepository
from ziniao_automation.scheduler import ScheduleProjectionReport
from ziniao_automation.web import create_app

from tests.test_db_api import FakeAutomation, bootstrap


class _MutationProjectionManager:
    """Return a report for the schedule ID supplied by the HTTP mutation."""

    def __init__(self, *, outcome: str, expects_projection: bool) -> None:
        self.outcome = outcome
        self.expects_projection = expects_projection
        self.calls: list[int] = []

    async def refresh_schedule(self, schedule_id: int) -> ScheduleProjectionReport:
        self.calls.append(schedule_id)
        unrelated_id = 999_999
        if self.outcome == "target_failure":
            return ScheduleProjectionReport(
                projected_schedule_ids=frozenset(),
                failed_schedule_ids=frozenset({schedule_id}),
            )
        projected = (
            frozenset({schedule_id})
            if self.expects_projection
            else frozenset()
        )
        failed = (
            frozenset({unrelated_id})
            if self.outcome == "unrelated_failure"
            else frozenset()
        )
        return ScheduleProjectionReport(
            projected_schedule_ids=projected,
            failed_schedule_ids=failed,
        )


@pytest.fixture()
def projection_client(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'single-projection.db').as_posix()}",
        testing=True,
    )
    app = create_app(settings, automation_service=FakeAutomation())
    with TestClient(app) as client:
        csrf = bootstrap(client)
        with app.state.sessions() as db:
            store = StoreRepository(db).create(
                name="Single projection store",
                selector_type="id",
                selector_value="single-projection-profile",
                browser_id="single-projection-profile",
                expected_seller_id="SELLER-SINGLE-PROJECTION",
            )
            store.identity_confirmed = True
            store.enabled = True
            db.add(
                StoreMarketplace(
                    store_id=store.id,
                    code="CA",
                    domain="sellercentral.amazon.ca",
                    currency="CAD",
                    enabled=True,
                )
            )
            db.commit()
            store_id = store.id
        yield app, client, {"X-CSRF-Token": csrf}, store_id


def _seed_schedule(app, store_id: int, *, name: str = "Existing schedule") -> int:
    with app.state.sessions() as db:
        schedule = ScheduleRepository(
            db, app.state.workflow_registry
        ).create(
            store_id=store_id,
            name=name,
            workflow="amazon_disbursement",
            mode="dry_run",
            local_time="09:00",
            days_of_week="mon,tue,wed,thu,fri",
            timezone="Asia/Singapore",
            marketplace_codes=["CA"],
            workflow_config={"marketplace_codes": ["CA"]},
            enabled=True,
        )
        db.commit()
        return schedule.id


def _assert_projection_response(
    body: dict,
    *,
    schedule_id: int,
    outcome: str,
) -> None:
    if outcome == "target_failure":
        assert body["scheduler_refreshed"] is False
        assert body["scheduler_failed_schedule_ids"] == [schedule_id]
        assert "不要重复提交" in body["warning"]
    else:
        assert body["scheduler_refreshed"] is True
        assert "scheduler_failed_schedule_ids" not in body
        assert "warning" not in body


@pytest.mark.parametrize(
    "outcome", ("success", "target_failure", "unrelated_failure")
)
def test_single_create_reports_projection_without_rolling_back_durable_row(
    projection_client,
    outcome: str,
) -> None:
    app, client, headers, store_id = projection_client
    manager = _MutationProjectionManager(
        outcome=outcome, expects_projection=True
    )
    app.state.schedule_manager = manager

    response = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "store_id": store_id,
            "name": "Created once",
            "workflow": "amazon_disbursement",
            "mode": "dry_run",
            "local_time": "09:00",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
            "timezone": "Asia/Singapore",
            "marketplace_codes": ["CA"],
            "workflow_config": {"marketplace_codes": ["CA"]},
            "enabled": True,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    schedule_id = body["id"]
    assert manager.calls == [schedule_id]
    _assert_projection_response(body, schedule_id=schedule_id, outcome=outcome)
    with app.state.sessions() as db:
        assert db.scalar(select(func.count(Schedule.id))) == 1
        assert db.get(Schedule, schedule_id).name == "Created once"


@pytest.mark.parametrize(
    "outcome", ("success", "target_failure", "unrelated_failure")
)
def test_single_patch_reports_projection_without_rolling_back_saved_edit(
    projection_client,
    outcome: str,
) -> None:
    app, client, headers, store_id = projection_client
    schedule_id = _seed_schedule(app, store_id)
    manager = _MutationProjectionManager(
        outcome=outcome, expects_projection=True
    )
    app.state.schedule_manager = manager

    response = client.patch(
        f"/api/schedules/{schedule_id}",
        headers=headers,
        json={"name": "Edited once"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert manager.calls == [schedule_id]
    _assert_projection_response(body, schedule_id=schedule_id, outcome=outcome)
    with app.state.sessions() as db:
        assert db.get(Schedule, schedule_id).name == "Edited once"


@pytest.mark.parametrize(
    "outcome", ("success", "target_failure", "unrelated_failure")
)
def test_single_delete_reports_projection_without_recreating_deleted_row(
    projection_client,
    outcome: str,
) -> None:
    app, client, headers, store_id = projection_client
    schedule_id = _seed_schedule(app, store_id)
    manager = _MutationProjectionManager(
        outcome=outcome, expects_projection=False
    )
    app.state.schedule_manager = manager

    response = client.delete(f"/api/schedules/{schedule_id}", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "deleted"
    assert manager.calls == [schedule_id]
    _assert_projection_response(body, schedule_id=schedule_id, outcome=outcome)
    with app.state.sessions() as db:
        assert db.get(Schedule, schedule_id) is None

