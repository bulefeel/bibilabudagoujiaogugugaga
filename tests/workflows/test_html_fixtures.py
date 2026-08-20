from __future__ import annotations

from datetime import date
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from ziniao_automation.workflows.amazon_disbursement import AmazonPaymentsPage
from ziniao_automation.workflows.errors import (
    DomContractError,
    PayoutRateLimited,
    PreflightRejected,
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


def fixture_page(name: str, url: str) -> FixturePage:
    return FixturePage((FIXTURES / name).read_text(encoding="utf-8"), url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "domain", "currency", "expected", "deferred"),
    [
        ("dashboard_zh_CA.html", "sellercentral.amazon.ca", "CAD", "18.86", "0.00"),
        ("dashboard_en_AU.html", "sellercentral.amazon.com.au", "AUD", "25.10", "0.00"),
    ],
)
async def test_saved_dashboard_fixtures_parse_kat_rows_and_labels(
    fixture: str, domain: str, currency: str, expected: str, deferred: str
) -> None:
    adapter = AmazonPaymentsPage()
    page = fixture_page(fixture, f"https://{domain}/payments/dashboard/index.html")
    rows = await adapter._classified_rows(page)
    kinds = [kind for kind, _ in rows]
    assert kinds == ["PAYABLE", "DEFERRED", "ALL"]
    payable = rows[0][1]
    delayed = rows[1][1]
    assert await adapter._available_amount(payable, currency) == Decimal(expected)
    assert await adapter._available_amount(delayed, currency) == Decimal(deferred)
    # The delayed total is present only in kat-link[label], not innerText.
    assert await adapter._total_amount(delayed, currency) > 0
    buttons = await adapter._payout_buttons(payable)
    assert len(buttons) == 1


@pytest.mark.asyncio
async def test_live_columnar_dashboard_aligns_three_cards_by_strict_index() -> None:
    adapter = AmazonPaymentsPage()
    page = fixture_page(
        "dashboard_columnar_zh_CA.html",
        "https://sellercentral.amazon.ca/payments/dashboard/index.html",
    )

    rows = await adapter._classified_rows(page)
    assert [kind for kind, _ in rows] == ["PAYABLE", "DEFERRED", "ALL"]
    payable, deferred, all_accounts = (row for _, row in rows)

    assert await adapter._total_amount(payable, "CAD") == Decimal("158.05")
    assert await adapter._available_amount(payable, "CAD") == Decimal("158.05")
    assert await adapter._total_amount(deferred, "CAD") == Decimal("2410.10")
    assert await adapter._available_amount(deferred, "CAD") == Decimal("0.00")
    assert await adapter._total_amount(all_accounts, "CAD") == Decimal("2568.15")
    assert await adapter._available_amount(all_accounts, "CAD") == Decimal("158.05")
    assert len(await adapter._payout_buttons(payable)) == 1
    assert len(await adapter._payout_buttons(all_accounts)) == 0


@pytest.mark.asyncio
async def test_negative_standard_order_total_keeps_sign_and_passes_balance_check() -> None:
    """Amazon renders CA debt as ``-CA$...`` (minus before currency code)."""

    adapter = AmazonPaymentsPage()
    page = fixture_page(
        "dashboard_columnar_negative_zh_CA.html",
        "https://sellercentral.amazon.ca/payments/dashboard/index.html",
    )

    async def no_navigation(*args, **kwargs) -> None:
        return None

    page.goto = no_navigation  # type: ignore[attr-defined]
    marketplace = MarketplaceRef(
        id="ca-negative",
        code="CA",
        domain="sellercentral.amazon.ca",
        currency="CAD",
    )
    run = WorkflowRun(
        id="negative-balance",
        workflow="amazon_disbursement",
        mode=RunMode.DRY_RUN,
        store=StoreRef(
            id="store",
            name="fixture",
            selector_type="oauth",
            selector_value="oauth-fixture",
            expected_seller_id="SELLER_NEGATIVE_CA_FIXTURE",
            identity_confirmed=True,
        ),
        marketplaces=(marketplace,),
    )

    snapshot = await adapter.read_snapshot(
        page, run, marketplace, )

    rows = await adapter._classified_rows(page)
    payable, deferred, all_accounts = (row for _, row in rows)
    assert await adapter._total_amount(payable, "CAD") == Decimal("-197.63")
    assert await adapter._total_amount(deferred, "CAD") == Decimal("2295.78")
    assert await adapter._total_amount(all_accounts, "CAD") == Decimal("2098.15")
    assert snapshot.payable_amount == Decimal("0.00")
    assert snapshot.can_submit is False


