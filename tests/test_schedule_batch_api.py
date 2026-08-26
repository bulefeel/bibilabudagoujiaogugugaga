from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from ziniao_automation.config import Settings
from ziniao_automation.models import Schedule, StoreMarketplace
from ziniao_automation.repositories import StoreRepository
from ziniao_automation.scheduler import ScheduleManager
from ziniao_automation.web import create_app

from tests.test_db_api import FakeAutomation, bootstrap


class _RefreshCounter:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh(self) -> None:
        self.calls += 1


class _ProjectionJob:
    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.next_run_time = datetime(2026, 8, 26, 1, tzinfo=timezone.utc)


class _SelectiveProjectionScheduler:
    """APScheduler stand-in that can reject selected projection attempts."""

    def __init__(
        self,
        *,
        failed_attempts: tuple[int, ...] = (),
        failed_schedule_ids: tuple[int, ...] = (),
    ) -> None:
        self.failed_attempts = set(failed_attempts)
        self.failed_schedule_ids = set(failed_schedule_ids)
        self.attempted_schedule_ids: list[int] = []
        self.jobs: dict[str, _ProjectionJob] = {}

    def add_job(self, _func, **kwargs):
        schedule_id = int(kwargs["args"][0])
        self.attempted_schedule_ids.append(schedule_id)
        attempt = len(self.attempted_schedule_ids)
        if (
            attempt in self.failed_attempts
            or schedule_id in self.failed_schedule_ids
        ):
            raise RuntimeError("synthetic add_job failure")
        job = _ProjectionJob(kwargs["id"])
        self.jobs[job.id] = job
        return job

    def get_jobs(self):
        return list(self.jobs.values())

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


@pytest.fixture()
def batch_client(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'batch.db').as_posix()}",
        testing=True,
    )
    app = create_app(settings, automation_service=FakeAutomation())
    with TestClient(app) as client:
        csrf = bootstrap(client)
        refresh = _RefreshCounter()
        app.state.schedule_manager = refresh
        yield app, client, {"X-CSRF-Token": csrf}, refresh


def _seed_store(app, *, name: str, code: str = "CA", enabled: bool = True) -> int:
    with app.state.sessions() as db:
        store = StoreRepository(db).create(
            name=name,
            selector_type="id",
            selector_value=f"profile-{name}",
            browser_id=f"profile-{name}",
            expected_seller_id=f"SELLER-{name}",
        )
        store.identity_confirmed = enabled
        store.enabled = enabled
        db.add(
            StoreMarketplace(
                store_id=store.id,
                code=code,
                domain="sellercentral.amazon.ca",
                currency="CAD",
                enabled=enabled,
            )
        )
        db.commit()
        return store.id


def _payload(store_ids: list[int], *, request_id: str) -> dict:
    return {
        "request_id": request_id,
        "store_ids": store_ids,
        "template": {
            "name": "工作日批量提现",
            "workflow": "amazon_disbursement",
            "mode": "dry_run",
            "workflow_config": {"marketplace_codes": ["CA"]},
            "local_time": "09:00",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
            "timezone": "Asia/Singapore",
            "enabled": True,
        },
    }


def test_workflow_metadata_is_safe_and_code_registered(batch_client) -> None:
    _, client, _, _ = batch_client

    response = client.get("/api/workflows")

    assert response.status_code == 200
    body = response.json()
    assert [item["key"] for item in body] == ["amazon_disbursement"]
    assert body[0]["business_priority"] == 1
    assert body[0]["config_schema"]["additionalProperties"] is False
    assert set(body[0]) == {
        "key",
        "display_name",
        "description",
        "supported_modes",
        "default_mode",
            "config_version",
            "requires_marketplace_targets",
            "requires_confirmed_identity",
            "requires_financial_lock",
            "execution_class",
        "business_priority",
        "config_schema",
    }


