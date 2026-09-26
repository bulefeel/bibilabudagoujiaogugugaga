from __future__ import annotations

import gc
from decimal import Decimal
import logging
from pathlib import Path
import weakref

import pytest

from ziniao_automation.workflows.amazon_disbursement import AmazonPaymentsPage
from ziniao_automation.workflows.errors import (
    DomContractError,
    HumanAuthRequired,
    PreflightRejected,
    SubmissionNotDispatched,
)
from ziniao_automation.workflows.types import (
    MarketplaceRef,
    MarketplaceSnapshot,
    RunMode,
    StoreRef,
    WorkflowRun,
)

from .fixture_dom import FixturePage


FIXTURES = Path(__file__).parents[1] / "fixtures" / "amazon_payments"


class _CountingButton:
    def __init__(self, on_click=None) -> None:
        self.clicks = 0
        self._on_click = on_click

    async def get_attribute(self, name: str):
        del name
        return None

    async def is_enabled(self) -> bool:
        return True

    async def click(self, **kwargs) -> None:
        del kwargs
        self.clicks += 1
        if self._on_click is not None:
            self._on_click()


class _ResolvedPageLoginAdvancer:
    def __init__(self, target) -> None:
        self.target = target
        self.calls = 0
        self.allow_payment_details_handoffs: list[bool] = []

    async def advance_or_raise(
        self,
        page,
        *,
        expected_host=None,
        allow_payment_details_handoff=False,
    ):
        del page
        assert expected_host == "sellercentral.amazon.ca"
        self.calls += 1
        self.allow_payment_details_handoffs.append(
            bool(allow_payment_details_handoff)
        )
        return self.target


class _CompletingSamePageLoginAdvancer:
    def __init__(self) -> None:
        self.calls = 0
        self.allow_payment_details_handoffs: list[bool] = []

    async def advance_or_raise(
        self,
        page,
        *,
        expected_host=None,
        allow_payment_details_handoff=False,
    ):
        assert expected_host == "sellercentral.amazon.ca"
        assert page.url.endswith("/ap/signin")
        self.calls += 1
        self.allow_payment_details_handoffs.append(
            bool(allow_payment_details_handoff)
        )
        page.set_details()
        return page


class _MutableFixturePage:
    def __init__(self) -> None:
        self.set_dashboard()

    def set_dashboard(self) -> None:
        self._fixture = FixturePage(
            "<html><body><main>Payments dashboard</main></body></html>",
            "https://sellercentral.amazon.ca/payments/dashboard/index.html",
        )
        self.url = self._fixture.url

    def set_details(self, *, amount: str = "2.89") -> None:
        html = (FIXTURES / "details_en_CA.html").read_text(encoding="utf-8")
        if amount != "2.89":
            html = html.replace("CA$2.89", f"CA${amount}")
        self._fixture = FixturePage(
            html,
            "https://sellercentral.amazon.ca/payments/disburse/details?accountType=PAYABLE",
        )
        self.url = self._fixture.url

    def locator(self, selector: str):
        return self._fixture.locator(selector)

    async def wait_for_load_state(self, *args, **kwargs) -> None:
        del args, kwargs


class _DelayedTransitionPage(_MutableFixturePage):
    def __init__(self) -> None:
        super().__init__()
        self.elapsed_ms = 0
        self.transition_at_ms: int | None = None
        self.transition_kind: str | None = None

    def schedule_transition(self, kind: str, *, delay_ms: int = 1_000) -> None:
        assert kind in {"signin", "details"}
        self.transition_kind = kind
        self.transition_at_ms = self.elapsed_ms + delay_ms

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.elapsed_ms += milliseconds
        if (
            self.transition_at_ms is None
            or self.elapsed_ms < self.transition_at_ms
        ):
            return
        kind = self.transition_kind
        self.transition_at_ms = None
        self.transition_kind = None
        if kind == "signin":
            self._fixture = FixturePage(
                '<html><body><form name="signIn">Amazon sign in</form></body></html>',
                "https://sellercentral.amazon.ca/ap/signin",
            )
            self.url = self._fixture.url
        else:
            self.set_details()


