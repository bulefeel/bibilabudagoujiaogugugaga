from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import unittest

import pytest

from ziniao_automation.workflows.amazon_disbursement import (
    AmazonDomContract,
    AmazonPaymentsPage,
    parse_amount,
)
from ziniao_automation.workflows.amazon_login import AmazonLoginAdvancer
from ziniao_automation.workflows.errors import DomContractError, HumanAuthRequired
from ziniao_automation.workflows.types import (
    MarketplaceRef,
    MarketplaceSnapshot,
    ReconcileResult,
    ReconcileStatus,
    RunMode,
    StoreRef,
    WorkflowRun,
    utc_now,
)


class _TextLocator:
    def __init__(self, text: str) -> None:
        self._text = text
        self.first = self

    async def count(self) -> int:
        return 1

    async def inner_text(self, **kwargs) -> str:
        return self._text


class _Row:
    def __init__(self, label: str, contract: AmazonDomContract) -> None:
        self.label = label
        self.contract = contract

    def locator(self, selector: str):
        if selector == self.contract.balance_label:
            return _TextLocator(self.label)
        raise AssertionError(selector)


class _Rows:
    def __init__(self, rows: list[_Row]) -> None:
        self.rows = rows

    async def count(self) -> int:
        return len(self.rows)

    def nth(self, index: int) -> _Row:
        return self.rows[index]


class _Page:
    def __init__(self, rows: list[_Row], contract: AmazonDomContract) -> None:
        self.rows = _Rows(rows)
        self.contract = contract

    def locator(self, selector: str):
        if selector == self.contract.balance_rows:
            return self.rows
        raise AssertionError(selector)


class _Button:
    def __init__(self) -> None:
        self.clicks = 0

    async def get_attribute(self, name: str):
        return None

    async def is_enabled(self) -> bool:
        return True

    async def click(self, **kwargs) -> None:
        self.clicks += 1


class _NativeChallengePage:
    """The browser-owned Passkey surface is intentionally absent from DOM."""

    url = "https://sellercentral.amazon.ca/payments/dashboard/index.html"


