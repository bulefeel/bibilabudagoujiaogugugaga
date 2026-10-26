"""Inventory resume UI/API tests; all rows and service doubles are isolated."""
from pathlib import Path
from datetime import datetime, timezone
import re

import pytest
from sqlalchemy import func, select, text

from ziniao_automation.models import InventoryGpsrItem, Run, RunEvent, SiteRun, Store, StoreMarketplace
from ziniao_automation.web import UnconfiguredAutomationService
from tests.test_db_api import app_client, bootstrap


WORKFLOW = "amazon_inventory_gpsr_attestation"
LEGACY_ERROR = (
    "InventoryContractError: 德国站库存接口返回 HTTP 403，本次流程已停止；"
    "请在紫鸟确认页面恢复后再创建新任务"
)


def _seed(app, *, status="PAUSED", workflow=WORKFLOW, error=None):
    with app.state.sessions() as db:
        store = Store(name="库存暂停测试店", selector_type="oauth", selector_value="fixture",
                      expected_seller_id="SELLER", identity_confirmed=True, enabled=True)
        db.add(store)
        db.flush()
        market = StoreMarketplace(store_id=store.id, code="DE", domain="sellercentral.amazon.de",
                                  currency="EUR", enabled=True)
        db.add(market)
        db.flush()
        db.add(Run(id="inventory-resume", store_id=store.id, workflow=workflow, mode="auto",
                   status=status, trigger="manual", error=error))
        db.flush()
        db.add(SiteRun(id="inventory-resume-de", run_id="inventory-resume", marketplace_id=market.id,
                       marketplace_code="DE", status=status))
        db.flush()
        db.add(RunEvent(run_id="inventory-resume", site_run_id="inventory-resume-de",
                        event_type="inventory_gpsr_scan_complete", details={"scanned": 13}))
        for index in range(13):
            db.add(InventoryGpsrItem(
                id=f"candidate-{index}", run_id="inventory-resume", store_id=store.id,
                marketplace_code="DE", sku=f"SKU-{index}", asin=f"B{index:09d}",
                page_number=1, position=index, state="PENDING", eligible=False,
            ))
        db.commit()


def test_resume_post_uses_existing_run_and_releases_request_transaction(app_client):
    app, client, fake = app_client
    headers = {"X-CSRF-Token": bootstrap(client)}
    _seed(app)

    async def resume(run_id):
        # Mirrors the service's writer reservation, without a worker or browser.
        with app.state.sessions() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            db.get(Run, run_id).status = "QUEUED"
            db.commit()
        fake.calls.append(("resume", run_id))

    fake.resume_inventory_run = resume
    response = client.post("/api/runs/inventory-resume/resume-inventory", headers=headers)
    assert response.status_code == 202, response.text
    assert response.json() == {"status": "queued"}
    assert fake.calls == [("resume", "inventory-resume")]
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        assert db.scalar(select(func.count()).select_from(InventoryGpsrItem)) == 13
        assert db.get(Run, "inventory-resume").status == "QUEUED"
    assert client.post("/api/runs/inventory-resume/resume-inventory", headers=headers).status_code == 409
    assert len(fake.calls) == 1


def test_resume_needs_csrf_and_an_existing_run(app_client):
    app, client, fake = app_client
    csrf = bootstrap(client)
    _seed(app)
    assert client.post("/api/runs/inventory-resume/resume-inventory").status_code == 403
    assert client.post("/api/runs/inventory-resume/resume-inventory",
                       headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/runs/missing/resume-inventory",
                       headers={"X-CSRF-Token": csrf}).status_code == 404
    assert fake.calls == []


@pytest.mark.parametrize("status,workflow", [
    ("CANCELLED", WORKFLOW), ("RUNNING", WORKFLOW), ("FAILED", WORKFLOW),
    ("SUCCEEDED", WORKFLOW), ("PAUSED", "amazon_disbursement"),
])
def test_resume_ineligible_run_has_no_button_or_service_call(app_client, status, workflow):
    app, client, fake = app_client
    headers = {"X-CSRF-Token": bootstrap(client)}
    _seed(app, status=status, workflow=workflow)
    response = client.post("/api/runs/inventory-resume/resume-inventory", headers=headers)
    assert response.status_code == 409
    page = client.get("/runs/inventory-resume")
    assert page.status_code == 200
    assert 'data-run-action="resume-inventory"' not in page.text
    assert fake.calls == []