class _PostClickWaitFailurePage(_MutableFixturePage):
    def __init__(self) -> None:
        super().__init__()
        self.click_dispatched = False

    async def wait_for_timeout(self, milliseconds: int) -> None:
        del milliseconds
        if self.click_dispatched:
            raise RuntimeError("Target page, context or browser has been closed")


class _RouteOnlyPage:
    def __init__(self, url: str) -> None:
        self.url = url


class _SubmitRouteGuardAdapter(AmazonPaymentsPage):
    def __init__(self) -> None:
        super().__init__(pacing_range_ms=(0, 0))
        self.final_button = _CountingButton()
        self.auth_handoff_flags: list[bool] = []
        self.switcher_flags: list[bool] = []

    async def _raise_if_auth(
        self,
        page,
        marketplace=None,
        *,
        allow_payment_details_handoff=False,
        select_marketplace_on_switcher=True,
    ):
        del marketplace
        self.auth_handoff_flags.append(bool(allow_payment_details_handoff))
        self.switcher_flags.append(bool(select_marketplace_on_switcher))
        return page

    async def _matching_buttons(self, page):
        del page
        return [self.final_button]


class _ProbeAdapter(AmazonPaymentsPage):
    def __init__(
        self,
        snapshot: MarketplaceSnapshot,
        *,
        transition_on_click: bool = True,
        details_amount: str = "2.89",
    ) -> None:
        super().__init__(pacing_range_ms=(0, 0))
        self.snapshot = snapshot
        self.page: _MutableFixturePage | None = None
        self.final_button = _CountingButton()

        def transition() -> None:
            if transition_on_click:
                assert self.page is not None
                self.page.set_details(amount=details_amount)

        self.dashboard_button = _CountingButton(transition)

    async def read_snapshot(self, page, run, marketplace):
        del page, run, marketplace
        return self.snapshot

    async def _unique_payable_row(self, page):
        del page
        return object()

    async def _payout_buttons(self, container):
        if isinstance(container, _MutableFixturePage):
            return [self.final_button]
        return [self.dashboard_button]

    async def _matching_buttons(self, page):
        del page
        return [self.final_button]

    async def _verify_identity(self, page, expected, marketplace):
        del page, marketplace
        return expected, "fixture:merchant-id"

    async def _settle(self, page):
        del page


def _run_and_marketplace(
    *, amount: str = "2.89", can_submit: bool = True
) -> tuple[WorkflowRun, MarketplaceRef, MarketplaceSnapshot]:
    marketplace = MarketplaceRef(
        id="ca",
        code="CA",
        domain="sellercentral.amazon.ca",
        currency="CAD",
    )
    run = WorkflowRun(
        id="account-probe",
        workflow="amazon_disbursement",
        mode=RunMode.DRY_RUN,
        store=StoreRef(
            id="store-1",
            name="fixture store",
            selector_type="oauth",
            selector_value="oauth-value",
            expected_seller_id="SELLER_FIXTURE",
            identity_confirmed=True,
        ),
        marketplaces=(marketplace,),
    )
    snapshot = MarketplaceSnapshot(
        marketplace_code="CA",
        domain=marketplace.domain,
        seller_id=run.store.expected_seller_id,
        payment_account="",
        currency=marketplace.currency,
        payable_amount=Decimal(amount),
        delayed_amount=Decimal("0"),
        settlement_key="OPEN:fixture",
        can_submit=can_submit,
        contract_version="fixture",
        page_fingerprint="fixture",
        skip_reason=None if can_submit else "请求付款窗口未到或按钮禁用",
        identity_source="fixture:merchant-id",
    )
    return run, marketplace, snapshot

















