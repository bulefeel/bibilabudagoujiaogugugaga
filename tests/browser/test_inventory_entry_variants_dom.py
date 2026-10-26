"""Observed suppressed-listing entry and item-local failures; offline DOM only."""
from dataclasses import replace

import pytest
from sqlalchemy import select

from test_inventory_gpsr_dom import BROWSER, DE, RUN, HTML, POLICY, page, renderer, opened
from test_inventory_compliance_dom import install
from tests.workflows.test_inventory_gpsr_workflow import harness
from tests.workflows.test_inventory_multisite_completion import four_site_run, CODES
from ziniao_automation.models import Run, RunEvent, Store
from ziniao_automation.workflows.amazon_inventory_gpsr.config import MARKETPLACE_LABELS, PARTY_TYPES
from ziniao_automation.workflows.amazon_inventory_gpsr.page import (
    AmazonInventoryGpsrPage, InventoryContractError, ItemLoadError,
)
from ziniao_automation.workflows.types import RunMode, SiteStatus

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(not BROWSER, reason="Requires offline fixture renderer"),
]
ENTRY_VARIANTS = (
    "解决商品信息问题", "修正刊登資訊問題",
    "修复被禁止显示的商品信息", "修正限制刊登問題",
)


async def set_entry(page, sku, label):
    await page.evaluate("""p => {
        const row = [...document.querySelectorAll('[data-sku]')].find(r => r.dataset.sku === p.sku);
        const entry = row.querySelector('kat-link');
        entry.setAttribute('label', p.label);
        entry.textContent = p.label;
    }""", {"sku": sku, "label": label})


@pytest.mark.parametrize("label", ENTRY_VARIANTS)
async def test_supported_entries_reach_only_known_compliance_form(page, label):
    adapter, items = await opened(page)
    await set_entry(page, items[0].sku, label)
    # Suppression sidebars also contain ordinary listing fields/save buttons.
    # They must never be confused with the compliance form's exact save.
    await page.evaluate("""() => {
        const violation = document.querySelector('[data-sku="SKU-GPSR"] kat-link');
        const original = violation.onclick;
        violation.onclick = event => {
            original(event);
            const body = document.querySelector('[data-testid="action-panel-body"]');
            body.insertAdjacentHTML('beforeend',
                '<input name="color" placeholder="颜色"><kat-button label="保存更改">保存更改</kat-button>');
            body.querySelector('input').oninput = () => window.fixtureClicks.push('color-edit');
            body.querySelector('kat-button').onclick = () => window.fixtureClicks.push('listing-save');
        };
    }""")
    result = await adapter.inspect_item(page, RUN, DE, items[0])
    assert result.status == "eligible"
    actions = await page.evaluate("window.fixtureClicks")
    assert actions == ["open:SKU-GPSR", "violation:SKU-GPSR", "card:SKU-GPSR"]


@pytest.mark.parametrize("label", ENTRY_VARIANTS[2:])
async def test_suppressed_listing_without_compliance_entry_never_edits_listing(page, label):
    adapter, items = await opened(page)
    await set_entry(page, items[2].sku, label)
    await page.evaluate("""() => {
        const entry = document.querySelector('[data-sku="SKU-NONE"] kat-link');
        const original = entry.onclick;
        entry.onclick = event => {
            original(event);
            const body = document.querySelector('[data-testid="action-panel-body"]');
            body.insertAdjacentHTML('beforeend',
                '<input name="color"><kat-button label="保存更改">保存更改</kat-button>');
            body.querySelector('kat-button').onclick = () => window.fixtureClicks.push('listing-save');
        };
    }""")
    result = await adapter.inspect_item(page, RUN, DE, items[2])
    assert result.status == "no_violation_entry" and result.requirements_complete
    assert await page.evaluate("window.fixtureClicks") == ["open:SKU-NONE"]


@pytest.mark.parametrize("mutation,reason", [
    ("unknown", "item_entry_unrecognized"),
    ("hidden", "item_entry_unrecognized"),
    ("duplicate", "item_entry_ambiguous"),
])
async def test_entry_failure_is_local_and_does_not_reset_previous_sidebar(page, mutation, reason):
    adapter, items = await opened(page)
    await adapter.inspect_item(page, RUN, DE, items[0])
    await page.evaluate("""kind => {
        const entry = document.querySelector('[data-sku="SKU-EPR"] kat-link');
        if (kind === 'unknown') entry.setAttribute('label', '未知的商品处理入口');
        if (kind === 'hidden') entry.hidden = true;
        if (kind === 'duplicate') entry.after(entry.cloneNode(true));
    }""", mutation)
    before = await page.evaluate("window.fixtureClicks")
    with pytest.raises(ItemLoadError) as error:
        await adapter.inspect_item(page, RUN, DE, items[1])
    assert error.value.reason_code == reason
    assert await page.evaluate("window.fixtureClicks") == before
    # Failure of this row must not prevent the next ordinary row from being read.
    assert (await adapter.inspect_item(page, RUN, DE, items[2])).status == "no_violation_entry"


@pytest.mark.parametrize("mutation", ["asin_changed", "duplicate_row"])
async def test_identity_error_stays_fatal_even_when_entry_is_unknown(page, mutation):
    adapter, items = await opened(page)
    await set_entry(page, items[0].sku, "未知入口")
    await page.evaluate("""kind => {
        const row = document.querySelector('[data-sku="SKU-GPSR"]');
        if (kind === 'asin_changed') row.querySelector('a').href = 'https://www.amazon.de/dp/B999999999';
        else row.after(row.cloneNode(true));
    }""", mutation)
    with pytest.raises(InventoryContractError):
        await adapter.inspect_item(page, RUN, DE, items[0])
    assert not await page.evaluate("window.fixtureClicks")