def test_resume_service_conflicts_are_409(app_client):
    app, client, fake = app_client
    headers = {"X-CSRF-Token": bootstrap(client)}
    _seed(app)

    async def conflict(run_id):
        raise RuntimeError("任务状态已变化，请重新查看")

    fake.resume_inventory_run = conflict
    response = client.post("/api/runs/inventory-resume/resume-inventory", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "任务状态已变化，请重新查看"
    app.state.automation_service = UnconfiguredAutomationService()
    response = client.post("/api/runs/inventory-resume/resume-inventory", headers=headers)
    assert response.status_code == 409
    assert "尚未配置" in response.json()["detail"]


def test_paused_detail_and_list_show_manual_resume_cancel_and_all_original_items(app_client):
    app, client, fake = app_client
    csrf = bootstrap(client)
    _seed(app)
    with app.state.sessions() as db:
        confirmed = db.get(InventoryGpsrItem, "candidate-12")
        confirmed.state = "CONFIRMED"
        confirmed.intent_at = datetime.now(timezone.utc)
        uncertain = db.get(InventoryGpsrItem, "candidate-11")
        uncertain.state = "UNCERTAIN"
        uncertain.intent_at = datetime.now(timezone.utc)
        db.get(InventoryGpsrItem, "candidate-0").reason = "<script>untrusted</script>"
        db.commit()
    page = client.get("/runs/inventory-resume")
    assert page.status_code == 200, page.text
    for label in ("已暂停，等待页面恢复", "页面已恢复，继续剩余商品", "对应店铺的紫鸟窗口",
                  "点击后才将当前任务重新入队", "复用原候选清单", "已提交的商品不重放",
                  "不自动刷新亚马逊页面", "原候选清单与当前进度", "取消本次任务"):
        assert label in page.text
    assert page.text.count('data-inventory-item-id=') == 13
    assert page.text.index('data-inventory-item-id="candidate-12"') < page.text.index('data-inventory-item-id="candidate-0"')
    assert "提交结果不明，仅回读" in page.text
    assert "<script>untrusted</script>" not in page.text
    assert "&lt;script&gt;untrusted&lt;/script&gt;" in page.text
    listing = client.get("/runs")
    assert "已暂停，等待页面恢复" in listing.text
    assert 'data-run-action="cancel" data-run-id="inventory-resume"' in listing.text
    # Rendering and listing do not queue work or rewrite persisted states.
    assert fake.calls == []
    with app.state.sessions() as db:
        assert db.get(Run, "inventory-resume").status == "PAUSED"
        assert db.get(InventoryGpsrItem, "candidate-11").state == "UNCERTAIN"
    response = client.post("/api/runs/inventory-resume/cancel", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 202
    assert fake.calls == [("cancel", "inventory-resume")]


def test_known_legacy_http403_failure_offers_same_run_resume_read_only(app_client):
    app, client, fake = app_client
    headers = {"X-CSRF-Token": bootstrap(client)}
    _seed(app, status="FAILED", error=LEGACY_ERROR)
    with app.state.sessions() as db:
        run = db.get(Run, "inventory-resume")
        run.result_summary = {"workflow_plan_v1": {
            "workflow": WORKFLOW, "run_id": run.id, "store_id": str(run.store_id),
            "lines": [{"marketplace": {"code": "DE", "domain": "sellercentral.amazon.de"},
                       "snapshot": {"marketplace_code": "DE", "domain": "sellercentral.amazon.de",
                                    "seller_id": "SELLER"}}],
        }}
        db.commit()
    page = client.get("/runs/inventory-resume")
    assert page.status_code == 200
    assert 'data-run-action="resume-inventory"' in page.text
    with app.state.sessions() as db:
        assert db.get(Run, "inventory-resume").status == "FAILED"
        assert db.get(Run, "inventory-resume").error == LEGACY_ERROR

    async def resume(run_id):
        fake.calls.append(("resume", run_id))

    fake.resume_inventory_run = resume
    assert client.post("/api/runs/inventory-resume/resume-inventory", headers=headers).status_code == 202
    assert fake.calls == [("resume", "inventory-resume")]


def test_legacy_without_original_identity_has_no_resume_button_or_service_call(app_client):
    app, client, fake = app_client
    headers = {"X-CSRF-Token": bootstrap(client)}
    _seed(app, status="FAILED", error=LEGACY_ERROR)
    page = client.get("/runs/inventory-resume")
    assert page.status_code == 200
    assert 'data-run-action="resume-inventory"' not in page.text
    assert client.post("/api/runs/inventory-resume/resume-inventory", headers=headers).status_code == 409
    assert fake.calls == []


def test_inventory_resume_assets_have_a_shared_new_cache_key():
    root = Path(__file__).resolve().parents[1] / "src/ziniao_automation"
    base = (root / "templates/base.html").read_text(encoding="utf-8")
    keys = re.findall(r"app\.(?:js|css)'\) }}\?v=([\w.-]+)", base)
    assert len(keys) == 2 and keys[0] == keys[1]
    assert keys[0] >= "20261014-setup-multisite"
    css = (root / "static/app.css").read_text(encoding="utf-8")
    assert ".status-paused" in css
    assert ".inventory-run-actions" in css