@pytest.mark.asyncio
async def test_open_confirmation_finishes_after_auth_adopts_exact_details_tab() -> None:
    """A successful automatic login must not request another manual Continue."""

    run, marketplace, snapshot = _run_and_marketplace()
    source = FixturePage(
        '<html><body><form name="signIn">Amazon sign in</form></body></html>',
        "https://sellercentral.amazon.ca/ap/signin",
    )
    target = _MutableFixturePage()
    target.set_details()
    login_advancer = _ResolvedPageLoginAdvancer(target)
    adapter = _ProbeAdapter(snapshot, transition_on_click=False)
    adapter._login_advancer = login_advancer
    old_key = (id(source), "CA")
    new_key = (id(target), "CA")
    adapter._confirmation_dispatched.add(old_key)
    verified_pages: list[object] = []

    async def verify_details(page_arg, marketplace_arg, expected_arg):
        assert marketplace_arg is marketplace
        assert expected_arg is snapshot
        verified_pages.append(page_arg)
        return expected_arg

    adapter._verify_details = verify_details

    result = await adapter.open_confirmation(source, run, marketplace, snapshot)

    assert result is snapshot
    assert verified_pages == [target]
    assert adapter.resolved_page_for(source) is target
    assert login_advancer.allow_payment_details_handoffs == [True]
    assert old_key not in adapter._confirmation_dispatched
    assert new_key not in adapter._confirmation_dispatched
    assert adapter.dashboard_button.clicks == 0
    assert adapter.final_button.clicks == 0


@pytest.mark.asyncio
async def test_open_confirmation_keeps_marker_until_details_verification_succeeds() -> None:
    run, marketplace, snapshot = _run_and_marketplace()
    page = _MutableFixturePage()
    page.set_details()
    adapter = _ProbeAdapter(snapshot, transition_on_click=False)
    key = (id(page), "CA")
    adapter._confirmation_dispatched.add(key)

    async def reject_details(page_arg, marketplace_arg, expected_arg):
        del page_arg, marketplace_arg, expected_arg
        raise DomContractError("fixture details verification failed")

    adapter._verify_details = reject_details

    with pytest.raises(DomContractError, match="details verification failed"):
        await adapter.open_confirmation(page, run, marketplace, snapshot)

    assert key in adapter._confirmation_dispatched
    assert adapter.dashboard_button.clicks == 0
    assert adapter.final_button.clicks == 0








@pytest.mark.parametrize(
    "url",
    [
        "https://sellercentral.amazon.ca/payments/disburse/details-extra",
        "https://sellercentral.amazon.ca/prefix/payments/disburse/details",
        "https://sellercentral.amazon.ca/payments/disburse/details/",
        "https://sellercentral.amazon.ca/payments/disburse/details;unexpected",
        "https://sellercentral.amazon.co.uk/payments/disburse/details",
    ],
)
def test_expected_details_route_rejects_prefix_suffix_and_wrong_host(url: str) -> None:
    _, marketplace, _ = _run_and_marketplace()

    assert not AmazonPaymentsPage._is_expected_details_url(url, marketplace)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://sellercentral.amazon.ca/payments/disburse/details-extra",
        "https://sellercentral.amazon.ca/prefix/payments/disburse/details",
        "https://sellercentral.amazon.ca/payments/disburse/details/",
        "https://sellercentral.amazon.ca/payments/disburse/details;unexpected",
        "https://sellercentral.amazon.co.uk/payments/disburse/details",
    ],
)
async def test_submit_once_rejects_nonexact_details_route_without_click(
    url: str,
) -> None:
    run, marketplace, snapshot = _run_and_marketplace()
    adapter = _SubmitRouteGuardAdapter()
    page = _RouteOnlyPage(url)

    with pytest.raises(DomContractError, match="最终提交只允许"):
        await adapter.submit_once(page, run, marketplace, snapshot)

    assert adapter.auth_handoff_flags == [True]
    # ARMED 之后、唯一不可逆点击之前：不在这个窗口里多做一次选站导航。
    assert adapter.switcher_flags == [False]
    assert adapter.final_button.clicks == 0




def test_verified_identity_cache_follows_page_lifetime() -> None:
    adapter = AmazonPaymentsPage(pacing_range_ms=(0, 0))
    page = FixturePage(
        "<html><body>Payments dashboard</body></html>",
        "https://sellercentral.amazon.ca/payments/dashboard/index.html",
    )
    adapter._page_verified_identities(page)["sellercentral.amazon.ca"] = "seller"
    reference = weakref.ref(page)

    assert len(adapter._verified_identities) == 1
    del page
    gc.collect()

    assert reference() is None
    assert len(adapter._verified_identities) == 0


