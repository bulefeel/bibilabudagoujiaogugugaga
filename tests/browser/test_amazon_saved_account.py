"""Offline DOM regression: no CDP, no navigation and no real account profile.

Set ZINIAO_LOGIN_FIXTURE_BROWSER to a local Chromium executable. One headless
temporary browser renders sanitized HTML via set_content on about:blank.
The tested URL is an in-memory input to the production JS contract, not a URL
that the browser visits. All network requests are blocked as a second guard.
"""
from pathlib import Path
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from ziniao_automation.workflows.amazon_login import AmazonLoginAdvancer
from ziniao_automation.workflows.amazon_saved_account import SAVED_ACCOUNT_ACTION
from ziniao_automation.workflows.errors import HumanAuthRequired

BROWSER = os.getenv("ZINIAO_LOGIN_FIXTURE_BROWSER", "")
pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(not BROWSER, reason="Requires a local offline fixture renderer"),
]
HOST = "sellercentral.amazon.de"
ORIGIN = "https://" + HOST
HTML = (Path(__file__).parents[1] / "fixtures/amazon_login/saved_account_chooser.html").read_text(encoding="utf-8")


class OfflinePage:
    """Real DOM, with location supplied as data; no goto/connect API exposed."""

    def __init__(self, raw):
        self.raw = raw
        self.url = ORIGIN + "/ap/signin"
        self.context = SimpleNamespace(pages=[self])

    def locator(self, selector):
        return self.raw.locator(selector)

    def is_closed(self):
        return self.raw.is_closed()

    async def evaluate(self, expression, arg=None):
        if expression == SAVED_ACCOUNT_ACTION:
            return await self.raw.evaluate(
                "(payload) => { const location = new URL(payload.url); return (" +
                SAVED_ACCOUNT_ACTION + ")(payload.contract); }",
                {"url": self.url, "contract": arg},
            )
        return await self.raw.evaluate(expression, arg)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def offline_renderer():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=BROWSER, headless=True,
            args=["--disable-background-networking", "--no-first-run"],
        )
        context = await browser.new_context(offline=True, service_workers="block")
        await context.route("**/*", lambda route: route.abort())
        raw = await context.new_page()
        try:
            yield raw
        finally:
            await browser.close()  # This test's temporary browser only.


@pytest_asyncio.fixture(loop_scope="module")
async def page(offline_renderer):
    assert offline_renderer.url == "about:blank"
    await offline_renderer.set_content(HTML)
    yield OfflinePage(offline_renderer)


def advancer():
    return AmazonLoginAdvancer(pacing_range_ms=(0, 0), managed_passkey_wait_timeout_ms=0)


async def clicks(page):
    return await page.evaluate("window.fixtureClicks")


@pytest.mark.parametrize("heading", ["Konten wechseln", "Switch accounts", "选择账户"])
@pytest.mark.parametrize("count", [1, 3])
async def test_first_saved_account_by_dom_order_not_language(page, heading, count):
    await page.evaluate("""({heading,count}) => {
        document.querySelector('h1').textContent = heading;
        const first = document.querySelector('[data-test-id="switchableAccounts"]');
        for (let i=2; i<=count; i++) {
            const next = first.cloneNode(true);
            next.querySelector('a').dataset.value = 'fixture-account-' + i;
            first.parentElement.insertBefore(next, document.querySelector('#cvf-account-switcher-add-accounts-link').parentElement);
        }
    }""", {"heading": heading, "count": count})
    helper = advancer()
    assert await helper._classify(page, expected_host=HOST) == "saved_account_choice"
    outcome, _ = await helper._dispatch_action_once(page, "saved_account_choice", expected_host=HOST)
    assert outcome == "dispatched"
    assert await clicks(page) == ["fixture-account-1"]


@pytest.mark.parametrize("mutation", [
    "document.querySelector('[data-test-id]').remove()",
    "document.querySelector('.cvf-widget-btn-verify-account-switcher').setAttribute('aria-disabled','true')",
    "document.querySelector('.cvf-widget-btn-verify-account-switcher').removeAttribute('data-value')",
    "document.querySelector('.cvf-widget-btn-verify-account-switcher').dataset.name='sign_out_request'",
    "document.querySelector('.cvf-widget-btn-verify-account-switcher').id='cvf-account-switcher-add-accounts-link'",
    "document.querySelector('.cvf-widget-btn-verify-account-switcher').classList.add('cvf-account-switcher-sign-out-link')",
    "document.querySelector('[data-test-id] .cvf-widget-form').setAttribute('method','get')",
    "document.querySelector('[data-test-id] .cvf-widget-form').setAttribute('action','https://example.invalid/ap/switchaccount')",
    "document.querySelector('[data-test-id] .cvf-widget-form').setAttribute('action','/ap/switchaccount/extra')",
    "document.querySelector('.cvf-widget-btn-verify-account-switcher').href='https://example.invalid/ap/signin'",
    "document.querySelector('.cvf-widget-btn-verify-account-switcher').href='/ap/signin/'",
    "const a=document.querySelector('.cvf-widget-btn-verify-account-switcher');a.after(a.cloneNode(true))",
    "const r=document.querySelector('.cvf-account-switcher');r.after(r.cloneNode(true))",
    "document.body.insertAdjacentHTML('beforeend','<input type=password>')",
    "document.body.insertAdjacentHTML('beforeend','<input id=ap_email>')",
    "document.body.insertAdjacentHTML('beforeend','<input id=auth-mfa-otpcode>')",
    "document.body.insertAdjacentHTML('beforeend','<div id=auth-captcha-image>captcha</div>')",
    "document.body.insertAdjacentHTML('beforeend','<div id=auth-error-message-box>error</div>')",
    "document.body.insertAdjacentHTML('beforeend','<div data-testid=select-account>other chooser</div>')",
])
async def test_changed_or_ambiguous_contract_never_clicks(page, mutation):
    await page.evaluate("() => {" + mutation + "}")
    with pytest.raises(HumanAuthRequired):
        await advancer().advance(page, expected_host=HOST)
    assert await clicks(page) == []


