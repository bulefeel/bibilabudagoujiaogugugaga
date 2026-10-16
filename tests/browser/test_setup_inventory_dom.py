"""Real DOM engine, offline fixtures only; no Amazon network or submissions."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_inventory_gpsr_dom import page, renderer, BROWSER, POLICY
from ziniao_automation.config import PROJECT_ROOT
from ziniao_automation.workflows.amazon_inventory_gpsr.page import AmazonInventoryGpsrPage
from ziniao_automation.workflows.amazon_inventory_gpsr.config import INVENTORY_URL
from ziniao_automation.workflows.store_setup.inventory_adapter import InventoryStoreSetupPage
from ziniao_automation.workflows.store_setup.adapter import AmazonStoreSetupPage
from ziniao_automation.workflows.store_setup.policy import OnboardingPolicy
from ziniao_automation.workflows.store_setup.identity import SetupRejected
from ziniao_automation.workflows.store_setup.config import SETUP_MARKETPLACES
from ziniao_automation.workflows.types import MarketplaceRef

pytestmark = [pytest.mark.asyncio(loop_scope='module'),
              pytest.mark.skipif(not BROWSER, reason='Requires offline fixture renderer')]


def subject(page):
    page.is_closed = lambda: False
    policy = OnboardingPolicy.load(PROJECT_ROOT/'onboarding-policy.toml').model_copy(update={
        'candidate_ready_timeout_ms': 1000, 'sample_interval_ms': 10,
    })
    inventory = AmazonInventoryGpsrPage(policy=replace(POLICY, list_timeout_seconds=.5,
        panel_timeout_seconds=.5, action_interval_seconds=0))
    return InventoryStoreSetupPage(policy, inventory=inventory)


def site(code):
    domain, currency = SETUP_MARKETPLACES[code]
    return MarketplaceRef(id=code, code=code, domain=domain, currency=currency)


STORE = SimpleNamespace(expected_seller_id='fixture seller', identity_confirmed=True)


@pytest.mark.parametrize('code,label', [('DE','德国'),('IT','義大利'),('FR','法國'),('ES','西班牙'),('PL','波蘭')])
async def test_eu_setup_bootstraps_identity_then_switches_country_not_domain(page, code, label):
    page.url = INVENTORY_URL.replace('amazon.de', 'amazon.it')
    await page.evaluate('''label=>{
      window.setupClicks=[];
      const root=document.querySelector('#dropdown-account-switcher-container');
      root.insertAdjacentHTML('beforeend','<button class="dropdown-account-switcher-header">country</button>');
      root.querySelector('button').onclick=()=>{
        window.setupClicks.push('menu');
        const opt=document.createElement('button');opt.className='dropdown-account-switcher-list-item';
        opt.title=label;opt.textContent=label;root.append(opt);
        opt.onclick=()=>{window.setupClicks.push('country');root.querySelector('.dropdown-account-switcher-header-label-regional').textContent=label;opt.remove();};
      };
      // Incomplete lazy page is sufficient for ACCESS, not a candidate scan.
      document.querySelector('kat-pagination').setAttribute('total-items','230');
    }''',label)
    adapter = subject(page)
    async def forbidden(*args, **kwargs): raise AssertionError('onboarding must not scan or submit')
    for name in ('scan_candidates', '_wait_list', 'inspect_item', 'submit_once'):
        setattr(adapter.inventory, name, forbidden)
    adapter.inventory.landing.land_on = forbidden
    seller, _ = await adapter.detect_identity(page, site(code))
    assert seller == 'fixture seller'
    result = await adapter.check_site(page, STORE, site(code))
    assert result.access_check == 'PASSED' and result.payment_capability == 'NOT_CHECKED'
    assert 'amazon.it' in page.url
    assert await page.evaluate('window.fixtureClicks') == []
    assert await page.evaluate('window.setupClicks') == ([] if code == 'DE' else ['menu','country'])


async def test_wrong_seller_stops_before_marketplace_click(page):
    adapter = subject(page)
    with pytest.raises(SetupRejected) as error:
        await adapter.check_site(page, SimpleNamespace(expected_seller_id='OTHER', identity_confirmed=True), site('DE'))
    assert error.value.diagnostic.reason_code == 'IDENTITY_MISMATCH'
    assert await page.evaluate('window.fixtureClicks') == []


async def test_empty_inventory_without_pagination_is_valid_access(page):
    await page.evaluate("document.querySelector('#rows').innerHTML='';document.querySelector('kat-pagination').remove();document.body.insertAdjacentHTML('beforeend','<p>0 - 0 共 0 個</p>')")
    assert (await subject(page).check_site(page, STORE, site('DE'))).access_check == 'PASSED'


async def test_wrong_filter_does_not_report_pass(page):
    await page.evaluate("document.querySelector('kat-dropdown').setAttribute('value','All')")
    with pytest.raises(SetupRejected) as error:
        await subject(page).check_site(page, STORE, site('DE'))
    assert error.value.diagnostic.reason_code == 'INVENTORY_ACCESS_UNPROVEN'


async def test_login_new_page_is_retained_by_facade(page):
    from test_inventory_gpsr_dom import OfflinePage
    adapter = subject(page)
    old = OfflinePage(page.raw)
    old.url = 'https://sellercentral.amazon.de/ap/signin'
    old.is_closed = lambda: False
    adapter.inventory.login.advance_or_raise = AsyncMock(return_value=page)
    facade = AmazonStoreSetupPage(adapter.policy)
    facade._inventory_setup = adapter
    assert (await facade.detect_identity(old, site('DE')))[0] == 'fixture seller'
    assert facade.resolved_page_for(old) is page
    assert (await facade.check_site(old, STORE, site('DE'))).payment_capability == 'NOT_CHECKED'
    adapter.inventory.login.advance_or_raise.assert_awaited_once()


@pytest.mark.parametrize('url', [
    'https://sellercentral.amazon.it.evil.test/myinventory/inventory',
    'http://sellercentral.amazon.it/myinventory/inventory',
    'https://sellercentral.amazon.it/myinventory/inventory/',
])
async def test_route_not_accepted_as_inventory_evidence(page, url):
    adapter = subject(page)
    page.url = url
    with pytest.raises(SetupRejected):
        await adapter._stable(page, site('IT'))
