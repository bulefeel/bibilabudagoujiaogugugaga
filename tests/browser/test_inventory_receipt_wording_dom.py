"""Recorded short receipt variants; offline DOM only, no Amazon connections."""
from dataclasses import replace

import pytest

from test_inventory_compliance_dom import page, renderer, BROWSER, RUN, DE, install
from test_inventory_gpsr_dom import POLICY
from tests.workflows.test_inventory_gpsr_workflow import harness
from ziniao_automation.models import InventoryGpsrRequirement, Run, Store
from ziniao_automation.workflows.amazon_inventory_gpsr.config import (
    RESPONSIBLE_PERSON, MANUFACTURER,
)
from ziniao_automation.workflows.amazon_inventory_gpsr.page import (
    AmazonInventoryGpsrPage, InventoryContractError,
)
from ziniao_automation.workflows.amazon_inventory_gpsr.store import InventoryRunCancelled
from ziniao_automation.workflows.types import SiteStatus

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(not BROWSER, reason="Requires offline fixture renderer"),
]
OLD_RECEIPTS = ("您的提交正在审核中。", "您的提交正在審核中。")
NEW_RECEIPTS = ("您提交的内容正在审核中。", "您提交的內容正在審核中。")
PARTIES = (RESPONSIBLE_PERSON, MANUFACTURER)


async def submitted(page, kind, message):
    adapter, items = await install(
        page, includeManufacturer=True, personReceiptStyle="responsible",
        personReceiptText=message, traditional="審核" in message,
    )
    item = replace(items[0], requirement_type=kind)
    assert (await adapter.inspect_item(page, RUN, DE, item)).status == "eligible"
    await adapter.prepare_submission(page, RUN, DE, item)
    await adapter.submit_once(page, RUN, DE, item)
    return adapter, item


@pytest.mark.parametrize("kind", PARTIES)
@pytest.mark.parametrize("message", OLD_RECEIPTS + NEW_RECEIPTS)
async def test_short_receipt_confirms_and_restart_only_reads(page, kind, message):
    adapter, item = await submitted(page, kind, message)
    before = await page.evaluate("window.complianceClicks")
    assert await adapter.confirm_submission(page, RUN, DE, item)
    restarted = AmazonInventoryGpsrPage(policy=POLICY)
    restarted._remember(page, page, RUN, DE)
    assert await restarted.reconcile_item(page, RUN, DE, item)
    assert await page.evaluate("window.complianceClicks") == before
    with pytest.raises(InventoryContractError):
        await adapter.submit_once(page, RUN, DE, item)
    assert len([a for a in before if a.startswith("send-")]) == 1


@pytest.mark.parametrize("kind", PARTIES)
@pytest.mark.parametrize("change", [
    "wrong_sku", "wrong_asin", "wrong_type", "hidden", "notice",
    "send_button", "editable", "loading",
])
async def test_new_receipt_preserves_identity_type_and_completed_view_guards(page, kind, change):
    adapter, item = await submitted(page, kind, NEW_RECEIPTS[0])
    adapter._timing = replace(POLICY, confirmation_timeout_seconds=.2)
    await page.evaluate("""change => {
        const p = document.querySelector('kat-panel');
        if (change === 'wrong_sku') p.innerHTML = p.innerHTML.replace('SKU：SKU-GPSR', 'SKU：OTHER');
        if (change === 'wrong_asin') document.querySelector('[data-sku="SKU-GPSR"] a').href = 'https://www.amazon.de/dp/B999999999';
        if (change === 'wrong_type') p.querySelector('h3').textContent = '安全证明';
        if (change === 'hidden') p.querySelector('.submitted p').hidden = true;
        if (change === 'notice') p.querySelector('.submitted p').textContent = '示例：您提交的内容正在审核中。请继续填写';
        if (change === 'send_button') p.insertAdjacentHTML('beforeend', '<kat-button>送出</kat-button>');
        if (change === 'editable') p.insertAdjacentHTML('beforeend', '<input type="radio">');
        if (change === 'loading') p.insertAdjacentHTML('beforeend', '<kat-spinner></kat-spinner>');
    }""", change)
    before = await page.evaluate("window.complianceClicks")
    assert not await adapter.confirm_submission(page, RUN, DE, item)
    assert await page.evaluate("window.complianceClicks") == before


@pytest.mark.parametrize("kind", PARTIES)
@pytest.mark.parametrize("message", NEW_RECEIPTS)
async def test_cancelled_confirmation_drains_new_receipt_without_any_click(page, kind, message):
    adapter, item = await submitted(page, kind, message)
    async def cancelled(run):
        raise InventoryRunCancelled("fixture cancelled")
    adapter.cancel_check = cancelled
    before = await page.evaluate("window.complianceClicks")
    assert await adapter.confirm_submission(page, RUN, DE, item)
    assert await page.evaluate("window.complianceClicks") == before
    with pytest.raises(InventoryRunCancelled):
        await adapter._check_cancelled()


@pytest.mark.parametrize("kind", PARTIES)
@pytest.mark.parametrize("message", NEW_RECEIPTS)
async def test_preexisting_new_receipt_is_already_submitted_not_a_new_action(page, kind, message):
    adapter, items = await install(
        page, includeManufacturer=True, personInitiallyPending=True,
        manufacturerOnly=kind == MANUFACTURER, responsibleFirst=kind != MANUFACTURER,
        personReceiptText=message, traditional="審核" in message,
    )
    item = replace(items[0], requirement_type=kind)
    assert (await adapter.inspect_item(page, RUN, DE, item)).status == "already_submitted"
    assert not any(a.startswith(("send-", "select-", "save-"))
                   for a in await page.evaluate("window.complianceClicks"))
    state = await adapter._panel_snapshot(page, RUN, DE)
    assert not adapter._receipt_matches(page, items[0], state)


@pytest.mark.parametrize("message", NEW_RECEIPTS)
async def test_real_workflow_persists_confirmation_then_processes_other_requirements_and_products(
    page, harness, message,
):
    adapter, items = await install(
        page, includeManufacturer=True, personReceiptStyle="responsible",
        personReceiptText=message, traditional="審核" in message,
    )
    run = harness.new_run()
    config = {"marketplace_codes": ["DE"], "processing_types": list(PARTIES)}
    with harness.factory() as session:
        session.get(Run, run.id).workflow_config = config
        session.get(Store, int(run.store.id)).expected_seller_id = RUN.store.expected_seller_id
        session.commit()
    run = replace(run, workflow_config=config,
                  store=replace(run.store, expected_seller_id=RUN.store.expected_seller_id),
                  artifact_dir=harness.settings.evidence_dir)
    flow = harness.workflow(adapter)
    await flow.preflight(run, page)
    plan = await flow.plan(run, page)
    await flow.execute(run, page, plan)
    records = await harness.ledger.list_items(run.id)
    assert len(records) == len(items) * 2
    assert all(r.state == "CONFIRMED" and r.intent_at for r in records)
    with harness.factory() as session:
        assert all(session.get(InventoryGpsrRequirement, r.id).confirmed_at for r in records)
    counts = await harness.ledger.summary(run.id)
    assert counts["confirmed"] == 6 and counts["pending"] == counts["uncertain"] == 0
    assert await harness.repo.get_site_status(run.id, "DE") == SiteStatus.CONFIRMED
    actions = await page.evaluate("window.complianceClicks")
    for action in ("send-person", "send-manufacturer"):
        assert actions.count(action) == len(items)
    assert "save-safety" not in actions