@pytest.mark.asyncio
async def test_identity_detection_reads_machine_id_from_saved_page() -> None:
    adapter = AmazonPaymentsPage()
    page = fixture_page(
        "dashboard_zh_CA.html",
        "https://sellercentral.amazon.ca/payments/dashboard/index.html",
    )
    seller_id, source = await adapter._detect_identity(page)
    assert seller_id == "SELLER_FIXTURE"
    assert source == "dom:content"


@pytest.mark.asyncio
async def test_identity_detection_ignores_marketplace_query_id() -> None:
    adapter = AmazonPaymentsPage()
    page = FixturePage(
        '<html><head><meta name="merchant-id" content="MERCHANT-123"></head></html>',
        "https://sellercentral.amazon.ca/payments/dashboard/index.html?"
        "mons_sel_dir_mcid=MERCHANT-123&mons_sel_mkid=A2EUQ1WTGCTBG2",
    )
    seller_id, _ = await adapter._detect_identity(page)
    assert seller_id == "MERCHANT-123"


@pytest.mark.asyncio
async def test_account_switcher_fallback_reads_only_current_account_button() -> None:
    adapter = AmazonPaymentsPage()
    page = FixturePage(
        """
        <html><body>
          <button>zhengxiang-us (当前)</button>
          <button class="full-page-account-switcher-account-selected">加拿大 (当前)</button>
          <button>英国</button><button>澳大利亚</button>
          <kat-button label="选择账户"></kat-button>
        </body></html>
        """,
        "https://sellercentral.amazon.ca/account-switcher/default/merchantMarketplace",
    )
    async def no_navigation(*args, **kwargs):
        return None

    page.goto = no_navigation  # type: ignore[attr-defined]
    # The lightweight fixture has no wait_for_function; the production method
    # deliberately treats that wait as best-effort and still reads the DOM.
    marketplace = MarketplaceRef(
        id="ca",
        code="CA",
        domain="sellercentral.amazon.ca",
        currency="CAD",
    )
    seller_id, source = await adapter._detect_identity_from_account_switcher(
        page, marketplace
    )
    assert seller_id == "zhengxiang-us"
    assert source == "account_switcher:current"


@pytest.mark.asyncio
async def test_saved_no_data_fixture_is_normal_skip() -> None:
    adapter = AmazonPaymentsPage()
    page = fixture_page(
        "dashboard_no_data_UK.html",
        "https://sellercentral.amazon.co.uk/payments/dashboard/index.html",
    )
    message = await adapter._no_data_message(page)
    assert message == "无付款数据 您在此商城中没有任何可用的付款数据。"


@pytest.mark.asyncio
async def test_saved_no_data_fixture_becomes_terminal_zero_balance_snapshot() -> None:
    """An empty marketplace is a normal result, not a hanging probe."""

    adapter = AmazonPaymentsPage()
    page = fixture_page(
        "dashboard_no_data_UK.html",
        "https://sellercentral.amazon.co.uk/payments/dashboard/index.html",
    )

    async def no_navigation(*args, **kwargs) -> None:
        return None

    page.goto = no_navigation  # type: ignore[attr-defined]
    marketplace = MarketplaceRef(
        id="uk",
        code="UK",
        domain="sellercentral.amazon.co.uk",
        currency="GBP",
    )
    run = WorkflowRun(
        id="no-data",
        workflow="amazon_disbursement",
        mode=RunMode.DRY_RUN,
        store=StoreRef(
            id="store",
            name="fixture",
            selector_type="oauth",
            selector_value="oauth-fixture",
            expected_seller_id="SELLER_FIXTURE",
            identity_confirmed=True,
        ),
        marketplaces=(marketplace,),
    )

    snapshot = await adapter.read_snapshot(
        page, run, marketplace, )

    assert snapshot.payable_amount == Decimal("0")
    assert snapshot.delayed_amount == Decimal("0")
    assert snapshot.can_submit is False
    assert snapshot.settlement_key == "NO_DATA:UK"
    assert snapshot.skip_reason is not None
    assert snapshot.skip_reason.startswith("无付款数据：")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("无付款数据", "您在此商城中没有任何可用的付款数据。"),
        (
            "No payments data",
            "You do not have any payments data available in this marketplace.",
        ),
    ],
)
async def test_no_data_message_accepts_observed_bilingual_alert_attributes(
    title: str, description: str
) -> None:
    # KAT web components can expose their useful text only through attributes.
    page = FixturePage(
        f'<html><body><kat-alert label="{title}" '
        f'description="{description}"></kat-alert></body></html>',
        "https://sellercentral.amazon.com.au/payments/dashboard/index.html",
    )

    message = await AmazonPaymentsPage()._no_data_message(page)

    assert message is not None
    assert title in message
    assert description in message


