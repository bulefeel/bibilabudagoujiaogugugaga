"""Synthetic US/DE coverage; no Amazon connection or real disbursement."""
from dataclasses import replace
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.test_schedule_batch_api import batch_client, _payload, _seed_store
from tests.unit.test_setup_persistence import records, begin
from tests.workflows.fixture_dom import FixturePage, Locator
from tests.workflows.test_amount_and_dom import _confirmation_fixture
from tests.workflows.test_store_setup_adapter import adapter, store
from ziniao_automation.models import Schedule, OperationGuard, StoreMarketplace, Store
from ziniao_automation.repositories import StoreRepository
from ziniao_automation.workflows.amazon_disbursement import AmazonPaymentsPage
from ziniao_automation.workflows.amazon_disbursement.config import ALLOWED_MARKETPLACES
from ziniao_automation.workflows.amazon_disbursement.definition import build_amazon_disbursement_definition
from ziniao_automation.workflows.amazon_login import AmazonLoginAdvancer
from ziniao_automation.workflows.amazon_account_switcher import MARKETPLACE_NAMES
from ziniao_automation.workflows.amazon_switcher_contract import (
    AccountCandidate, MarketplaceCandidate, SwitcherSnapshot, choose_marketplace,
)
from ziniao_automation.workflows.errors import PreflightRejected
from ziniao_automation.workflows.store_setup.config import DEFAULT_SETUP_CODES

SITES = [("US", "sellercentral.amazon.com", "USD"), ("DE", "sellercentral.amazon.de", "EUR")]
FIXTURES = Path(__file__).parent / "fixtures/amazon_payments"


def refs(code, domain, currency):
    market, run, snapshot = _confirmation_fixture()
    market = replace(market, id=code, code=code, domain=domain, currency=currency)
    run = replace(run, marketplaces=(market,), store=replace(run.store, expected_seller_id="SELLER_FIXTURE"))
    snapshot = replace(snapshot, marketplace_code=code, domain=domain,
                       currency=currency, seller_id="SELLER_FIXTURE")
    return market, run, snapshot


@pytest.mark.parametrize("code,domain,currency", SITES)
async def test_dashboard_and_details_read_original_currency_with_no_clicks(code, domain, currency, monkeypatch):
    market, run, expected = refs(code, domain, currency)
    subject = AmazonPaymentsPage(pacing_range_ms=(0, 0))
    html = (FIXTURES / "dashboard_en_AU.html").read_text(encoding="utf8")
    html = html.replace("AUD $", "$" if code == "US" else "EUR €")
    if code == "DE":
        html = html.replace("25.10", "25,10").replace("100.00", "100,00").replace("125.10", "125,10").replace("0.00", "0,00")
    page = FixturePage(html, market.payments_url)
    async def no_navigation(*args, **kwargs): pass
    async def forbidden(*args, **kwargs): raise AssertionError("read-only fixture clicked")
    page.goto = no_navigation
    monkeypatch.setattr(Locator, "click", forbidden)
    result = await subject.read_snapshot(page, run, market)
    assert (result.marketplace_code, result.currency, result.payable_amount) == (code, currency, Decimal("25.10"))
    assert result.delayed_amount == 0
    details = (FIXTURES / "details_en_CA.html").read_text(encoding="utf8").replace(
        "CA$2.89", "$1,234.56" if code == "US" else "1.234,56 €")
    page = FixturePage(details, f"https://{domain}/payments/disburse/details?accountType=PAYABLE")
    result = await subject._verify_details(page, market, expected)
    assert (result.currency, result.payable_amount, result.payment_account) == (currency, Decimal("1234.56"), "493")


@pytest.mark.parametrize("code,domain,currency", SITES)
@pytest.mark.parametrize("invalid", ["domain", "currency", "identity", "disabled"])
def test_new_sites_keep_static_preflight_guards(code, domain, currency, invalid):
    market, run, _ = refs(code, domain, currency)
    if invalid == "domain": market = replace(market, domain=domain + ".invalid")
    elif invalid == "currency": market = replace(market, currency="GBP")
    elif invalid == "identity": run = replace(run, store=replace(run.store, identity_confirmed=False))
    else: market = replace(market, enabled=False)
    with pytest.raises(PreflightRejected):
        AmazonPaymentsPage()._validate_static_configuration(run, market)


@pytest.mark.parametrize("code,domain,currency", SITES)
def test_login_handoff_requires_the_selected_exact_host(code, domain, currency):
    check = AmazonLoginAdvancer.is_supported_business_url
    assert check(f"https://{domain}/payments/dashboard/index.html", expected_host=domain)
    details = f"https://{domain}/payments/disburse/details"
    assert not check(details, expected_host=domain)
    assert check(details, expected_host=domain, allow_payment_details_handoff=True)
    assert not check(details, expected_host="sellercentral.amazon.ca", allow_payment_details_handoff=True)
    assert not check(details.replace(domain, domain+".invalid"), expected_host=domain, allow_payment_details_handoff=True)


