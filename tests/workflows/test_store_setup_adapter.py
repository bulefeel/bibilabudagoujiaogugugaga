"""Saved payments fixtures plus explicitly synthetic identity boundary cases.

Synthetic switcher cases are NOT evidence for the failing S01/S02 live layout.
No test launches a browser or connects to Amazon.
"""
from pathlib import Path
import asyncio
from dataclasses import replace

import pytest

from ziniao_automation.config import PROJECT_ROOT
from ziniao_automation.workflows.amazon_disbursement.page import AmazonPaymentsPage
from ziniao_automation.workflows.amazon_disbursement.config import ALLOWED_MARKETPLACES
from ziniao_automation.workflows.errors import DomContractError
from ziniao_automation.workflows.store_setup.adapter import AmazonStoreSetupPage
from ziniao_automation.workflows.store_setup.identity import IdentityObservation, SetupRejected
from ziniao_automation.workflows.store_setup.policy import OnboardingPolicy
from ziniao_automation.workflows.types import MarketplaceRef, StoreRef, WorkflowRun, RunMode
from tests.workflows.fixture_dom import FixturePage, Locator

FIXTURES = Path(__file__).parents[1] / "fixtures" / "amazon_payments"
POLICY = PROJECT_ROOT / "onboarding-policy.toml"
SHELL = '<button class="full-page-account-switcher-account-details">Canada (current)</button><kat-button data-test="confirm-selection"></kat-button>'
META = '<meta name="merchant-id" content="SELLER_FIXTURE">'


def switcher_tree(account_button):
    return ('<body><div class="full-page-account-switcher"><div class="full-page-account-switcher-inner">'
            '<div class="full-page-account-switcher-accounts-wrapper"><div class="full-page-account-switcher-accounts">'
            '<div class="full-page-account-switcher-account">' + account_button
            + '<div class="full-page-account-switcher-accounts"><div class="full-page-account-switcher-account">'
            '<button class="full-page-account-switcher-account-details full-page-account-switcher-account-selected">Canada (current)</button>'
            '</div></div></div></div></div></div></div></body>')


def policy(**overrides):
    values = OnboardingPolicy.load(POLICY).model_dump()
    values.update(candidate_ready_timeout_ms=250, sample_interval_ms=1)
    values.update(overrides)
    return OnboardingPolicy.model_validate(values)


def site(code="CA"):
    domain, currency = ALLOWED_MARKETPLACES[code]
    return MarketplaceRef(id=code, code=code, domain=domain, currency=currency)


def store(expected="SELLER_FIXTURE"):
    return StoreRef(id="1", name="fixture", selector_type="oauth", selector_value="fixture",
                    expected_seller_id=expected, identity_confirmed=True, enabled=False)


class Navigation(AmazonPaymentsPage):
    def __init__(self, dashboard, switcher=None, *, returned=None):
        super().__init__(pacing_range_ms=(0, 0))
        self.dashboard = dashboard
        self.switcher = switcher if switcher is not None else '<body></body>'
        self.returned = returned
        self.paths = []
        self.auth_options = []
        self.page = FixturePage('<body></body>', site().payments_url)

    async def _goto(self, page, url):
        self.paths.append(url)
        if '/account-switcher/' in url:
            html = self.switcher
        elif len(self.paths) > 1 and self.returned is not None:
            html = self.returned
        else:
            html = self.dashboard
        FixturePage.__init__(self.page, html, url)

    async def _raise_if_auth(self, page, marketplace, **kwargs):
        self.auth_options.append(kwargs)
        return self.page

    async def _goto_dashboard(self, page, marketplace):
        await self._goto(page, marketplace.payments_url)

    async def _verify_identity(self, page, expected, marketplace):
        return expected, 'fixture'


def adapter(dashboard, switcher=None, **kwargs):
    navigation = Navigation(dashboard, switcher, **kwargs)
    subject = AmazonStoreSetupPage(policy(), navigation=navigation)
    return subject, navigation


