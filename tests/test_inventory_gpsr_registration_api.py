from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ziniao_automation.composition import build_runtime
from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import Schedule, Store, StoreMarketplace
from ziniao_automation.web import create_app
from tests.test_db_api import FakeAutomation, bootstrap
from tests.test_schedule_batch_api import _RefreshCounter


KEY = "amazon_inventory_gpsr_attestation"


@pytest.fixture
def inventory_client(tmp_path: Path):
    settings = Settings(project_root=tmp_path, testing=True)
    engine = create_sqlite_engine(settings)
    init_database(engine)
    sessions = make_session_factory(engine)
    # Exercise the actual production registration without starting workers or browsers.
    runtime = build_runtime(settings, sessions, controller=SimpleNamespace(), notifier=SimpleNamespace())
    app = create_app(settings, automation_service=FakeAutomation(), workflow_registry=runtime.workflow_registry)
    with TestClient(app) as client:
        token = bootstrap(client)
        app.state.schedule_manager = _RefreshCounter()
        yield app, client, {"X-CSRF-Token": token}
    engine.dispose()


def test_production_workflows_expose_inventory_without_replacing_existing(inventory_client):
    _, client, _ = inventory_client
    response = client.get("/api/workflows")
    assert response.status_code == 200
    metadata = {item["key"]: item for item in response.json()}
    assert {KEY, "amazon_gpsr_attestation", "amazon_disbursement", "amazon_feedback_removal"} <= metadata.keys()
    item = metadata[KEY]
    assert item["supported_modes"] == ["dry_run", "auto"]
    assert item["default_mode"] == "dry_run"
    assert item["business_priority"] == 4
    assert item["requires_financial_lock"] is False
    assert item["requires_confirmed_identity"] is True
    market_schema = item["config_schema"]["properties"]["marketplace_codes"]["items"]
    assert market_schema["enum"] == ["DE", "IT", "FR", "ES", "PL"]
    types = item["config_schema"]["properties"]["processing_types"]
    assert types["default"] == ["safety_attestation"]
    assert types["x-create-default"] == ["safety_attestation", "responsible_person", "manufacturer"]
    assert types["minItems"] == 1
    assert item["config_version"] == 1


def test_existing_batch_endpoint_accepts_inventory_auto_without_financial_history(inventory_client):
    app, client, headers = inventory_client
    with app.state.sessions() as db:
        ids = []
        for index in range(2):
            store = Store(name=f"Inventory {index}", selector_type="oauth", selector_value=f"profile-{index}", expected_seller_id=f"SELLER-{index}", identity_confirmed=True, enabled=True)
            db.add(store)
            db.flush()
            db.add(StoreMarketplace(store_id=store.id, code="DE", domain="sellercentral.amazon.de", currency="EUR", enabled=True))
            ids.append(store.id)
        db.commit()
    payload = {
        "request_id": str(uuid4()), "store_ids": ids,
        "template": {"name": "库存安全证明", "workflow": KEY, "mode": "auto", "workflow_config": {"marketplace_codes": ["DE"]}, "first_run_at": "2026-11-01T01:00:00+00:00", "interval_minutes": 1440, "timezone": "Asia/Singapore", "enabled": False},
    }
    preview = client.post("/api/schedules/batch/preview", json=payload, headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["created_count"] == 2
    created = client.post("/api/schedules/batch", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    assert app.state.schedule_manager.calls == 1
    with app.state.sessions() as db:
        rows = list(db.scalars(select(Schedule).order_by(Schedule.batch_order)))
        assert [s.store_id for s in rows] == ids
        assert all(s.workflow == KEY and s.workflow_config == {"marketplace_codes": ["DE"], "processing_types": ["safety_attestation"]} for s in rows)
    payload["template"]["mode"] = "approval"
    assert client.post("/api/schedules/batch/preview", json=payload, headers=headers).status_code == 422
    payload["template"]["mode"] = "auto"
    payload["template"]["workflow_config"]["marketplace_codes"] = ["CA"]
    assert client.post("/api/schedules/batch/preview", json=payload, headers=headers).status_code == 422
