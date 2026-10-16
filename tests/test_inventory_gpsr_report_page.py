"""Render persisted inventory summaries through the real run-details route."""

import pytest

from ziniao_automation.models import Evidence, InventoryGpsrItem, Run, RunEvent, SiteRun, Store, StoreMarketplace
from tests.test_db_api import bootstrap
from tests.test_gpsr_web_exposure import client_with_every_workflow


@pytest.mark.parametrize("mode", ["dry_run", "auto"])
@pytest.mark.parametrize("scan_complete", [True, False])
def test_inventory_run_detail_has_counts_and_sku_without_a_money_card(client_with_every_workflow, mode, scan_complete):
    app, client, _ = client_with_every_workflow
    bootstrap(client)
    with app.state.sessions() as session:
        store = Store(name="库存测试店", selector_type="oauth", selector_value="selector",
                      browser_oauth="selector", expected_seller_id="expected",
                      identity_confirmed=True, enabled=True)
        session.add(store)
        session.flush()
        market = StoreMarketplace(store_id=store.id, code="DE", domain="sellercentral.amazon.de",
                                  currency="EUR", enabled=True)
        session.add(market)
        session.flush()
        session.add(Run(id="inventory-detail", store_id=store.id,
                        workflow="amazon_inventory_gpsr_attestation", mode=mode,
                        status="SUCCEEDED", trigger="manual"))
        session.flush()
        session.add(SiteRun(id="inventory-detail-de", run_id="inventory-detail",
                            marketplace_id=market.id, marketplace_code="DE",
                            status="SUCCEEDED", currency="EUR"))
        if scan_complete:
            session.add(RunEvent(run_id="inventory-detail", event_type="inventory_gpsr_scan_complete"))
        session.add(RunEvent(run_id="inventory-detail", event_type="inventory_gpsr_summary",
                             message="扫描 0 件",
                             details={"scanned": 1, "eligible": 1}))
        session.flush()
        session.add(RunEvent(run_id="inventory-detail", event_type="inventory_gpsr_summary",
                             details={"scanned": 13, "pending": 0, "blocked": 0, "eligible": 11, "confirmed": 0,
                                      "already_submitted": 0, "no_violation_entry": 0,
                                      "no_attestation": 2, "missing": 0, "failed": 0, "uncertain": 0}))
        session.add(RunEvent(run_id="inventory-detail", event_type="inventory_gpsr_item",
                             message="已检查声明表单", details={"sku": "SKU-<script>private</script>", "asin": "B012345678"}))
        session.add(Evidence(id="inventory-text-proof", run_id="inventory-detail", kind="json",
                             file_path="PRIVATE_LOCAL_PATH.json", sha256="a" * 64,
                             metadata_json={"label": "inventory_form"}))
        session.commit()

    response = client.get("/runs/inventory-detail")
    assert response.status_code == 200, response.text
    html = response.text
    for label in ("库存待批准商品合规处理", "DE 站点"):
        assert label in html
    if scan_complete:
        for label in ("候选扫描", "尚未检查", "历史记录待核对", "已核验可处理", "本轮提交确认（审核中）", "此前审核中", "无违规入口",
                      "无安全证明", "清单商品未找到", "处理失败", "提交结果不明"):
            assert label in html
        assert "13 件" in html and "11 件" in html and "2 件" in html
    else:
        assert "商品计数尚不可用" in html
        assert "13 件" not in html
        assert "扫描 0 件" not in html
    assert "SKU-&lt;script&gt;private&lt;/script&gt;" in html
    assert "B012345678" in html
    assert 'href="/evidence/inventory-text-proof"' in html
    assert "文字快照 · inventory_form" in html
    assert "PRIVATE_LOCAL_PATH" not in html
    for forbidden in ("站点与原币金额", "可提现 PAYABLE", "延迟资金", "审核通过", "这条流程不写台账表", "<script>private</script>"):
        assert forbidden not in html
    if mode == "dry_run":
        assert "不勾选、不保存" in html


def test_legacy_failure_details_project_ledger_without_rewriting_timeline(client_with_every_workflow):
    app, client, _ = client_with_every_workflow
    bootstrap(client)
    with app.state.sessions() as session:
        store = Store(name="库存测试店", selector_type="oauth", selector_value="selector",
                      browser_oauth="selector", expected_seller_id="expected",
                      identity_confirmed=True, enabled=True)
        session.add(store)
        session.flush()
        session.add(Run(id="inventory-legacy", store_id=store.id,
                        workflow="amazon_inventory_gpsr_attestation", mode="auto",
                        status="FAILED", trigger="manual"))
        session.flush()
        session.add(RunEvent(run_id="inventory-legacy", event_type="inventory_gpsr_scan_complete"))
        session.add(RunEvent(run_id="inventory-legacy", event_type="inventory_gpsr_summary",
                             message="旧事件原文：扫描13件，可处理0件，失败1件",
                             details={"scanned": 13, "eligible": 0, "confirmed": 0, "failed": 1}))
        for index in range(13):
            session.add(InventoryGpsrItem(
                run_id="inventory-legacy", store_id=store.id, marketplace_code="DE",
                sku=f"SKU-{index}", asin="B123456789", page_number=1, position=index,
                state="BLOCKED" if index == 0 else "PENDING", eligible=False,
            ))
        session.commit()
    html = client.get("/runs/inventory-legacy").text
    for expected in (
        "<dt>候选扫描</dt><dd>13 件</dd>", "<dt>尚未检查</dt><dd>12 件</dd>",
        "<dt>历史记录待核对</dt><dd>1 件</dd>", "<dt>处理失败</dt><dd>0 件</dd>",
        "<dt>本轮提交确认（审核中）</dt><dd>0 件</dd>", "逐商品核验尚未完成",
        "不代表其余商品不可处理", "候选扫描仅建立列表清单", "旧事件原文：扫描13件，可处理0件，失败1件",
        "下方旧汇总事件未包含", "请以本页当前台账汇总为准",
    ):
        assert expected in html