async def test_hidden_duplicate_and_add_signout_are_not_accounts(page):
    await page.evaluate("""() => {
        const card = document.querySelector('[data-test-id]');
        const hidden = card.cloneNode(true);
        hidden.style.display = 'none';
        card.before(hidden);
        const duplicate = card.querySelector('a').cloneNode(true);
        duplicate.style.display = 'none';
        card.querySelector('a').after(duplicate);
    }""")
    outcome, _ = await advancer()._dispatch_action_once(page, "saved_account_choice", expected_host=HOST)
    assert outcome == "dispatched"
    assert await clicks(page) == ["fixture-account-1"]


async def test_disabled_first_account_does_not_fall_through_to_second(page):
    await page.evaluate("""() => {
        const card = document.querySelector('[data-test-id]');
        card.after(card.cloneNode(true));
        card.querySelector('a').setAttribute('aria-disabled', 'true');
    }""")
    with pytest.raises(HumanAuthRequired):
        await advancer().advance(page, expected_host=HOST)
    assert await clicks(page) == []


async def test_reordered_list_uses_current_first_account(page):
    helper = advancer()
    assert await helper._classify(page, expected_host=HOST) == "saved_account_choice"
    await page.evaluate("""() => {
        const card = document.querySelector('[data-test-id]');
        const second = card.cloneNode(true);
        second.querySelector('a').dataset.value = 'fixture-account-2';
        card.before(second);
    }""")
    await helper._dispatch_action_once(page, "saved_account_choice", expected_host=HOST)
    assert await clicks(page) == ["fixture-account-2"]


@pytest.mark.parametrize("url", [
    "https://sellercentral.amazon.ca/ap/signin",
    ORIGIN + "/ap/signin/",
    ORIGIN + "/ap/signin;param",
    ORIGIN + "/account-switcher",
    ORIGIN + "/ap/mfa",
])
async def test_saved_account_contract_rejects_other_routes(page, url):
    page.url = url  # Test input only; never navigate.
    state = await advancer()._saved_account_state(page, click=True, expected_host=HOST)
    assert state["status"] == "untrusted_route"
    assert await clicks(page) == []

async def test_saved_account_contract_requires_exact_expected_host(page):
    state = await advancer()._saved_account_state(
        page, click=True, expected_host="sellercentral.amazon.de.example"
    )
    assert state["status"] == "untrusted_route"
    assert await clicks(page) == []

@pytest.mark.parametrize("markup", [
    '<div class="a-row a-size-base cvf-account-switcher-spacing-top-micro"><div class="a-column a-text-center"><a href="/gp/help/customer/display.html/">Erfahre mehr über den Kontowechsel.</a></div></div>',
    '<div class="account-selection-spacing"><p>Layout only</p></div>',
])
async def test_layout_outside_saved_list_is_not_a_second_auth_challenge(page, markup):
    await page.evaluate("(html) => document.body.insertAdjacentHTML('beforeend', html)", markup)
    helper = advancer()
    assert await helper._classify(page, expected_host=HOST) == "saved_account_choice"
    await helper._dispatch_action_once(page, "saved_account_choice", expected_host=HOST)
    assert await clicks(page) == ["fixture-account-1"]


@pytest.mark.parametrize("markup", [
    '<div class="unknown-account-selection"><button>Other account</button></div>',
    '<div class="unknown-account-selection"><a href="/ap/signin">Other account</a></div>',
    '<div class="unknown-account-selection" role="listbox">Other account</div>',
    '<div class="cvf-account-switcher-spacing-top-micro"><input name="newAccount"></div>',
])
async def test_real_extra_selector_is_blocked_even_when_it_uses_layout_classes(page, markup):
    await page.evaluate("(html) => document.body.insertAdjacentHTML('beforeend', html)", markup)
    with pytest.raises(HumanAuthRequired):
        await advancer().advance(page, expected_host=HOST)
    assert await clicks(page) == []