@pytest.mark.parametrize("filename,code,capability", [
    ("dashboard_zh_CA.html", "CA", "SUPPORTED"),
    ("dashboard_en_AU.html", "AU", "SUPPORTED"),
    ("dashboard_no_data_UK.html", "UK", "NO_PAYMENT_DATA"),
])
async def test_saved_payment_pages_read_only(filename, code, capability, monkeypatch):
    subject, nav = adapter((FIXTURES / filename).read_text(encoding="utf-8"))
    async def forbidden(*args, **kwargs):
        raise AssertionError("onboarding must not enter a financial action")
    monkeypatch.setattr(Locator, 'click', forbidden)
    for name in ('preflight', 'read_snapshot', 'submit_once', '_verify_identity'):
        monkeypatch.setattr(nav, name, forbidden)
    result = await subject.check_site(nav.page, store(), site(code))
    assert result.identity_check == result.access_check == 'PASSED'
    assert result.payment_capability == capability
    assert len(nav.paths) == 1
    assert not hasattr(subject, 'submit_once')


async def test_missing_balance_with_explicit_account_and_marketplace_header():
    subject, nav = adapter('<body><div class="partner-info">Fixture Store | Canada</div></body>')
    result = await subject.check_site(nav.page, store('Fixture Store'), site())
    assert result.payment_capability == 'UNKNOWN'
    assert result.access_check == 'PASSED'
    # The financial preflight remains strict for exactly this page.
    run = WorkflowRun(id='fixture', workflow='amazon_disbursement', mode=RunMode.DRY_RUN,
                      store=replace(store('Fixture Store'), enabled=True), marketplaces=(site(),))
    with pytest.raises(DomContractError):
        await nav.preflight(nav.page, run, site())


@pytest.mark.parametrize('code,label', [('US','United States'), ('DE','Germany')])
async def test_payment_sites_do_not_require_inventory_capability(code, label, monkeypatch):
    subject, nav = adapter(f'<body><div class="partner-info">Fixture Store | {label}</div></body>')
    def forbidden():
        raise AssertionError('payment setup must not navigate to inventory')
    monkeypatch.setattr(subject, '_inventory_adapter', forbidden)
    assert (await subject.detect_identity(nav.page, site(code)))[0] == 'Fixture Store'
    result = await subject.check_site(nav.page, store('Fixture Store'), site(code))
    assert result.identity_check == result.access_check == 'PASSED'
    assert result.payment_capability == 'UNKNOWN'
    assert result.inventory_capability == 'NOT_CHECKED'
    assert all(path == site(code).payments_url for path in nav.paths)


@pytest.mark.parametrize('html', [
    f'<head>{META}</head><body></body>',
    '<body><div class="partner-info">Fixture Store</div></body>',
    '<body><div class="partner-info">Fixture Store | Australia</div></body>',
])
async def test_url_or_identity_alone_does_not_prove_access(html):
    subject, nav = adapter(html)
    expected = 'SELLER_FIXTURE' if META in html else 'Fixture Store'
    with pytest.raises(SetupRejected) as raised:
        await subject.check_site(nav.page, store(expected), site())
    assert raised.value.diagnostic.reason_code == 'SITE_ACCESS_UNPROVEN'


async def test_stable_nonempty_candidates_required_even_without_loading(monkeypatch):
    subject, nav = adapter('')
    samples = [IdentityObservation(), IdentityObservation(),
               IdentityObservation(visible=(("Fixture", "dom:visible_account"),), container_present=True)]
    calls = 0
    async def sample(*args, **kwargs):
        nonlocal calls
        value = samples[min(calls, len(samples)-1)]
        calls += 1
        return value
    monkeypatch.setattr(subject, '_sample', sample)
    observed = await subject._stable(nav.page, site())
    assert calls == 4
    assert observed.choose()[0] == 'Fixture'