@pytest.mark.parametrize("label", ["美国", "美國", "United States"])
def test_us_switcher_chooses_exact_country(label):
    snap = SwitcherSnapshot((AccountCandidate("Fixture (current)"),), (
        MarketplaceCandidate(0, 0, "Canada"), MarketplaceCandidate(1, 0, label),
    ), True)
    assert choose_marketplace(snap, MARKETPLACE_NAMES["sellercentral.amazon.com"])[0] == 1


async def test_us_onboarding_only_reads_dashboard(monkeypatch):
    subject, nav = adapter((FIXTURES / "dashboard_en_AU.html").read_text(encoding="utf8"))
    market, _, _ = refs(*SITES[0])
    async def forbidden(*args, **kwargs): raise AssertionError("onboarding clicked")
    monkeypatch.setattr(Locator, "click", forbidden)
    result = await subject.check_site(nav.page, store(), market)
    assert result.payment_capability == "SUPPORTED"
    assert nav.paths == [market.payments_url]


def test_setup_batch_accepts_explicit_us_de_without_expanding_defaults(records):
    repo, sessions, sid = records
    assert DEFAULT_SETUP_CODES == ("CA", "UK", "AU")
    target = repo.preview([sid], "fill_missing", {sid: ["US", "DE"]})["targets"][0]
    assert target["eligible"] and target["marketplace_codes"] == ["US", "DE"]
    assert not target["defaulted"]
    row = begin(repo, sid, ["US", "DE"])
    assert {site["code"] for site in row["marketplaces"]} == {"US", "DE"}
    with sessions() as db:
        assert {site.code for site in db.scalars(select(StoreMarketplace))} == {"US", "DE"}
        assert not list(db.scalars(select(OperationGuard)))


@pytest.mark.parametrize("mode", ["dry_run", "approval", "auto"])
def test_api_batch_schedules_new_sites_and_preserves_queued_snapshot(batch_client, mode):
    app, client, headers, refresh = batch_client
    ids = [_seed_store(app, name=name) for name in ("US-DE-A", "US-DE-B")]
    for sid in ids:
        response = client.patch(f"/api/stores/{sid}", headers=headers, json={
            "marketplaces": [{"code": code, "enabled": True} for code in ("CA", "US", "DE")]})
        assert response.status_code == 200, response.text
    payload = _payload(ids, request_id=str(uuid4()))
    payload["template"]["mode"] = mode
    payload["template"]["workflow_config"] = {"marketplace_codes": ["US", "DE"]}
    preview = client.post("/api/schedules/batch/preview", headers=headers, json=payload)
    assert preview.status_code == 200 and preview.json()["eligible"], preview.text
    response = client.post("/api/schedules/batch", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    assert refresh.calls == 1
    with app.state.sessions() as db:
        rows = list(db.scalars(select(Schedule).order_by(Schedule.batch_order)))
        assert len(rows) == 2
        assert all(s.workflow_config == {"marketplace_codes": ["US", "DE"]} for s in rows)
    # Creating a run only queues through FakeAutomation; no browser is opened.
    response = client.post("/api/runs", headers=headers, json={
        "store_id": ids[0], "workflow": "amazon_disbursement", "mode": mode,
        "workflow_config": {"marketplace_codes": ["US", "DE"]}})
    assert response.status_code in (200, 201, 202), response.text
    run_id = response.json()["id"]
    response = client.patch(f"/api/schedules/{rows[0].id}", headers=headers, json={
        "workflow_config": {"marketplace_codes": ["CA"]}, "marketplace_codes": ["CA"]})
    assert response.status_code == 200, response.text
    queued = client.get(f"/api/runs/{run_id}").json()
    assert queued["workflow_config"] == {"marketplace_codes": ["US", "DE"]}


def test_metadata_expands_only_disbursement_and_html_enables_new_sites(batch_client):
    app, client, _, _ = batch_client
    from ziniao_automation.workflows.amazon_feedback.definition import build_amazon_feedback_definition
    definition = build_amazon_disbursement_definition()
    assert definition.supported_marketplace_codes() == set(ALLOWED_MARKETPLACES)
    assert not {"US", "DE"} & build_amazon_feedback_definition().supported_marketplace_codes()
    store_ids = []
    for code, domain, currency in SITES:
        sid = _seed_store(app, name="only-" + code)
        store_ids.append(str(sid))
        with app.state.sessions.begin() as db:
            db.get(Store, sid).raw_profile = {"platform_id": "1"}
            StoreRepository(db).replace_marketplaces(sid, [{"code": code, "enabled": True}])
    html = client.get("/stores").text
    assert 'data-store-marketplace="US"' in html
    assert 'data-store-marketplace="DE"' in html
    class Buttons(HTMLParser):
        def __init__(self):
            super().__init__()
            self.enabled = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "button" and attrs.get("data-action") == "new-run" and "disabled" not in attrs:
                self.enabled.append(attrs.get("data-store-id"))
    buttons = Buttons()
    buttons.feed(html)
    assert set(store_ids).issubset(buttons.enabled)
    meta = client.get("/api/workflows").json()[0]
    assert set(meta["config_schema"]["properties"]["marketplace_codes"]["items"]["enum"]) == set(ALLOWED_MARKETPLACES)