@pytest.mark.parametrize("mutation", ["entry", "asin"])
async def test_entry_is_rechecked_after_preparing_previous_sidebar(page, mutation):
    adapter, items = await opened(page)
    async def changed(*args):
        if mutation == "entry":
            await set_entry(page, items[0].sku, "未知入口")
        else:
            await page.evaluate("""document.querySelector('[data-sku="SKU-GPSR"] a').href =
                'https://www.amazon.de/dp/B999999999'""")
    adapter._prepare_item_entry = changed
    with pytest.raises(ItemLoadError if mutation == "entry" else InventoryContractError):
        await adapter.inspect_item(page, RUN, DE, items[0])
    assert not await page.evaluate("window.fixtureClicks")


async def test_unsupported_entry_does_not_retry_or_relabel_as_old_http403(page):
    adapter, items = await opened(page)
    await set_entry(page, items[0].sku, "未知入口")
    adapter._network_failure = 403
    with pytest.raises(ItemLoadError) as error:
        await adapter.inspect_item(page, RUN, DE, items[0])
    assert error.value.reason_code == "item_entry_unrecognized"
    assert not adapter._http_retries
    assert not await page.evaluate("window.fixtureClicks")


@pytest.mark.parametrize("label", ENTRY_VARIANTS[2:])
@pytest.mark.parametrize("kind", PARTY_TYPES)
async def test_suppressed_listing_reuses_party_barrier_and_single_submit(page, label, kind):
    adapter, items = await install(page, includeManufacturer=True,
                                   traditional=label == "修正限制刊登問題")
    await set_entry(page, items[0].sku, label)
    item = replace(items[0], requirement_type=kind)
    assert (await adapter.inspect_item(page, RUN, DE, item)).status == "eligible"
    assert not any(a.startswith(("select-", "send-")) for a in await page.evaluate("window.complianceClicks"))
    await adapter.prepare_submission(page, RUN, DE, item)
    await adapter.submit_once(page, RUN, DE, item)
    assert await adapter.confirm_submission(page, RUN, DE, item)
    with pytest.raises(InventoryContractError):
        await adapter.submit_once(page, RUN, DE, item)
    actions = await page.evaluate("window.complianceClicks")
    assert sum(a.startswith("send-") for a in actions) == 1
    assert "save-safety" not in actions


@pytest.mark.parametrize("entry", ["修复被禁止显示的商品信息", "未知入口"])
@pytest.mark.parametrize("mode", [RunMode.DRY_RUN, RunMode.AUTO])
async def test_mixed_fr_entries_continue_remaining_product_and_later_markets(page, harness, entry, mode):
    # Real workflow + SQLite + DOM adapter; only marketplace navigation is
    # replaced by local fixture rendering, with all network requests blocked.
    class FixtureMarketplaceAdapter(AmazonInventoryGpsrPage):
        def __init__(self):
            super().__init__(policy=POLICY)
            self.markets = []
            self.actions = []

        async def _switch_marketplace(self, current, run, market):
            self.markets.append(market.code)
            await current.raw.set_content(HTML)
            await current.evaluate("""label => {
                document.querySelector('.dropdown-account-switcher-header-label-regional').textContent = label;
            }""", MARKETPLACE_LABELS[market.code][0])
            if market.code == "FR":
                await set_entry(current, "SKU-EPR", entry)
            return current

        async def inspect_item(self, current, run, market, item):
            self.actions.append((market.code, item.sku))
            return await super().inspect_item(current, run, market, item)

    run = four_site_run(harness, mode)
    config = {"marketplace_codes": list(CODES), "processing_types": ["safety_attestation"]}
    with harness.factory() as session:
        session.get(Run, run.id).workflow_config = config
        session.get(Store, int(run.store.id)).expected_seller_id = RUN.store.expected_seller_id
        session.commit()
    run = replace(run, workflow_config=config,
                  store=replace(run.store, expected_seller_id=RUN.store.expected_seller_id))
    adapter = FixtureMarketplaceAdapter()
    flow = harness.workflow(adapter)
    await flow.preflight(run, page)
    plan = await flow.plan(run, page)
    if plan.lines:
        await flow.execute(run, page, plan)
    assert adapter.markets == list(CODES) * (2 if mode is RunMode.AUTO else 1)
    assert adapter.actions == [(code, sku) for code in CODES for sku in ("SKU-NONE", "SKU-EPR", "SKU-GPSR")]
    rows = await harness.ledger.list_items(run.id)
    assert len(rows) == 12 and not any(r.state in {"PENDING", "SUBMITTING", "UNCERTAIN"} for r in rows)
    assert sum(r.state == "CONFIRMED" for r in rows) == (4 if mode is RunMode.AUTO else 0)
    assert sum(r.state == "FAILED" for r in rows) == int(entry == "未知入口")
    for code in ("IT", "PL"):
        assert await harness.repo.get_site_status(run.id, code) is (
            SiteStatus.CONFIRMED if mode is RunMode.AUTO else SiteStatus.DRY_RUN_COMPLETE)
    if entry == "未知入口":
        failed = next(r for r in rows if r.state == "FAILED")
        assert failed.reason == "item_entry_unrecognized" and failed.intent_at is None
        with harness.factory() as session:
            event = session.scalar(select(RunEvent).where(
                RunEvent.run_id == run.id, RunEvent.event_type == "inventory_gpsr_failed"))
            assert "入口未识别" in event.message and "继续后续商品和站点" in event.message