@pytest.mark.parametrize('observation,reason', [
    (IdentityObservation(loading=True), 'IDENTITY_PAGE_NOT_READY'),
    (IdentityObservation(container_present=True), 'IDENTITY_CURRENT_NOT_FOUND'),
    (IdentityObservation(), 'IDENTITY_LAYOUT_UNKNOWN'),
    (IdentityObservation(visible=(("A", "dom:visible_account"), ("B", "dom:visible_account")), container_present=True), 'IDENTITY_AMBIGUOUS'),
])
async def test_structured_identity_failures(monkeypatch, observation, reason):
    subject, nav = adapter('')
    async def sample(*args, **kwargs):
        return observation
    monkeypatch.setattr(subject, '_sample', sample)
    with pytest.raises(SetupRejected) as raised:
        await subject._stable(nav.page, site())
    assert raised.value.diagnostic.reason_code == reason


async def test_unstable_and_hung_dom_reads_have_a_real_deadline(monkeypatch):
    subject, nav = adapter('')
    count = 0
    async def unstable(*args, **kwargs):
        nonlocal count
        count += 1
        return IdentityObservation(visible=((str(count), 'dom:visible_account'),), container_present=True)
    monkeypatch.setattr(subject, '_sample', unstable)
    with pytest.raises(SetupRejected) as raised:
        await asyncio.wait_for(subject._stable(nav.page, site()), timeout=1)
    assert raised.value.diagnostic.reason_code == 'IDENTITY_PAGE_NOT_READY'
    cancelled = asyncio.Event()
    async def hung(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(subject, '_sample', hung)
    with pytest.raises(SetupRejected) as raised:
        await asyncio.wait_for(subject._stable(nav.page, site()), timeout=1)
    assert raised.value.diagnostic.reason_code == 'IDENTITY_PAGE_NOT_READY'
    assert cancelled.is_set()


@pytest.mark.parametrize('buttons,expected', [
    ('<button class="full-page-account-switcher-account-details">Fixture (current)</button>', 'Fixture'),
    ('<button class="full-page-account-switcher-account-details">Fixture （ 当前 ）</button>', 'Fixture'),
    ('<button>Canada (current)</button>', None),
    ('<button>Fixture</button>', None),
    ('<div role="button">Fixture (current)</div>', None),
    ('<button>Fixture (目前)</button>', None),
    ('<button>Fixture (current)</button>', None),
])
async def test_legacy_current_row_contract_is_not_broadened(buttons, expected):
    subject, nav = adapter('<body></body>', switcher_tree(buttons))
    if expected is None:
        with pytest.raises(SetupRejected):
            await subject.detect_identity(nav.page, site())
    else:
        value, source = await subject.detect_identity(nav.page, site())
        assert (value, source) == (expected, 'account_switcher:current')
    assert all(option.get('select_marketplace_on_switcher') is False for option in nav.auth_options)
    assert not any('/disburse/' in path for path in nav.paths)


async def test_unknown_switcher_layout_not_recognized_by_a_lone_current_button():
    subject, nav = adapter('<body></body>', '<body><button>Fixture (current)</button></body>')
    with pytest.raises(SetupRejected) as raised:
        await subject.detect_identity(nav.page, site())
    assert raised.value.diagnostic.reason_code == 'IDENTITY_LAYOUT_UNKNOWN'


async def test_ambiguous_identity_never_falls_back():
    subject, nav = adapter('<body><div class="partner-info">A</div><div class="partner-info">B</div></body>')
    with pytest.raises(SetupRejected) as raised:
        await subject.detect_identity(nav.page, site())
    assert raised.value.stop_store
    assert raised.value.diagnostic.reason_code == 'IDENTITY_AMBIGUOUS'
    assert len(nav.paths) == 1


async def test_bound_machine_and_display_name_are_not_compared_as_two_sellers():
    subject, nav = adapter(f'<head>{META}</head><body><div class="partner-info">Fixture Store | Canada</div><div class="multi-row-card-row"></div></body>')
    result = await subject.check_site(nav.page, store('Fixture Store'), site())
    assert result.identity_check == 'PASSED'


async def test_explicit_conflict_not_hidden_by_fallback():
    subject, nav = adapter('<body><div class="partner-info">Wrong Store</div></body>',
                           switcher_tree('<button class="full-page-account-switcher-account-details">Expected Store (current)</button>'))
    with pytest.raises(SetupRejected) as raised:
        await subject.check_site(nav.page, store('Expected Store'), site())
    assert raised.value.diagnostic.reason_code == 'IDENTITY_MISMATCH'
    assert len(nav.paths) == 2


async def test_machine_identity_changed_after_switcher_is_fatal():
    subject, nav = adapter(f'<head>{META}</head><body><div class="multi-row-card-row"></div></body>',
                           switcher_tree('<button class="full-page-account-switcher-account-details">Fixture Store (current)</button>'),
                           returned='<head><meta name="merchant-id" content="OTHER"></head><body><div class="multi-row-card-row"></div></body>')
    with pytest.raises(SetupRejected) as raised:
        await subject.check_site(nav.page, store('Fixture Store'), site())
    assert raised.value.diagnostic.reason_code == 'IDENTITY_MISMATCH'


async def test_fresh_fallback_can_preserve_a_display_name_binding():
    subject, nav = adapter(f'<head>{META}</head><body><div class="multi-row-card-row"></div></body>',
                           switcher_tree('<button class="full-page-account-switcher-account-details">Fixture Store (current)</button>'))
    result = await subject.check_site(nav.page, store('Fixture Store'), site())
    assert result.identity_check == 'PASSED'
    assert len(nav.paths) == 3


@pytest.mark.parametrize('url,reason', [
    ('https://sellercentral.amazon.ca.evil.test/payments/dashboard/index.html', 'DOMAIN_MISMATCH'),
    ('http://sellercentral.amazon.ca/payments/dashboard/index.html', 'DOMAIN_MISMATCH'),
    ('https://user@sellercentral.amazon.ca/payments/dashboard/index.html', 'DOMAIN_MISMATCH'),
    ('https://sellercentral.amazon.ca:9876/payments/dashboard/index.html', 'DOMAIN_MISMATCH'),
    ('https://sellercentral.amazon.ca/inactiveAccount', 'INACTIVE_ACCOUNT'),
    ('https://sellercentral.amazon.ca/payments/disburse/details', 'UNKNOWN_PAGE'),
    ('https://sellercentral.amazon.ca/unrecognized', 'UNKNOWN_PAGE'),
])
async def test_untrusted_or_non_business_landing_rejected(url, reason, monkeypatch):
    subject, nav = adapter(f'<head>{META}</head><body></body>')
    async def redirected(page, marketplace, **kwargs):
        nav.page.url = url
        return nav.page
    monkeypatch.setattr(nav, '_raise_if_auth', redirected)
    with pytest.raises(SetupRejected) as raised:
        await subject.check_site(nav.page, store(), site())
    assert raised.value.diagnostic.reason_code == reason


async def test_closed_page_is_not_an_empty_identity(monkeypatch):
    subject, nav = adapter('')
    nav.page.is_closed = lambda: True
    with pytest.raises(SetupRejected) as raised:
        await subject._stable(nav.page, site())
    assert raised.value.diagnostic.reason_code == 'SESSION_CLOSED'
    assert raised.value.stop_store


async def test_candidate_limit_never_returns_partial_identity():
    subject, nav = adapter('<body>' + '<div class="partner-info">A</div>' * 51 + '</body>')
    with pytest.raises(SetupRejected) as raised:
        await subject.detect_identity(nav.page, site())
    assert raised.value.diagnostic.reason_code == 'IDENTITY_CANDIDATE_LIMIT'
    assert len(nav.paths) == 1


async def test_new_tag_is_adopted_without_payment_or_cached_identity(monkeypatch):
    subject, nav = adapter((FIXTURES / 'dashboard_zh_CA.html').read_text(encoding='utf-8'))
    original = FixturePage('<body></body>', site().payments_url)
    result = await subject.check_site(original, store(), site())
    assert nav.page is not original
    assert result.identity_check == 'PASSED'


def test_normalization_preserves_punctuation_and_machine_vs_display():
    value = IdentityObservation(machine=(("MACHINE", "dom:content"),),
                                visible=((" A-cme (West) ", "dom:visible_account"),), container_present=True)
    assert value.choose('a-cme (west)')[0].strip() == 'A-cme (West)'
    with pytest.raises(SetupRejected):
        value.choose('Acme West')


async def test_login_redirect_during_capability_read_is_not_success(monkeypatch):
    subject, nav = adapter(f'<head>{META}</head><body><div class="multi-row-card-row"></div></body>')
    async def redirected(page):
        page.url = f'https://{site().domain}/ap/signin'
        return 'SUPPORTED'
    monkeypatch.setattr(subject, '_payment_capability', redirected)
    with pytest.raises(SetupRejected):
        await subject.check_site(nav.page, store(), site())


async def test_background_identity_change_during_structure_read_is_rejected(monkeypatch):
    subject, nav = adapter(f'<head>{META}</head><body><div class="multi-row-card-row"></div></body>')
    async def changed(page):
        FixturePage.__init__(page, '<meta name="merchant-id" content="OTHER"><body></body>', site().payments_url)
        return 'SUPPORTED'
    monkeypatch.setattr(subject, '_payment_capability', changed)
    with pytest.raises(SetupRejected) as raised:
        await subject.check_site(nav.page, store(), site())
    assert raised.value.diagnostic.reason_code == 'IDENTITY_MISMATCH'


async def test_slow_business_structure_waits_for_evidence(monkeypatch):
    subject, nav = adapter(f'<head>{META}</head><body></body>')
    samples = []
    async def late_structure(page):
        samples.append(True)
        return 'SUPPORTED' if len(samples) >= 3 else 'UNKNOWN'
    monkeypatch.setattr(subject, '_payment_capability', late_structure)
    result = await subject.check_site(nav.page, store(), site())
    assert result.payment_capability == 'SUPPORTED'
    assert len(samples) == 3


def test_success_result_rejects_unknown_or_failed_check_states():
    from ziniao_automation.workflows.store_setup.adapter import SiteSetupResult
    with pytest.raises(ValueError):
        SiteSetupResult('FAILED', 'PASSED', 'SUPPORTED', 'fixture')
    with pytest.raises(ValueError):
        SiteSetupResult('PASSED', 'PASSED', 'made-up', 'fixture')


async def test_identity_failure_and_evidence_do_not_leak_values_or_urls():
    subject, nav = adapter('<body><div class="partner-info">SECRET_ONE</div><div class="partner-info">SECRET_TWO</div></body>')
    with pytest.raises(SetupRejected) as raised:
        await subject.detect_identity(nav.page, site())
    assert 'SECRET' not in str(raised.value)
    assert 'SECRET' not in str(raised.value.diagnostic.to_payload())
    observed = IdentityObservation(machine=(("SECRET_ONE", "url:TOKEN=SECRET_TWO"),), container_present=True)
    identity = subject._record(observed, observed.choose())
    assert identity[0] == 'SECRET_ONE'  # only the local binding value retains it
    assert identity[1] == 'url:merchant_identity'
    assert 'SECRET' not in str(subject.last_identity_evidence)


@pytest.mark.parametrize('entry', ['dashboard', 'switcher'])
@pytest.mark.parametrize('new_tab', [False, True])
@pytest.mark.parametrize('landing,reason', [
    ('https://sellercentral.amazon.ca/inactiveAccount', 'INACTIVE_ACCOUNT'),
    ('https://sellercentral.amazon.ca.evil.test/inactiveAccount', 'DOMAIN_MISMATCH'),
    ('https://sellercentral.amazon.ca/ap/mfa', None),
    ('https://sellercentral.amazon.ca/inactiveAccount/extra', None),
])
async def test_login_exception_reclassifies_only_proven_status_landing(monkeypatch, entry, new_tab, landing, reason):
    from ziniao_automation.workflows.errors import HumanAuthRequired
    subject, nav = adapter('')
    challenge = HumanAuthRequired('fixture', reason_code='AUTH_PAGE_CHANGED', stage='otp_signin', attempts=1)
    original = nav.page
    async def redirected(page, marketplace, **kw):
        if new_tab:
            nav.page = FixturePage('<body></body>', landing)
        else:
            page.url = landing
        raise challenge
    monkeypatch.setattr(nav, '_raise_if_auth', redirected)
    monkeypatch.setattr(nav, 'resolved_page_for', lambda page: nav.page)
    async def operation():
        if entry == 'dashboard':
            return await subject._open_dashboard(original, site(), select_site=True)
        return await subject._switcher_identity(original, site())
    if reason:
        with pytest.raises(SetupRejected) as exc: await operation()
        assert exc.value.diagnostic.reason_code == reason
        assert exc.value.diagnostic.page_category == reason
    else:
        with pytest.raises(HumanAuthRequired) as exc: await operation()
        assert exc.value is challenge


@pytest.mark.parametrize('method', ['detect_identity', 'check_site'])
@pytest.mark.parametrize('closed', [True, False])
async def test_protocol_error_classified_as_closed_only_with_actual_closed_page(monkeypatch, method, closed):
    from playwright.async_api import Error
    subject, nav = adapter('')
    error = Error('fixture protocol error, never copied to diagnostic')
    async def gone(page, url):
        page.is_closed = lambda: closed
        raise error
    monkeypatch.setattr(nav, '_goto', gone)
    args = (nav.page, site()) if method == 'detect_identity' else (nav.page, store(), site())
    if closed:
        with pytest.raises(SetupRejected) as exc: await getattr(subject, method)(*args)
        assert exc.value.diagnostic.reason_code == 'SESSION_CLOSED'
        assert exc.value.stop_store
    else:
        with pytest.raises(Error) as exc: await getattr(subject, method)(*args)
        assert exc.value is error


async def test_actual_s06_landing_record_does_not_become_an_otp_wait(monkeypatch):
    import json
    from ziniao_automation.workflows.errors import HumanAuthRequired
    fixture = Path(__file__).parents[1] / 'fixtures/store_setup_live_landings/observations.json'
    capture = json.loads(fixture.read_text(encoding='utf-8'))
    observed = next(row for row in capture['observations'] if row['sample'] == 'S06')
    assert capture['source_kind'] == 'actual_ziniao_url_and_diagnostic_observation'
    assert capture['contains_page_html'] is False
    subject, nav = adapter('')
    async def login_landing(page, marketplace, **kw):
        page.url = 'https://' + observed['page']['host'] + observed['page']['path']
        raise HumanAuthRequired('recorded stop', **observed['diagnostic'])
    monkeypatch.setattr(nav, '_raise_if_auth', login_landing)
    with pytest.raises(SetupRejected) as exc:
        await subject._open_dashboard(nav.page, site('UK'), select_site=True)
    assert exc.value.diagnostic.reason_code == 'INACTIVE_ACCOUNT'


async def test_account_without_current_marker_enters_exact_site_before_binding(monkeypatch):
    tree = switcher_tree('<button class="full-page-account-switcher-account-details">Fixture Store</button>')
    subject, nav = adapter('<body></body>', tree)
    calls = []
    async def redirected(page, marketplace, **kwargs):
        select_site = kwargs.get("select_marketplace_on_switcher")
        calls.append(select_site)
        if select_site:
            FixturePage.__init__(nav.page, '<div class="partner-info">Fixture Store | Canada</div>', marketplace.payments_url)
        else:
            FixturePage.__init__(nav.page, tree, f"https://{marketplace.domain}/account-switcher/default/merchantMarketplace?returnTo=%2Fpayments%2Fdashboard%2Findex.html")
        return nav.page
    monkeypatch.setattr(nav, "_raise_if_auth", redirected)
    assert await subject.detect_identity(nav.page, site()) == ("Fixture Store", "dom:visible_account")
    assert True in calls
    assert calls.index(True) > 0


async def test_site_selection_without_current_marker_does_not_guess_identity(monkeypatch):
    tree = switcher_tree('<button class="full-page-account-switcher-account-details">Only Available Seller</button>')
    subject, nav = adapter('<body></body>', tree)
    async def still_ambiguous(page, marketplace, **kwargs):
        if kwargs.get("select_marketplace_on_switcher"):
            FixturePage.__init__(nav.page, '<body>Dashboard without identity</body>', marketplace.payments_url)
        elif '/account-switcher/' in nav.page.url or not nav.paths:
            FixturePage.__init__(nav.page, tree, f"https://{marketplace.domain}/account-switcher/default/merchantMarketplace")
        else:
            FixturePage.__init__(nav.page, tree, f"https://{marketplace.domain}/account-switcher/default/merchantMarketplace")
        return nav.page
    monkeypatch.setattr(nav,"_raise_if_auth",still_ambiguous)
    with pytest.raises(SetupRejected) as exc:
        await subject.detect_identity(nav.page, site())
    assert exc.value.diagnostic.reason_code == "IDENTITY_CURRENT_NOT_FOUND"
    assert subject.last_identity_evidence == {}


@pytest.mark.parametrize("reason",["REGISTRATION_REQUIRED", "MARKETPLACE_NOT_OFFERED", "MARKETPLACE_AMBIGUOUS"])
async def test_onboarding_site_selector_refusal_never_becomes_login_wait(reason,monkeypatch):
    from ziniao_automation.workflows.errors import HumanAuthRequired
    subject,nav=adapter("")
    async def blocked(*args,**kwargs):
        nav.page.url=f"https://{site().domain}/account-switcher/default/merchantMarketplace"
        raise HumanAuthRequired("raw",kind="marketplace_switch",reason_code=reason,
                                stage="marketplace_switch",page_category="SITE_SWITCHER")
    monkeypatch.setattr(nav,"_raise_if_auth",blocked)
    with pytest.raises(SetupRejected) as exc:
        await subject._handle_auth(nav.page,site(),select_site=True)
    assert exc.value.diagnostic.reason_code==reason


@pytest.mark.parametrize("reason,stage,expected", [
    ("AUTH_INPUT_NOT_READY", "otp_signin", "AUTH_OTP_NOT_FILLED"),
    ("AUTH_INPUT_NOT_READY", "password_signin", None),
    ("AUTH_CHALLENGE", "otp_signin", None),
    ("AUTH_RETRY_EXHAUSTED", "otp_signin", None),
])
async def test_only_unfilled_otp_ends_setup_site_without_human_lease(reason,stage,expected,monkeypatch):
    from ziniao_automation.workflows.errors import HumanAuthRequired
    subject,nav=adapter("")
    async def blocked(*args,**kwargs):
        nav.page.url=f"https://{site().domain}/ap/mfa"
        raise HumanAuthRequired("sensitive must not be copied",kind="challenge",reason_code=reason,
                                stage=stage,page_category="LOGIN")
    monkeypatch.setattr(nav,"_raise_if_auth",blocked)
    with pytest.raises(SetupRejected if expected else HumanAuthRequired) as exc:
        await subject._handle_auth(nav.page,site(),select_site=True)
    if expected:
        assert exc.value.diagnostic.reason_code==expected
        assert not exc.value.stop_store
        assert 'sensitive' not in str(exc.value)


async def test_dashboard_identity_is_one_atomic_snapshot_not_nth_roundtrips(monkeypatch):
    subject,nav=adapter('<meta name="merchant-id" content="SELLER"><div class="partner-info">Display Name | Canada</div>')
    async def forbidden(*args,**kwargs):
        raise AssertionError("old per-element reader must not be used by onboarding")
    monkeypatch.setattr(nav,"_identity_candidates",forbidden)
    assert await subject.detect_identity(nav.page,site()) == ("SELLER","dom:content")


async def test_hidden_header_cannot_conflict_with_current_visible_header():
    subject,nav=adapter('<div class="partner-info" style="display:none">OLD</div><div class="partner-info">CURRENT | Canada</div>')
    assert await subject.detect_identity(nav.page,site()) == ("CURRENT","dom:visible_account")


def test_onboarding_has_explicit_short_otp_budget_only():
    from ziniao_automation.workflows.amazon_login import AmazonLoginAdvancer
    subject=AmazonStoreSetupPage(policy(otp_autofill_timeout_ms=15000))
    assert subject._navigation._login_advancer._otp_prefill_wait_timeout_ms==15000
    assert AmazonLoginAdvancer()._otp_prefill_wait_timeout_ms==60000