def test_batch_preview_create_and_idempotent_retry(batch_client) -> None:
    app, client, headers, refresh = batch_client
    first = _seed_store(app, name="A")
    second = _seed_store(app, name="B")
    payload = _payload(
        [first, second], request_id="12345678-1234-4234-9234-123456789abc"
    )

    preview = client.post(
        "/api/schedules/batch/preview", json=payload, headers=headers
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible"] is True
    assert [item["order"] for item in preview.json()["targets"]] == [1, 2]

    created = client.post("/api/schedules/batch", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "created"
    assert created.json()["created_count"] == 2
    assert refresh.calls == 1

    repeated = client.post("/api/schedules/batch", json=payload, headers=headers)
    assert repeated.status_code == 201
    assert repeated.json()["status"] == "existing"
    # Idempotent retries also repair a previous scheduler projection failure.
    assert refresh.calls == 2

    with app.state.sessions() as db:
        rows = list(
            db.scalars(select(Schedule).order_by(Schedule.batch_order))
        )
        assert len(rows) == 2
        assert rows[0].batch_id == rows[1].batch_id
        assert [row.batch_order for row in rows] == [1, 2]
        assert all(
            row.workflow_config == {"marketplace_codes": ["CA"]}
            for row in rows
        )

    changed = _payload(
        [first, second], request_id="12345678-1234-4234-9234-123456789abc"
    )
    changed["template"]["local_time"] = "10:00"
    conflict = client.post("/api/schedules/batch", json=changed, headers=headers)
    assert conflict.status_code == 409


def test_batch_reports_missing_scheduler_and_same_id_retry_repairs_projection(
    batch_client,
) -> None:
    app, client, headers, refresh = batch_client
    store_id = _seed_store(app, name="scheduler-retry")
    payload = _payload(
        [store_id], request_id="12121212-3434-4567-9898-121212121212"
    )
    app.state.schedule_manager = None

    created = client.post("/api/schedules/batch", json=payload, headers=headers)

    assert created.status_code == 201, created.text
    assert created.json()["status"] == "created"
    assert created.json()["scheduler_refreshed"] is False
    assert "不要重新新建" in created.json()["warning"]

    app.state.schedule_manager = refresh
    retried = client.post("/api/schedules/batch", json=payload, headers=headers)

    assert retried.status_code == 201, retried.text
    assert retried.json()["status"] == "existing"
    assert retried.json()["scheduler_refreshed"] is True
    assert refresh.calls == 1


@pytest.mark.parametrize(
    ("failed_attempts", "failed_indexes"),
    (
        pytest.param((1, 2), (0, 1), id="all-add-job-calls-fail"),
        pytest.param((2,), (1,), id="one-add-job-call-fails"),
    ),
)
def test_batch_reports_its_own_projection_failures_without_rolling_back(
    batch_client,
    failed_attempts: tuple[int, ...],
    failed_indexes: tuple[int, ...],
) -> None:
    app, client, headers, _ = batch_client
    first = _seed_store(app, name="projection-A")
    second = _seed_store(app, name="projection-B")
    payload = _payload(
        [first, second], request_id="abababab-1234-4234-9234-abababababab"
    )
    scheduler = _SelectiveProjectionScheduler(failed_attempts=failed_attempts)
    app.state.schedule_manager = ScheduleManager(
        app.state.sessions,
        FakeAutomation(),
        scheduler=scheduler,
        workflow_registry=app.state.workflow_registry,
    )

    created = client.post("/api/schedules/batch", json=payload, headers=headers)

    assert created.status_code == 201, created.text
    body = created.json()
    schedule_ids = [item["id"] for item in body["schedules"]]
    expected_failed = [schedule_ids[index] for index in failed_indexes]
    expected_projected = set(schedule_ids) - set(expected_failed)
    assert body["status"] == "created"
    assert body["scheduler_refreshed"] is False
    assert body["scheduler_failed_schedule_ids"] == expected_failed
    assert "warning" in body
    assert scheduler.attempted_schedule_ids == schedule_ids
    assert set(scheduler.jobs) == {
        f"db-schedule:{schedule_id}" for schedule_id in expected_projected
    }
    with app.state.sessions() as db:
        assert db.scalar(select(func.count(Schedule.id))) == 2


def test_unrelated_projection_failure_does_not_mark_new_batch_unrefreshed(
    batch_client,
) -> None:
    app, client, headers, _ = batch_client
    unrelated_store_id = _seed_store(app, name="unrelated-bad-projection")
    with app.state.sessions() as db:
        unrelated = Schedule(
            store_id=unrelated_store_id,
            name="Unrelated legacy schedule",
            workflow="amazon_disbursement",
            mode="dry_run",
            workflow_config={"marketplace_codes": ["CA"]},
            workflow_config_version=1,
            marketplace_codes=["CA"],
            local_time="09:00",
            days_of_week="mon,tue,wed,thu,fri",
            timezone="Asia/Singapore",
            enabled=True,
        )
        db.add(unrelated)
        db.commit()
        unrelated_schedule_id = unrelated.id

    first = _seed_store(app, name="healthy-batch-A")
    second = _seed_store(app, name="healthy-batch-B")
    payload = _payload(
        [first, second], request_id="cdcdcdcd-1234-4234-9234-cdcdcdcdcdcd"
    )
    scheduler = _SelectiveProjectionScheduler(
        failed_schedule_ids=(unrelated_schedule_id,)
    )
    app.state.schedule_manager = ScheduleManager(
        app.state.sessions,
        FakeAutomation(),
        scheduler=scheduler,
        workflow_registry=app.state.workflow_registry,
    )

    created = client.post("/api/schedules/batch", json=payload, headers=headers)

    assert created.status_code == 201, created.text
    body = created.json()
    batch_schedule_ids = [item["id"] for item in body["schedules"]]
    assert body["scheduler_refreshed"] is True
    assert "scheduler_failed_schedule_ids" not in body
    assert "warning" not in body
    assert scheduler.attempted_schedule_ids == [
        unrelated_schedule_id,
        *batch_schedule_ids,
    ]
    assert f"db-schedule:{unrelated_schedule_id}" not in scheduler.jobs
    assert set(scheduler.jobs) == {
        f"db-schedule:{schedule_id}" for schedule_id in batch_schedule_ids
    }


def test_invalid_target_keeps_batch_transaction_empty(batch_client) -> None:
    app, client, headers, refresh = batch_client
    valid = _seed_store(app, name="valid")
    invalid = _seed_store(app, name="disabled", enabled=False)
    payload = _payload(
        [valid, invalid], request_id="87654321-4321-4321-8321-cba987654321"
    )

    preview = client.post(
        "/api/schedules/batch/preview", json=payload, headers=headers
    )
    assert preview.status_code == 200
    assert preview.json()["eligible"] is False
    assert preview.json()["targets"][1]["reasons"]

    rejected = client.post("/api/schedules/batch", json=payload, headers=headers)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["eligible"] is False
    assert refresh.calls == 0
    with app.state.sessions() as db:
        assert db.scalar(select(func.count(Schedule.id))) == 0


def test_unknown_workflow_and_extra_config_are_rejected_before_writes(
    batch_client,
) -> None:
    app, client, headers, _ = batch_client
    store_id = _seed_store(app, name="safe")
    payload = _payload(
        [store_id], request_id="aaaaaaaa-1234-4234-9234-aaaaaaaaaaaa"
    )

    payload["template"]["workflow"] = "uploaded_script"
    unknown = client.post(
        "/api/schedules/batch/preview", json=payload, headers=headers
    )
    assert unknown.status_code == 422

    payload["template"]["workflow"] = "amazon_disbursement"
    payload["template"]["workflow_config"]["token"] = "must-not-be-accepted"
    extra = client.post(
        "/api/schedules/batch/preview", json=payload, headers=headers
    )
    assert extra.status_code == 422
    assert "must-not-be-accepted" not in extra.text
    with app.state.sessions() as db:
        assert db.scalar(select(func.count(Schedule.id))) == 0


def test_legacy_single_schedule_patch_updates_canonical_marketplaces(
    batch_client,
) -> None:
    app, client, headers, _ = batch_client
    store_id = _seed_store(app, name="legacy")
    with app.state.sessions() as db:
        db.add(
            StoreMarketplace(
                store_id=store_id,
                code="UK",
                domain="sellercentral.amazon.co.uk",
                currency="GBP",
                enabled=True,
            )
        )
        db.commit()

    created = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "store_id": store_id,
            "name": "旧客户端排期",
            "mode": "dry_run",
            "marketplace_codes": ["CA"],
            "enabled": False,
        },
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]

    changed = client.patch(
        f"/api/schedules/{schedule_id}",
        headers=headers,
        json={"marketplace_codes": ["UK"]},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["marketplace_codes"] == ["UK"]
    assert changed.json()["workflow_config"] == {"marketplace_codes": ["UK"]}

    empty = client.patch(
        f"/api/schedules/{schedule_id}",
        headers=headers,
        json={"marketplace_codes": []},
    )
    assert empty.status_code == 422


def test_equivalent_legacy_and_versioned_marketplaces_are_normalized(
    batch_client,
) -> None:
    app, client, headers, _ = batch_client
    store_id = _seed_store(app, name="normalized-input")

    response = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "store_id": store_id,
            "name": "normalized marketplace inputs",
            "workflow": "amazon_disbursement",
            "mode": "dry_run",
            "marketplace_codes": ["CA"],
            "workflow_config": {"marketplace_codes": ["ca", "CA"]},
            "enabled": False,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["marketplace_codes"] == ["CA"]
    assert response.json()["workflow_config"] == {"marketplace_codes": ["CA"]}