def test_verified_identity_cache_uses_object_attribute_for_nonweak_fixture() -> None:
    class NonWeakPage:
        __slots__ = ("_ziniao_verified_seller_identities",)

    adapter = AmazonPaymentsPage(pacing_range_ms=(0, 0))
    page = NonWeakPage()
    first = adapter._page_verified_identities(page)
    first["sellercentral.amazon.ca"] = "seller"

    assert adapter._page_verified_identities(page) is first
    assert adapter._page_verified_identities(page)["sellercentral.amazon.ca"] == "seller"
    assert len(adapter._verified_identities) == 0

    adapter._clear_page_verified_identities(page)
    assert not first










_DETAILS_URL = (
    "https://sellercentral.amazon.ca/payments/disburse/details?accountType=PAYABLE"
)


class _HydratingDetailsPage(_MutableFixturePage):
    """Commit the details route before painting the details document.

    This is the shape the production stall actually had: Playwright's
    ``Page.url`` flips to ``/payments/disburse/details`` while the previous
    dashboard document is still mounted, so every URL-shaped wait returns
    ``elapsed_ms=0`` and the reader races Amazon's paint.  The existing
    fixtures cannot express it — ``FixturePage.url`` is fixed at construction
    and ``_DelayedTransitionPage`` only mutates while the URL is still wrong.
    """

    def __init__(self, *, ready_after_ms: int = 1_000) -> None:
        super().__init__()
        self.elapsed_ms = 0
        self._ready_after_ms = ready_after_ms
        self._hydrated = False
        self.url = _DETAILS_URL

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.elapsed_ms += milliseconds
        if not self._hydrated and self.elapsed_ms >= self._ready_after_ms:
            self._hydrated = True
            self.set_details()


class _DetailsPageWithClock(_MutableFixturePage):
    """Details document is already painted; only its ``kat-`` buttons lag."""

    def __init__(self) -> None:
        super().__init__()
        self.elapsed_ms = 0
        self.set_details()

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.elapsed_ms += milliseconds


class _StaleButtonsAdapter(_ProbeAdapter):
    """Scan the stale dashboard's three payout buttons until it is replaced."""

    def __init__(self, snapshot: MarketplaceSnapshot) -> None:
        super().__init__(snapshot, transition_on_click=False)
        self.stale_buttons = [_CountingButton(), _CountingButton()]

    async def _matching_buttons(self, page):
        del page
        assert self.page is not None
        if self.page.elapsed_ms < 1_000:
            return [self.final_button, *self.stale_buttons]
        return [self.final_button]