class _NativeChallengeAdapter(AmazonPaymentsPage):
    def __init__(self, snapshot: MarketplaceSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.button = _Button()

    async def lookup_existing(self, page, run, marketplace, expected):
        return ReconcileResult(ReconcileStatus.NOT_FOUND, utc_now())

    async def read_snapshot(self, page, run, marketplace):
        return self.snapshot

    async def _unique_payable_row(self, page):
        return object()

    async def _payout_buttons(self, container):
        return [self.button]

    async def _settle(self, page):
        return None

    async def _raise_if_auth(
        self,
        page,
        marketplace=None,
        *,
        allow_payment_details_handoff=False,
    ):
        del marketplace
        assert allow_payment_details_handoff is True
        # Models a native overlay: Amazon DOM and URL expose no marker.
        # The adapter contract returns the current/resolved Page even when no
        # document-visible marker exists; callers now preserve new-tab aliases.
        return page


def _confirmation_fixture():
    """The marketplace/run/snapshot triple shared by the re-dispatch tests."""

    marketplace = MarketplaceRef(
        id="market-ca",
        code="CA",
        domain="sellercentral.amazon.ca",
        currency="CAD",
    )
    run = WorkflowRun(
        id="redispatch-run",
        workflow="amazon_disbursement",
        mode=RunMode.APPROVAL,
        store=StoreRef(
            id="store-1",
            name="测试店铺",
            selector_type="oauth",
            selector_value="oauth-value",
            expected_seller_id="SELLER-123",
            identity_confirmed=True,
        ),
        marketplaces=(marketplace,),
    )
    expected = MarketplaceSnapshot(
        marketplace_code="CA",
        domain=marketplace.domain,
        seller_id=run.store.expected_seller_id,
        payment_account="",
        currency=marketplace.currency,
        payable_amount=Decimal("10.00"),
        delayed_amount=Decimal("0.00"),
        settlement_key="2026-08-cycle-1",
        can_submit=True,
        contract_version="test-v1",
        page_fingerprint="stable-dom",
    )
    return marketplace, run, expected


class _LoginAnsweredPage:
    """Amazon answered the dashboard press with its own sign-in route.

    No native window is involved: the document really did navigate, so nothing
    will ever move this URL to details on its own.
    """

    def __init__(self) -> None:
        self.url = "https://sellercentral.amazon.ca/ap/signin?openid.pape.max_auth_age=300"
        self.gotos: list[str] = []

    async def goto(self, url: str, **kwargs) -> None:
        del kwargs
        self.gotos.append(url)
        self.url = url

    async def wait_for_load_state(self, *a, **k) -> None:
        del a, k


class _LoginAnsweredAdapter(AmazonPaymentsPage):
    """Reaches details only if the dashboard control is pressed again."""

    def __init__(self, snapshot: MarketplaceSnapshot) -> None:
        super().__init__(pacing_range_ms=(0, 0))
        self.snapshot = snapshot
        self.button = _Button()
        self.verify_details_calls = 0

    async def lookup_existing(self, page, run, marketplace, expected):
        return ReconcileResult(ReconcileStatus.NOT_FOUND, utc_now())

    async def read_snapshot(self, page, run, marketplace, **kwargs):
        del kwargs
        return self.snapshot

    async def _unique_payable_row(self, page):
        return object()

    async def _payout_buttons(self, container):
        return [self.button]

    async def _settle(self, page):
        return None

    async def _goto_dashboard(self, page, marketplace):
        page.url = f"https://{marketplace.domain}/payments/dashboard/index.html"

    async def _raise_if_auth(self, page, marketplace=None, *, allow_payment_details_handoff=False):
        del marketplace, allow_payment_details_handoff
        return page

    async def _wait_for_nonfinal_confirmation_transition(self, page, marketplace):
        # The re-press is what finally commits the details route.  The test
        # pre-seeds the dispatch marker rather than pressing once for real, so
        # the re-press is click number one.
        if self.button.clicks >= 1:
            page.url = (
                f"https://{marketplace.domain}/payments/disburse/details"
                "?accountType=PAYABLE"
            )

    async def _wait_for_post_auth_payment_details(self, page, marketplace):
        del marketplace
        return page

    def _require_host(self, page, marketplace):
        del page, marketplace

    async def _verify_identity(self, page, expected, marketplace):
        del page, marketplace
        return expected, "fake:identity"

    async def _verify_details(self, page, marketplace, expected):
        del page, marketplace
        self.verify_details_calls += 1
        return expected


class AmountAndDomTests(unittest.IsolatedAsyncioTestCase):
    def test_money_is_decimal_and_supports_common_formats(self) -> None:
        self.assertEqual(parse_amount("CAD $1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_amount("-CA$197.63"), Decimal("-197.63"))
        self.assertEqual(parse_amount("−CA$197.63"), Decimal("-197.63"))
        self.assertEqual(parse_amount("CA$−197.63"), Decimal("-197.63"))
        self.assertEqual(parse_amount("£ 2,345.60"), Decimal("2345.60"))
        self.assertEqual(parse_amount("AUD 99.5"), Decimal("99.50"))
        self.assertEqual(parse_amount("- $12.34"), Decimal("-12.34"))

    async def test_two_payable_rows_are_rejected_as_ambiguous(self) -> None:
        contract = AmazonDomContract()
        page = _Page(
            [_Row("PAYABLE", contract), _Row("可支付", contract)], contract
        )
        adapter = AmazonPaymentsPage(contract)
        with self.assertRaises(DomContractError):
            await adapter._unique_payable_row(page)

    async def test_native_passkey_after_dashboard_click_waits_for_human(self) -> None:
        marketplace = MarketplaceRef(
            id="market-ca",
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
        )
        run = WorkflowRun(
            id="native-passkey-run",
            workflow="amazon_disbursement",
            mode=RunMode.APPROVAL,
            store=StoreRef(
                id="store-1",
                name="测试店铺",
                selector_type="oauth",
                selector_value="oauth-value",
                expected_seller_id="SELLER-123",
                identity_confirmed=True,
            ),
            marketplaces=(marketplace,),
        )
        expected = MarketplaceSnapshot(
            marketplace_code="CA",
            domain=marketplace.domain,
            seller_id=run.store.expected_seller_id,
            payment_account="",
            currency=marketplace.currency,
            payable_amount=Decimal("10.00"),
            delayed_amount=Decimal("0.00"),
            settlement_key="2026-08-cycle-1",
            can_submit=True,
            contract_version="test-v1",
            page_fingerprint="stable-dom",
        )
        adapter = _NativeChallengeAdapter(expected)
        page = _NativeChallengePage()
        with self.assertRaises(HumanAuthRequired) as caught:
            await adapter.open_confirmation(
                page, run, marketplace, expected
            )

        self.assertEqual(caught.exception.kind, "native_passkey_or_challenge")
        self.assertEqual(adapter.button.clicks, 1)

        # Pressing Continue before the native window has completed must wait
        # again without dispatching the dashboard button a second time.
        with self.assertRaises(HumanAuthRequired):
            await adapter.open_confirmation(
                page, run, marketplace, expected
            )
        self.assertEqual(adapter.button.clicks, 1)


    async def test_login_answered_dispatch_is_re_dispatched_once(self) -> None:
        """A press answered with /ap/signin must be recoverable, not permanent.

        Field-reproduced 2026-08-18: the details route demands auth newer than
        300s while the dashboard does not, so the press lands on /ap/signin.
        The one-shot marker then made every later continue re-enter a wait for
        a URL that nothing was going to change.
        """

        marketplace, run, expected = _confirmation_fixture()
        adapter = _LoginAnsweredAdapter(expected)
        page = _LoginAnsweredPage()
        key = (id(page), "CA")
        adapter._confirmation_dispatched.add(key)

        verified = await adapter.open_confirmation(page, run, marketplace, expected)

        self.assertEqual(verified, expected)
        self.assertEqual(adapter.button.clicks, 1, "exactly one re-dispatch")
        self.assertTrue(adapter._is_expected_details_url(page.url, marketplace))
        self.assertEqual(adapter.verify_details_calls, 1)

    async def test_re_dispatch_budget_is_spent_only_once(self) -> None:
        """A second failure must not buy another press."""

        marketplace, run, expected = _confirmation_fixture()
        adapter = _LoginAnsweredAdapter(expected)

        # Never let the re-press commit the details route.
        async def _never_arrives(page, mk):
            del page, mk
        adapter._wait_for_nonfinal_confirmation_transition = _never_arrives

        page = _LoginAnsweredPage()
        adapter._confirmation_dispatched.add((id(page), "CA"))

        with self.assertRaises(HumanAuthRequired) as first:
            await adapter.open_confirmation(page, run, marketplace, expected)
        self.assertEqual(first.exception.kind, "native_passkey_or_challenge")
        self.assertEqual(adapter.button.clicks, 1)

        page.url = "https://sellercentral.amazon.ca/ap/signin"
        with self.assertRaises(HumanAuthRequired):
            await adapter.open_confirmation(page, run, marketplace, expected)
        self.assertEqual(adapter.button.clicks, 1, "budget must not refill")

    async def test_a_balance_that_grew_since_planning_still_opens_the_confirmation(
        self,
    ) -> None:
        """The adapter must bind the account, not the number.

        ``execute()`` compares ``binding_hash`` precisely so that an accruing
        balance — the normal case, since more orders settle while a queue of
        stores is worked through — does not abort the site.  This adapter then
        re-checked the same snapshot with ``snapshot_hash``, which *does*
        include both money figures, so the relaxation one call earlier never
        took effect and an ordinary few cents of accrual silently failed the
        site with 「打开确认页前金额…发生变化」.
        """

        marketplace, run, expected = _confirmation_fixture()
        grown = replace(expected, payable_amount=expected.payable_amount + Decimal("0.01"))
        self.assertNotEqual(grown.snapshot_hash, expected.snapshot_hash)
        self.assertEqual(grown.binding_hash, expected.binding_hash)

        adapter = _LoginAnsweredAdapter(grown)
        page = _LoginAnsweredPage()
        adapter._confirmation_dispatched.add((id(page), "CA"))

        verified = await adapter.open_confirmation(page, run, marketplace, expected)

        self.assertEqual(verified, expected)
        self.assertEqual(adapter.verify_details_calls, 1)

    async def test_a_site_that_became_unsubmittable_still_refuses(self) -> None:
        """Relaxing the amount must not relax the page's own verdict."""

        marketplace, run, expected = _confirmation_fixture()
        closed = replace(expected, can_submit=False)
        adapter = _LoginAnsweredAdapter(closed)
        page = _LoginAnsweredPage()
        adapter._confirmation_dispatched.add((id(page), "CA"))

        with self.assertRaises(DomContractError):
            await adapter.open_confirmation(page, run, marketplace, expected)
        self.assertEqual(adapter.verify_details_calls, 0)

    async def test_a_different_seller_on_the_dashboard_still_refuses(self) -> None:
        """The binding still covers everything that decides *whose* money it is."""

        marketplace, run, expected = _confirmation_fixture()
        other = replace(expected, seller_id="SELLER-OTHER")
        adapter = _LoginAnsweredAdapter(other)
        page = _LoginAnsweredPage()
        adapter._confirmation_dispatched.add((id(page), "CA"))

        with self.assertRaises(DomContractError):
            await adapter.open_confirmation(page, run, marketplace, expected)
        self.assertEqual(adapter.verify_details_calls, 0)




class _StepUpAfterPressPage(_LoginAnsweredPage):
    """The dashboard press is answered with a step-up sign-in, asynchronously.

    ``url`` still reads as the dashboard for the first look after the click and
    only flips to ``/ap/signin`` on the next one.  That one-read gap is the
    whole defect: ``_raise_if_auth`` samples the URL exactly once, with no
    wait, so it saw "no login page" and the advancer was never started.
    """

    def __init__(self) -> None:
        super().__init__()
        self.url = "https://sellercentral.amazon.ca/payments/dashboard/index.html"
        self._signin_pending = False

    def commit_click(self) -> None:
        """The press is away; Amazon's redirect has not landed yet."""

        self._signin_pending = True

    def observe(self) -> None:
        """The next poll after the press is where the redirect shows up.

        ``_raise_if_auth`` samples ``url`` before this ever runs — which is
        exactly why it saw the dashboard and skipped the advancer.
        """

        if self._signin_pending:
            self.url = (
                "https://sellercentral.amazon.ca/ap/signin"
                "?openid.pape.max_auth_age=300"
            )
            self._signin_pending = False


class _StepUpAfterPressAdapter(_LoginAnsweredAdapter):
    """Reaches details only if the advancer is given a turn on the sign-in page."""

    def __init__(self, snapshot: MarketplaceSnapshot) -> None:
        super().__init__(snapshot)
        self.auth_calls: list[str] = []

    async def _wait_for_nonfinal_confirmation_transition(self, page, marketplace):
        del marketplace
        page.commit_click()

    async def _raise_if_auth(
        self, page, marketplace=None, *, allow_payment_details_handoff=False
    ):
        del allow_payment_details_handoff
        self.auth_calls.append(str(page.url))
        if AmazonLoginAdvancer.is_supported_login_url(
            str(page.url),
            expected_host=marketplace.domain if marketplace else None,
        ):
            # Stands in for the advancer completing Ziniao's managed Passkey.
            page.url = (
                f"https://{marketplace.domain}/payments/disburse/details"
                "?accountType=PAYABLE"
            )
        return page

    async def _wait_for_post_auth_payment_details(self, page, marketplace):
        del marketplace
        page.observe()
        return page


@pytest.mark.asyncio
async def test_step_up_triggered_by_the_press_is_advanced_not_handed_to_a_human() -> None:
    """Field case TEST_STORE_CA: a site dropped while Passkey was on screen.

    Pressing Request disbursement on CA landed on ``/ap/signin`` with
    ``max_auth_age=300``.  ``_raise_if_auth`` had already sampled the URL one
    moment too early, so it saw the dashboard and skipped the advancer; the
    handoff poll then saw the sign-in route 1 ms later and the caller raised
    ``native_passkey_or_challenge`` on the spot.  That whole run's log contains
    no ``amazon_login`` line at all — nothing ever tried to log in, while
    Ziniao's managed-Passkey chooser sat there waiting to be clicked.
    """

    marketplace, run, expected = _confirmation_fixture()
    marketplace = replace(marketplace, code="CA", domain="sellercentral.amazon.ca")
    adapter = _StepUpAfterPressAdapter(expected)
    page = _StepUpAfterPressPage()

    verified = await adapter.open_confirmation(page, run, marketplace, expected)

    assert verified == expected
    assert adapter.button.clicks == 1, "the dashboard control is pressed once"
    # Two turns: the early one that legitimately saw no login page, and the
    # second chance that actually resolved the step-up.
    assert len(adapter.auth_calls) == 2
    assert "/payments/dashboard/" in adapter.auth_calls[0]
    assert "/ap/signin" in adapter.auth_calls[1]
    assert adapter._is_expected_details_url(page.url, marketplace)


@pytest.mark.asyncio
async def test_a_press_that_lands_somewhere_unknown_still_reaches_a_human() -> None:
    """Only a login route buys the retry; anything else still fails closed."""

    marketplace, run, expected = _confirmation_fixture()
    marketplace = replace(marketplace, code="CA", domain="sellercentral.amazon.ca")
    adapter = _StepUpAfterPressAdapter(expected)
    page = _StepUpAfterPressPage()

    async def land_on_an_unknown_page(page_, marketplace_):
        del marketplace_
        page_.observe()
        if "/ap/signin" in str(page_.url):
            page_.url = "https://sellercentral.amazon.ca/help/hub/reference/G200"
        return page_

    adapter._wait_for_post_auth_payment_details = land_on_an_unknown_page

    with pytest.raises(HumanAuthRequired) as raised:
        await adapter.open_confirmation(page, run, marketplace, expected)

    assert raised.value.kind == "native_passkey_or_challenge"


if __name__ == "__main__":
    unittest.main()