class _CountLocator:
    def __init__(self, page: "_DelayedNoDataPage", kind: str) -> None:
        self.page = page
        self.kind = kind

    async def count(self) -> int:
        if self.kind == "alerts":
            return int(self.page.alert_rendered)
        return 0


class _DelayedNoDataPage:
    """Models Amazon painting the empty-state alert after its page shell."""

    def __init__(self, adapter: AmazonPaymentsPage) -> None:
        self.adapter = adapter
        self.alert_rendered = False
        self.wait_calls: list[int] = []

    def locator(self, selector: str) -> _CountLocator:
        if selector == self.adapter.contract.alerts:
            return _CountLocator(self, "alerts")
        if selector == self.adapter.contract.balance_rows:
            return _CountLocator(self, "rows")
        raise AssertionError(selector)

    async def wait_for_function(
        self, expression: str, selectors: dict[str, str], *, timeout: int
    ) -> None:
        del expression, selectors
        self.wait_calls.append(timeout)
        if len(self.wait_calls) == 1:
            # The first structural wait times out while Amazon is still
            # painting the dashboard shell.
            raise TimeoutError("shell has no balance contract yet")
        self.alert_rendered = True


@pytest.mark.asyncio
async def test_dashboard_wait_gives_delayed_no_data_alert_a_second_window() -> None:
    adapter = AmazonPaymentsPage(navigation_timeout_ms=30_000)
    page = _DelayedNoDataPage(adapter)

    await adapter._wait_for_dashboard_contract(page)

    assert page.wait_calls == [15_000, 10_000]
    assert page.alert_rendered is True


@pytest.mark.asyncio
async def test_saved_statement_fixture_reads_status_label_and_today_record() -> None:
    adapter = AmazonPaymentsPage(today_provider=lambda: date(2026, 8, 11))
    page = fixture_page(
        "statements_zh_CA.html",
        "https://sellercentral.amazon.ca/payments/allstatements/index.html",
    )
    expected = MarketplaceSnapshot(
        marketplace_code="CA",
        domain="sellercentral.amazon.ca",
        seller_id="SELLER_FIXTURE",
        payment_account="493",
        currency="CAD",
        payable_amount=Decimal("2.89"),
        delayed_amount=Decimal("0"),
        settlement_key="OPEN:test",
        can_submit=False,
        contract_version="fixture",
        page_fingerprint="fixture",
    )
    result = await adapter._find_payment(page, expected, require_today=True)
    assert result.status.value == "CONFIRMED"
    assert result.platform_status == "已开始"
    assert result.platform_reference == "27271951371"


@pytest.mark.asyncio
async def test_live_statements_structure_is_matched() -> None:
    """The real page has no .disbursement-card — match its actual container.

    Field-measured 2026-08-18: on the live all-statements page the legacy row
    selectors matched zero elements, so _find_payment had no candidates to
    iterate and reported NOT_FOUND for a disbursement whose amount was plainly
    rendered.  That also silently disabled the platform-side idempotency check
    in open_confirmation, which relies on lookup_existing.
    """

    adapter = AmazonPaymentsPage(today_provider=lambda: date(2026, 8, 11))
    page = fixture_page(
        "allstatements_live_zh_CA.html",
        "https://sellercentral.amazon.ca/payments/allstatements/index.html",
    )
    expected = MarketplaceSnapshot(
        marketplace_code="CA",
        domain="sellercentral.amazon.ca",
        seller_id="SELLER_FIXTURE",
        payment_account="493",
        currency="CAD",
        payable_amount=Decimal("333.21"),
        delayed_amount=Decimal("0"),
        # A dashboard open-cycle key, exactly as the guard records it: it never
        # equals the numeric settlement id Amazon later assigns, so matching
        # must not depend on it.
        settlement_key="2026/8/11 - 至今",
        can_submit=False,
        contract_version="fixture",
        page_fingerprint="fixture",
    )

    result = await adapter._find_payment(page, expected, require_today=False)
    assert result.status.value == "CONFIRMED"
    assert result.platform_reference == "90000000001"
    assert result.platform_status == "已开始"

    # The other record on the page must not be mistaken for this one.
    other = replace(expected, payable_amount=Decimal("220.38"))
    other_result = await adapter._find_payment(page, other, require_today=False)
    assert other_result.platform_reference == "90000000002"

    # An amount that is on no record stays fail-closed.
    missing = replace(expected, payable_amount=Decimal("999.99"))
    assert (
        await adapter._find_payment(page, missing, require_today=False)
    ).status.value == "NOT_FOUND"