class _RecordingHandler(logging.Handler):
    """Capture one logger's records without touching global logging state.

    ``configure_logging`` clears the root handlers, which removes pytest's
    ``caplog`` handler for every test that runs after it.  Attaching directly
    to the module logger keeps this assertion order-independent.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())








class _SharedTabPage:
    """One Ziniao tab reused for every marketplace, exactly like a real run.

    The engine opens a single financial session per run and hands the same
    ``handle.page`` to every site, navigating it between marketplaces.
    """

    def __init__(self) -> None:
        self.url = ""

    def goto_details(self, marketplace: MarketplaceRef) -> None:
        self.url = f"https://{marketplace.domain}/payments/disburse/details"

    async def wait_for_load_state(self, *args, **kwargs) -> None:
        del args, kwargs
        return None


class _TwoSiteSubmitAdapter(AmazonPaymentsPage):
    """Runs the REAL ``submit_once``; only browser-facing seams are stubbed.

    Every one-shot marker, route check, host check and account-proof check is
    the production code path.
    """

    def __init__(self) -> None:
        super().__init__(pacing_range_ms=(0, 0))
        # The click is counted before the failure is raised, mirroring a browser
        # that dispatched the press and only then lost the page.
        self.click_error: Exception | None = None
        self.final_button = _CountingButton(self._fail_after_pressing)

    def _fail_after_pressing(self) -> None:
        if self.click_error is not None:
            raise self.click_error

    async def _raise_if_auth(
        self, page, marketplace=None, *, allow_payment_details_handoff=False,
        select_marketplace_on_switcher=True,
    ):
        del marketplace, allow_payment_details_handoff
        return page

    async def _verify_identity(self, page, expected, marketplace):
        del page, marketplace
        return expected, "fixture:merchant-id"

    async def _verify_details(self, page, marketplace, expected):
        del page, marketplace
        return expected

    async def _matching_buttons(self, page):
        del page
        return [self.final_button]

    async def _body_text(self, page) -> str:
        del page
        return "no banner"

    async def _settle(self, page) -> None:
        del page




def _payable_site(
    code: str, domain: str, currency: str, tail: str
) -> tuple[MarketplaceRef, MarketplaceSnapshot]:
    """``tail`` is what the confirmation page will report, not a baseline."""

    marketplace = MarketplaceRef(
        id=code.lower(), code=code, domain=domain, currency=currency
    )
    snapshot = MarketplaceSnapshot(
        marketplace_code=code,
        domain=domain,
        seller_id="SELLER_FIXTURE",
        payment_account=tail,
        currency=currency,
        payable_amount=Decimal("100.00"),
        delayed_amount=Decimal("0"),
        settlement_key=f"OPEN:{code}",
        can_submit=True,
        contract_version="fixture",
        page_fingerprint="fixture",
        identity_source="fixture:merchant-id",
    )
    return marketplace, snapshot


def _multi_site_run(*marketplaces: MarketplaceRef) -> WorkflowRun:
    return WorkflowRun(
        id="two-site-submit",
        workflow="amazon_disbursement",
        mode=RunMode.AUTO,
        store=StoreRef(
            id="store-1",
            name="fixture store",
            selector_type="oauth",
            selector_value="oauth-value",
            expected_seller_id="SELLER_FIXTURE",
            identity_confirmed=True,
        ),
        marketplaces=marketplaces,
    )


@pytest.mark.asyncio
async def test_real_submit_once_pays_a_second_marketplace_on_the_same_tab() -> None:
    """The production failure of 2026-08-18, against the real method.

    One run drives every site through one Playwright Page.  The one-shot marker
    used to be keyed on the page alone, so AU's payout silently vetoed UK's: the
    second site armed its money guard and then could never click, stranding
    GBP 610.03 and forcing a human review.  A run could only ever pay one site.

    Every existing multi-site test passes a fake ``submit_once`` (see
    ``CursorAdapter`` in test_auth_cursor.py), so none of them could see this.
    """

    au, au_snapshot = _payable_site("AU", "sellercentral.amazon.com.au", "AUD", "465")
    uk, uk_snapshot = _payable_site("UK", "sellercentral.amazon.co.uk", "GBP", "402")
    run = _multi_site_run(au, uk)
    adapter = _TwoSiteSubmitAdapter()
    page = _SharedTabPage()

    page.goto_details(au)
    first = await adapter.submit_once(page, run, au, au_snapshot)
    assert first.observed_status == "click_dispatched"
    assert adapter.final_button.clicks == 1

    # Same tab, next marketplace.  This is the click that used to be refused.
    page.goto_details(uk)
    second = await adapter.submit_once(page, run, uk, uk_snapshot)
    assert second.observed_status == "click_dispatched"
    assert adapter.final_button.clicks == 2

    assert adapter._page_consumed_marketplaces(page) == {"AU", "UK"}


@pytest.mark.asyncio
async def test_real_submit_once_still_refuses_a_second_click_for_the_same_site() -> None:
    """Per-site keying must not weaken the one-click-per-site guarantee."""

    au, snapshot = _payable_site("AU", "sellercentral.amazon.com.au", "AUD", "465")
    run = _multi_site_run(au)
    adapter = _TwoSiteSubmitAdapter()
    page = _SharedTabPage()
    page.goto_details(au)

    await adapter.submit_once(page, run, au, snapshot)
    assert adapter.final_button.clicks == 1

    with pytest.raises(DomContractError) as raised:
        await adapter.submit_once(page, run, au, snapshot)
    assert adapter.final_button.clicks == 1
    # Never releasable: the marker proves this site already clicked here.
    assert not isinstance(raised.value, SubmissionNotDispatched)


@pytest.mark.asyncio
async def test_failure_before_the_click_is_reported_as_not_dispatched() -> None:
    """A pre-click refusal is what lets the caller release its ARMED guard."""

    uk, snapshot = _payable_site("UK", "sellercentral.amazon.co.uk", "GBP", "402")
    run = _multi_site_run(uk)
    adapter = _TwoSiteSubmitAdapter()
    page = _SharedTabPage()
    page.url = "https://sellercentral.amazon.co.uk/payments/dashboard/index.html"

    with pytest.raises(SubmissionNotDispatched):
        await adapter.submit_once(page, run, uk, snapshot)
    assert adapter.final_button.clicks == 0
    assert not adapter._page_consumed_marketplaces(page)


@pytest.mark.asyncio
async def test_failure_after_the_click_is_never_reported_as_not_dispatched() -> None:
    """The safety floor under the release path.

    ``SubmissionNotDispatched`` authorises deleting a money guard, so it must be
    impossible to raise once the button has actually been pressed.  Here the
    press itself throws — a browser that dispatched the click and only then lost
    the page — which is the exact case where a payout is in flight but nothing
    can be observed.  The proof is the marker written on the line ABOVE
    ``click()``; this pins that adjacency, because moving it below would report
    a real payout as never sent, and then send it again.
    """

    uk, snapshot = _payable_site("UK", "sellercentral.amazon.co.uk", "GBP", "402")
    run = _multi_site_run(uk)
    adapter = _TwoSiteSubmitAdapter()
    adapter.click_error = RuntimeError("Target page, context or browser has been closed")
    page = _SharedTabPage()
    page.goto_details(uk)

    with pytest.raises(RuntimeError) as raised:
        await adapter.submit_once(page, run, uk, snapshot)
    assert not isinstance(raised.value, SubmissionNotDispatched)
    assert adapter.final_button.clicks == 1
    assert "UK" in adapter._page_consumed_marketplaces(page)


class _StepUpAdapter(AmazonPaymentsPage):
    """在最终点击**之前**被要求重新认证的真适配器(只有登录判据被替掉)。"""

    def __init__(self, *, after_click: bool = False) -> None:
        super().__init__(pacing_range_ms=(0, 0))
        self.after_click = after_click
        self.final_button = _CountingButton(self._maybe_raise_after_click)

    def _maybe_raise_after_click(self) -> None:
        if self.after_click:
            raise HumanAuthRequired("点完之后才被要求认证", kind="challenge")

    async def _raise_if_auth(
        self, page, marketplace=None, *, allow_payment_details_handoff=False,
        select_marketplace_on_switcher=True,
    ):
        del marketplace, allow_payment_details_handoff, select_marketplace_on_switcher
        if not self.after_click:
            raise HumanAuthRequired("付款区要求重新认证", kind="challenge")
        return page

    async def _verify_identity(self, page, expected, marketplace):
        del page, marketplace
        return expected, "fixture:merchant-id"

    async def _verify_details(self, page, marketplace, expected):
        del page, marketplace
        return expected

    async def _matching_buttons(self, page):
        del page
        return [self.final_button]

    async def _body_text(self, page) -> str:
        del page
        return "no banner"

    async def _settle(self, page) -> None:
        del page


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after_click", "expected"),
    [(False, False), (True, True)],
)
async def test_submit_once_reports_whether_the_click_had_already_happened(
    after_click, expected
) -> None:
    """``HumanAuthRequired.dispatch_recorded`` 是「点没点」唯一有凭据的答案。

    判据来自写在 ``click()`` 上一行的 consumed 标记。⚠️ 不能拿「``submitted_at``
    还空着」代替 —— 那一列是点击**之后**才写的,点完抛异常时它同样是空的。
    """

    run, marketplace, snapshot = _run_and_marketplace()
    adapter = _StepUpAdapter(after_click=after_click)
    page = _RouteOnlyPage("https://sellercentral.amazon.ca/payments/disburse/details")

    with pytest.raises(HumanAuthRequired) as caught:
        await adapter.submit_once(page, run, marketplace, snapshot)

    assert caught.value.dispatch_recorded is expected
    assert adapter.final_button.clicks == (1 if after_click else 0)


@pytest.mark.asyncio
async def test_a_step_up_before_the_click_releases_the_lock_and_keeps_going() -> None:
    """点击前被要求认证 = 这一下没发生。撤销锁定,自动模式跳过该站点继续跑。

    不撤销的话:引擎按「有说不清的记录」只收口不续跑,人验证完点继续,整条运行落
    UNCERTAIN_FINANCIAL、后面的站点一个没试,而那笔钱一分没动(2026-09-26 自审)。
    """

    from ziniao_automation.workflows.amazon_disbursement import (
        AmazonDisbursementWorkflow,
    )
    from dataclasses import replace

    from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
    from ziniao_automation.workflows.types import SiteStatus, SubmissionReceipt, utc_now

    from .test_auth_cursor import CursorAdapter, make_multi_site_run

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()

    async def open_without_auth(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        return replace(expected, payment_account="493")

    async def uk_step_up(page, run_, marketplace, expected):
        adapter.submit_calls.append(marketplace.code)
        if marketplace.code == "UK":
            raise HumanAuthRequired(
                "付款区要求重新认证", kind="challenge", dispatch_recorded=False
            )
        return SubmissionReceipt(utc_now(), receipt_id=f"click-{marketplace.code}")

    adapter.open_confirmation = open_without_auth
    adapter.submit_once = uk_step_up
    workflow = AmazonDisbursementWorkflow(repository=repo, page_adapter=adapter)
    plan = await workflow.plan(run, object())

    await workflow.execute(run, object(), plan)

    assert adapter.submit_calls == ["CA", "UK", "AU"], "UK 之后的站点照样要做"
    codes = {op.intent.marketplace_code for op in await repo.list_operations(run.id)}
    assert codes == {"CA", "AU"}, "UK 的锁定必须被撤销"
    assert await repo.get_site_status(run.id, "UK") is SiteStatus.NEEDS_HUMAN_AUTH
    released = [e for e in repo.events if e["event_type"] == "operation_released"]
    assert len(released) == 1 and released[0]["details"]["kind"] == "challenge"


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_recorded", [True, None])
async def test_a_step_up_that_may_have_clicked_never_releases(dispatch_recorded) -> None:
    """⚠️ 点过了、或**不知道点没点**,都不许撤销锁定 —— 撤了就等于删掉当天的防重屏障,
    下一次运行会再付一笔。不知道就当点过了(fail closed)。"""

    from ziniao_automation.workflows.amazon_disbursement import (
        AmazonDisbursementWorkflow,
    )
    from dataclasses import replace

    from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
    from ziniao_automation.workflows.types import GuardState

    from .test_auth_cursor import CursorAdapter, make_multi_site_run

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()

    async def open_without_auth(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        return replace(expected, payment_account="493")

    async def ca_step_up(page, run_, marketplace, expected):
        adapter.submit_calls.append(marketplace.code)
        raise HumanAuthRequired(
            "认证", kind="challenge", dispatch_recorded=dispatch_recorded
        )

    adapter.open_confirmation = open_without_auth
    adapter.submit_once = ca_step_up
    workflow = AmazonDisbursementWorkflow(repository=repo, page_adapter=adapter)
    plan = await workflow.plan(run, object())

    with pytest.raises(HumanAuthRequired):
        await workflow.execute(run, object(), plan)

    guards = await repo.list_operations(run.id)
    assert [g.state for g in guards] == [GuardState.ARMED], "锁定必须原样留着"
    assert [e for e in repo.events if e["event_type"] == "operation_released"] == []