@pytest.mark.asyncio
async def test_saved_details_fixture_verifies_amount_account_tail_and_unique_button() -> None:
    adapter = AmazonPaymentsPage()
    page = fixture_page(
        "details_en_CA.html",
        "https://sellercentral.amazon.ca/payments/disburse/details?accountType=PAYABLE",
    )
    marketplace = MarketplaceRef(
        id="ca",
        code="CA",
        domain="sellercentral.amazon.ca",
        currency="CAD",
    )
    expected = MarketplaceSnapshot(
        marketplace_code="CA",
        domain=marketplace.domain,
        seller_id="SELLER_FIXTURE",
        payment_account="",
        currency="CAD",
        payable_amount=Decimal("2.89"),
        delayed_amount=Decimal("0"),
        settlement_key="OPEN:test",
        can_submit=True,
        contract_version="fixture",
        page_fingerprint="fixture",
    )
    verified = await adapter._verify_details(page, marketplace, expected)
    # The tail is read and carried out for the audit trail, never judged.
    assert verified.payment_account == "493"
    assert verified.payable_amount == Decimal("2.89")


@pytest.mark.asyncio
async def test_rate_limited_details_page_is_a_skip_not_a_contract_failure() -> None:
    """Amazon's own 24-hour throttle is a normal outcome, not a fault.

    Field-observed 2026-08-18: the dashboard offered an enabled Request
    disbursement button for AU$17.89, and only the confirmation page revealed
    「24 小时内仅限一次。22 hrs 49 mins 后再次请求。」with the final control
    disabled.  Reported as a broken page contract this looked like a failure and
    the site was marked FAILED; nothing on the dashboard could have predicted it.
    """

    adapter = AmazonPaymentsPage()
    page = fixture_page(
        "details_rate_limited_zh_AU.html",
        "https://sellercentral.amazon.com.au/payments/disburse/details?accountType=PAYABLE",
    )
    marketplace = MarketplaceRef(
        id="au",
        code="AU",
        domain="sellercentral.amazon.com.au",
        currency="AUD",
    )
    expected = MarketplaceSnapshot(
        marketplace_code="AU",
        domain=marketplace.domain,
        seller_id="SELLER_FIXTURE",
        payment_account="493",
        currency="AUD",
        payable_amount=Decimal("12.34"),
        delayed_amount=Decimal("0"),
        settlement_key="OPEN:test",
        can_submit=True,
        contract_version="fixture",
        page_fingerprint="fixture",
    )

    with pytest.raises(PayoutRateLimited) as raised:
        await adapter._verify_details(page, marketplace, expected)
    # Not the generic "button is not unique" complaint that used to fire here.
    assert not isinstance(raised.value, DomContractError)
    assert "24 小时" in str(raised.value)
    # The wait is quoted back so the operator knows when it is worth retrying.
    assert raised.value.retry_after and "22" in raised.value.retry_after


@pytest.mark.asyncio
async def test_rate_limited_page_stops_the_content_wait_immediately() -> None:
    """Waiting for a button Amazon has deliberately disabled is dead time.

    Field-observed 2026-08-19: AU burned the full 15-second details-contract
    timeout on every run before reading the throttle notice that was on the
    page the whole time.  Multiply by three sites and dozens of accounts.
    """

    adapter = AmazonPaymentsPage()
    page = fixture_page(
        "details_rate_limited_zh_AU.html",
        "https://sellercentral.amazon.com.au/payments/disburse/details?accountType=PAYABLE",
    )

    assert await adapter._details_contract_ready(page) is True


@pytest.mark.asyncio
async def test_normal_details_page_is_unaffected_by_the_rate_limit_check() -> None:
    """The throttle check must only relabel a branch that already failed."""

    adapter = AmazonPaymentsPage()
    page = fixture_page(
        "details_en_CA.html",
        "https://sellercentral.amazon.ca/payments/disburse/details?accountType=PAYABLE",
    )
    marketplace = MarketplaceRef(
        id="ca",
        code="CA",
        domain="sellercentral.amazon.ca",
        currency="CAD",
    )
    expected = MarketplaceSnapshot(
        marketplace_code="CA",
        domain=marketplace.domain,
        seller_id="SELLER_FIXTURE",
        payment_account="",
        currency="CAD",
        payable_amount=Decimal("2.89"),
        delayed_amount=Decimal("0"),
        settlement_key="OPEN:test",
        can_submit=True,
        contract_version="fixture",
        page_fingerprint="fixture",
    )

    verified = await adapter._verify_details(page, marketplace, expected)
    assert verified.can_submit is True


