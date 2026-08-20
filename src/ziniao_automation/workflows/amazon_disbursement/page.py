"""Amazon Seller Central classic-payments Playwright adapter.

The observed UI is built from ``multi-row-card-row`` and KAT web components.
KAT components commonly render an empty ``innerText`` while their useful text
is in ``label``/``description`` attributes, so every extractor uses an
attribute-aware fallback.  Only the final details-page click submits money.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import logging
from pathlib import Path
import random
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from weakref import WeakKeyDictionary

from ..amazon_login import AmazonLoginAdvancer
from ..contracts import MarketplacePageAdapter
from ..errors import (
    DomContractError,
    HumanAuthRequired,
    PayoutRateLimited,
    PlatformDisbursementExists,
    PreflightRejected,
    SubmissionNotDispatched,
)
from ..types import (
    EvidenceRef,
    MarketplaceRef,
    MarketplaceSnapshot,
    OperationRecord,
    PreflightResult,
    ReconcileResult,
    ReconcileStatus,
    RunMode,
    SubmissionReceipt,
    WorkflowRun,
    stable_hash,
    utc_now,
)
from .config import ALLOWED_MARKETPLACES, AmazonDomContract

_AMOUNT = re.compile(
    r"(?P<prefix_sign>[-−–—])?\s*(?:[A-Z]{2,4}\s*)?(?:[$£€]\s*)?"
    r"(?P<suffix_sign>[-−–—])?\s*(?P<number>\d[\d\s,.]*)"
)
_MONEY_TOKEN = re.compile(
    r"[-−–—]?\s*(?:[A-Z]{2,4}\s*)?(?:[$£€]\s*)?[-−–—]?\s*\d[\d\s,.]*",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


class _CompositeBalanceRow:
    """One logical balance row assembled from Seller Central column cards."""

    def __init__(self, label: Any, total: Any, available: Any) -> None:
        self.label = label
        self.total = total
        self.available = available

    def locator(self, selector: str) -> Any:
        # Buttons live only in the available-funds cell.  Attribute-aware text
        # is handled explicitly by AmazonPaymentsPage for the three cells.
        return self.available.locator(selector)

    async def evaluate(self, expression: str) -> str:
        del expression
        parts: list[str] = []
        for cell in (self.label, self.total, self.available):
            try:
                parts.append(await cell.evaluate("element => element.outerHTML"))
            except Exception:
                parts.append("")
        return "<logical-balance-row>" + "".join(parts) + "</logical-balance-row>"


class AmazonPaymentsPage:
    def __init__(
        self,
        contract: AmazonDomContract | None = None,
        *,
        navigation_timeout_ms: int = 30_000,
        today_provider: Callable[[], date] = date.today,
        pacing_range_ms: tuple[int, int] = (1_200, 2_400),
        pacing_random: Callable[[float, float], float] = random.uniform,
        login_advancer: AmazonLoginAdvancer | None = None,
    ) -> None:
        self.contract = contract or AmazonDomContract()
        self.navigation_timeout_ms = navigation_timeout_ms
        self._today = today_provider
        # Which marketplaces have already had their sole irreversible final
        # click dispatched on a given live Page.
        #
        # Keyed per marketplace because one run drives every site through the
        # SAME Playwright Page — the engine opens one financial session per run.
        # A page-wide marker meant the first site to pay silently vetoed all the
        # others: the second site armed its guard and then could never click.
        #
        # Keyed by the Page object rather than id(page) for the same reason
        # _verified_identities below is: this adapter is process-lifetime (see
        # composition.py) and the marker is never cleared, so a numeric id freed
        # by a closed Page would eventually be handed to an unrelated later Page
        # and refuse a legitimate payout — reproducing exactly the failure above.
        self._consumed_marketplaces: WeakKeyDictionary[Any, set[str]] = (
            WeakKeyDictionary()
        )
        # A dashboard request-disbursement click is non-final, but repeating it
        # while a browser-owned Passkey window is still open is unsafe and
        # unnatural. Keep a per-live-page/site marker until details is reached.
        self._confirmation_dispatched: set[tuple[int, str]] = set()
        # The marker above stops a second dashboard click, but on its own it
        # also removed the only way back: when a dispatch never reached the
        # details page, every later continue just waited for a URL that nothing
        # was going to change.  Allow exactly one re-dispatch per live
        # page/site, tracked here so the budget cannot be reset by re-entry.
        self._nonfinal_retry_used: set[tuple[int, str]] = set()
        # Seller proofs are valid only for the exact live Playwright Page.
        # Weak keys prevent a closed Page's numeric object id from being reused
        # by a later store/session and accidentally hitting stale identity data.
        self._verified_identities: WeakKeyDictionary[
            Any, dict[str, str]
        ] = WeakKeyDictionary()
        self._page_aliases: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
        low, high = pacing_range_ms
        if low < 0 or high < low:
            raise ValueError("pacing_range_ms must be non-negative and ordered")
        self._pacing_range_ms = (int(low), int(high))
        self._pacing_random = pacing_random
        self._login_advancer = login_advancer or AmazonLoginAdvancer(
            pacing_random=pacing_random
        )
        self._last_business_navigation_at: float | None = None

    async def preflight(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
    ) -> PreflightResult:
        page = self.resolved_page_for(page)
        self._validate_static_configuration(run, marketplace)
        await self._goto_dashboard(page, marketplace)
        page = await self._raise_if_auth(page, marketplace)
        self._require_host(page, marketplace)
        seller_id, source = await self._verify_identity(
            page, run.store.expected_seller_id, marketplace
        )

        # Identity discovery may temporarily visit the read-only account
        # switcher when the Payments shell exposes no identity attributes.
        # Always restore and re-check the exact business page before reading
        # balances or buttons.
        if "/payments/dashboard/" not in urlparse(str(page.url)).path.lower():
            await self._goto_dashboard(page, marketplace)
            page = await self._raise_if_auth(page, marketplace)
            self._require_host(page, marketplace)

        no_data = await self._no_data_message(page)
        if no_data is None and await page.locator(self.contract.balance_rows).count() < 1:
            raise DomContractError(
                "付款页既没有 multi-row-card-row，也没有官方无付款数据提示"
            )
        return PreflightResult(
            marketplace_code=marketplace.code,
            seller_id=seller_id,
            # Only the confirmation page reveals the destination account.
            payment_account="",
            observed_domain=_host(str(page.url)),
            contract_version=f"{self.contract.version}:{source}",
        )

    async def detect_identity(
        self, page: Any, marketplace: MarketplaceRef
    ) -> tuple[str, str]:
        """Open Seller Central and discover one stable seller identity.

        This is deliberately read-only: it only navigates to the payments
        dashboard and reads URL/DOM values.  It never reads browser storage,
        cookies or tokens and never clicks a page element.
        """

        page = self.resolved_page_for(page)
        await self._goto_dashboard(page, marketplace)
        page = await self._raise_if_auth(page, marketplace)
        self._require_host(page, marketplace)
        try:
            return await self._detect_identity(page)
        except PreflightRejected:
            # The current Seller Central shell no longer exposes the account
            # name on every Payments dashboard.  Its read-only account
            # switcher still renders the active account as a normal button
            # (for example ``name (当前)`` / ``name (current)``).  Navigate
            # there only as a discovery fallback; never click a row or the
            # confirmation control.
            return await self._detect_identity_from_account_switcher(
                page, marketplace
            )

    async def read_snapshot(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
    ) -> MarketplaceSnapshot:
        page = self.resolved_page_for(page)
        checked = await self.preflight(page, run, marketplace)
        page = self.resolved_page_for(page)
        no_data = await self._no_data_message(page)
        if no_data is not None:
            return MarketplaceSnapshot(
                marketplace_code=marketplace.code,
                domain=checked.observed_domain,
                seller_id=checked.seller_id,
                # Only the confirmation page knows the destination; a dashboard
                # read legitimately has nothing to report here.
                payment_account="",
                currency=marketplace.currency,
                payable_amount=Decimal("0"),
                delayed_amount=Decimal("0"),
                settlement_key=f"NO_DATA:{marketplace.code}",
                can_submit=False,
                contract_version=self.contract.version,
                page_fingerprint=stable_hash({"no_data": no_data}),
                skip_reason=f"无付款数据：{no_data}",
                identity_source=checked.contract_version.split(":", 1)[-1],
            )

        rows = await self._classified_rows(page)
        payable_rows = [row for kind, row in rows if kind == "PAYABLE"]
        if len(payable_rows) != 1:
            raise DomContractError(
                f"严格匹配到 {len(payable_rows)} 个标准订单行，要求恰好 1 个"
            )
        payable_row = payable_rows[0]
        payable = await self._available_amount(payable_row, marketplace.currency)
        delayed_rows = [row for kind, row in rows if kind == "DEFERRED"]
        all_rows = [row for kind, row in rows if kind == "ALL"]
        delayed = Decimal("0")
        delayed_total: Decimal | None = None
        if len(delayed_rows) > 1 or len(all_rows) > 1:
            raise DomContractError("延迟交易行或所有账户汇总行不唯一")
        if delayed_rows:
            delayed = await self._available_amount(delayed_rows[0], marketplace.currency)
            delayed_total = await self._total_amount(delayed_rows[0], marketplace.currency)

        payable_total = await self._total_amount(payable_row, marketplace.currency)
        all_total = (
            await self._total_amount(all_rows[0], marketplace.currency)
            if all_rows
            else None
        )
        if all_total is not None and delayed_total is not None:
            # Real page invariant: Standard Orders + Deferred = All Accounts.
            if abs((payable_total + delayed_total) - all_total) > Decimal("0.01"):
                raise DomContractError(
                    "余额自校验失败：标准订单 + 延迟交易 != 所有账户"
                )

        buttons = await self._payout_buttons(payable_row)
        if len(buttons) > 1:
            raise DomContractError("标准订单行内请求付款按钮不唯一")
        enabled = bool(buttons) and await self._is_enabled(buttons[0])
        can_submit = payable > 0 and enabled
        reason = None
        if payable <= 0:
            reason = "标准订单可用资金为 0"
        elif not buttons:
            reason = "标准订单行无请求付款按钮"
        elif not enabled:
            reason = "请求付款窗口未到或按钮禁用"

        period = await self._dashboard_period(page, payable_row)
        fingerprint = await self._fingerprint(payable_row)
        return MarketplaceSnapshot(
            marketplace_code=marketplace.code,
            domain=checked.observed_domain,
            seller_id=checked.seller_id,
            payment_account="",
            currency=marketplace.currency,
            payable_amount=payable,
            delayed_amount=delayed,
            settlement_key=period,
            can_submit=can_submit,
            contract_version=self.contract.version,
            page_fingerprint=fingerprint,
            skip_reason=reason,
            identity_source=checked.contract_version.split(":", 1)[-1],
        )

    async def capture_evidence(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        *,
        label: str,
    ) -> EvidenceRef | None:
        page = self.resolved_page_for(page)
        if run.artifact_dir is None:
            return None
        directory = Path(run.artifact_dir) / run.id
        directory.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "evidence"
        target = directory / f"{marketplace.code}-{safe_label}.png"
        await page.screenshot(path=str(target), full_page=True)
        return EvidenceRef(
            kind="screenshot",
            file_path=str(target),
            sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            metadata={"marketplace": marketplace.code, "label": label},
        )

    async def lookup_existing(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        expected: MarketplaceSnapshot,
    ) -> ReconcileResult:
        page = self.resolved_page_for(page)
        await self._goto_statements(page, marketplace)
        page = await self._raise_if_auth(page, marketplace)
        self._require_host(page, marketplace)
        await self._verify_identity(page, run.store.expected_seller_id, marketplace)
        if "/payments/allstatements/" not in urlparse(str(page.url)).path.lower():
            await self._goto_statements(page, marketplace)
            page = await self._raise_if_auth(page, marketplace)
            self._require_host(page, marketplace)
        return await self._find_payment(page, expected, require_today=True)

    async def open_confirmation(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        expected: MarketplaceSnapshot,
    ) -> MarketplaceSnapshot:
        """Perform the non-final dashboard click and verify the details page.

        Authentication can appear after the dashboard click.  In that case a
        :class:`HumanAuthRequired` escapes; the engine keeps the same browser
        and locks, then reruns this method.  If already on details, it never
        clicks the dashboard a second time.
        """
        page = self.resolved_page_for(page)
        confirmation_key = (id(page), marketplace.code.upper())
        if self._is_expected_details_url(str(page.url), marketplace):
            page = await self._raise_if_auth(
                page,
                marketplace,
                allow_payment_details_handoff=True,
            )
            confirmation_key = (id(page), marketplace.code.upper())
            self._require_host(page, marketplace)
            await self._verify_identity(
                page, run.store.expected_seller_id, marketplace
            )
            verified = await self._verify_details(page, marketplace, expected)
            self._confirmation_dispatched.discard(confirmation_key)
            return verified

        if confirmation_key in self._confirmation_dispatched:
            # The preceding dispatch opened a native browser/Windows auth
            # surface while the document stayed on dashboard. Do not click it
            # a second time. The operator can press Continue again after the
            # visible Ziniao tab has actually reached details.
            #
            # That reasoning only holds while the document really did stay put.
            # If Amazon answered the press with its own sign-in route, no
            # native window is involved and nothing will ever move the URL to
            # details on its own — see _retry_nonfinal_dispatch.  Record which
            # of the two it is before the advancer rewrites the URL.
            answered_with_login = AmazonLoginAdvancer.is_supported_login_url(
                str(getattr(page, "url", "") or ""),
                expected_host=marketplace.domain,
            )
            page = await self._raise_if_auth(
                page,
                marketplace,
                allow_payment_details_handoff=True,
            )
            page = await self._wait_for_post_auth_payment_details(page, marketplace)
            confirmation_key = (id(page), marketplace.code.upper())
            if answered_with_login and not self._is_expected_details_url(
                str(page.url), marketplace
            ):
                # Waiting alone can never fix this: the details page is only
                # reachable by pressing the dashboard control, and that press
                # is exactly what the marker above forbids.  Spend the one
                # re-dispatch instead of raising forever.
                page = await self._retry_nonfinal_dispatch(
                    page, run, marketplace, expected
                )
                confirmation_key = (id(page), marketplace.code.upper())
            if not self._is_expected_details_url(str(page.url), marketplace):
                raise HumanAuthRequired(
                    "付款确认仍在等待人工 Passkey 或安全验证",
                    kind="native_passkey_or_challenge",
                )
            self._require_host(page, marketplace)
            await self._verify_identity(
                page, run.store.expected_seller_id, marketplace
            )
            verified = await self._verify_details(page, marketplace, expected)
            self._confirmation_dispatched.discard(confirmation_key)
            return verified

        # Required platform-side idempotency check comes before any click.
        existing = await self.lookup_existing(page, run, marketplace, expected)
        page = self.resolved_page_for(page)
        confirmation_key = (id(page), marketplace.code.upper())
        if existing.status in (
            ReconcileStatus.CONFIRMED,
            ReconcileStatus.PENDING,
            ReconcileStatus.CONFLICT,
        ):
            raise PlatformDisbursementExists(
                "付款记录已存在同日相同金额/周期，未打开确认页",
                result=existing,
            )

        current = await self.read_snapshot(page, run, marketplace)
        page = self.resolved_page_for(page)
        confirmation_key = (id(page), marketplace.code.upper())
        # ``binding_hash``, not ``snapshot_hash``: the balance keeps accruing
        # between planning and here — a queue of stores and an auth pause can
        # put minutes in between — and pressing Request disbursement transfers
        # whatever Amazon considers payable at that instant anyway.  Binding the
        # figure re-imposed exactly the abort the workflow-level check one call
        # earlier was relaxed to avoid, so the relaxation never took effect.
        # Everything that decides *which account* is being operated is still
        # bound, and ``can_submit`` is inside it.
        if current.binding_hash != expected.binding_hash:
            raise DomContractError("打开确认页前身份、站点或页面结构发生变化")
        if not current.can_submit:
            raise DomContractError("当前标准订单余额不可请求付款")
        row = await self._unique_payable_row(page)
        buttons = await self._payout_buttons(row)
        if len(buttons) != 1 or not await self._is_enabled(buttons[0]):
            raise DomContractError("面板请求付款按钮不唯一或不可用")
        await self._paced_pause(page, minimum_ms=1_800, maximum_ms=3_200)
        self._confirmation_dispatched.add(confirmation_key)
        await buttons[0].click(no_wait_after=True)
        await self._wait_for_nonfinal_confirmation_transition(page, marketplace)
        await self._settle(page)
        page = await self._raise_if_auth(
            page,
            marketplace,
            allow_payment_details_handoff=True,
        )
        page = await self._wait_for_post_auth_payment_details(page, marketplace)
        confirmation_key = (id(page), marketplace.code.upper())
        if not self._is_expected_details_url(str(page.url), marketplace):
            # A native Passkey/security-key window can be owned by Chromium or
            # Windows rather than the document.  In that case neither the URL
            # nor Amazon's DOM exposes a challenge marker.  Preserve the live
            # browser and let the normal WAITING_AUTH lease ask the operator to
            # finish it; continuing while it is still present safely lands here
            # again and never reaches ARMED/the final click.
            raise HumanAuthRequired(
                "面板操作后仍停留在付款页，请完成人工 Passkey 或安全验证",
                kind="native_passkey_or_challenge",
            )
        self._require_host(page, marketplace)
        await self._verify_identity(page, run.store.expected_seller_id, marketplace)
        verified = await self._verify_details(page, marketplace, expected)
        self._confirmation_dispatched.discard(confirmation_key)
        return verified

    async def _retry_nonfinal_dispatch(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        expected: MarketplaceSnapshot,
    ) -> Any:
        """Re-open the details page once after a dispatch that never arrived.

        The dashboard's request-disbursement control is a **non-final**
        transition: it opens the confirmation page and moves no money — the
        account-setup probe presses it routinely.  The one-shot marker exists
        to stop a second *money-adjacent* press while a browser-owned auth
        window is open, but it also removed the only route back, so a dispatch
        that was answered with a login page could never be recovered: every
        operator continue re-entered the bounded wait and re-raised.

        Field-reproduced 2026-08-18: pressing the control returns ``/ap/signin``
        within ~3 s because ``/payments/disburse/details`` demands
        authentication newer than 300 s, while reading the dashboard does not.

        Budget: one retry per live page/site, never reset.  The amount,
        identity and page contract are re-read first, so the retry can only
        re-open the *same* approved transition, never a wider one.
        """

        key = (id(page), marketplace.code.upper())
        if key in self._nonfinal_retry_used:
            return page
        self._nonfinal_retry_used.add(key)
        logger.info(
            "Re-dispatching the non-final disbursement transition: "
            "marketplace=%s url=%s",
            marketplace.code,
            str(getattr(page, "url", "") or ""),
            extra={"marketplace": marketplace.code, "event": "nonfinal_redispatch"},
        )

        # Clear whatever auth the click ran into, then start again from the
        # dashboard so the control is resolved against a live document.
        page = await self._raise_if_auth(
            page, marketplace, allow_payment_details_handoff=True
        )
        await self._goto_dashboard(page, marketplace)
        page = await self._raise_if_auth(page, marketplace)
        page = self.resolved_page_for(page)
        self._require_host(page, marketplace)

        current = await self.read_snapshot(page, run, marketplace)
        page = self.resolved_page_for(page)
        if current.binding_hash != expected.binding_hash:
            raise DomContractError("重开确认页前身份、站点或页面结构发生变化")
        if not current.can_submit:
            raise DomContractError("重开确认页时标准订单余额已不可请求付款")
        row = await self._unique_payable_row(page)
        buttons = await self._payout_buttons(row)
        if len(buttons) != 1 or not await self._is_enabled(buttons[0]):
            raise DomContractError("重开确认页时面板请求付款按钮不唯一或不可用")

        await self._paced_pause(page, minimum_ms=1_800, maximum_ms=3_200)
        self._confirmation_dispatched.add((id(page), marketplace.code.upper()))
        self._nonfinal_retry_used.add((id(page), marketplace.code.upper()))
        await buttons[0].click(no_wait_after=True)
        await self._wait_for_nonfinal_confirmation_transition(page, marketplace)
        await self._settle(page)
        page = await self._raise_if_auth(
            page, marketplace, allow_payment_details_handoff=True
        )
        return await self._wait_for_post_auth_payment_details(page, marketplace)

    async def submit_once(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        expected: MarketplaceSnapshot,
    ) -> SubmissionReceipt:
        """Click the sole final details-page button exactly once.

        Everything below the consumption marker is recovery-only; everything
        above it provably precedes the click.  A failure from the second group
        is re-raised as :class:`SubmissionNotDispatched` so the caller may
        release its ARMED guard instead of stranding a payout that never
        happened.  ``HumanAuthRequired`` is deliberately excluded: the engine
        dispatches on its type to park and wait for a human.
        """

        code = marketplace.code.upper()
        # Held so the "did it dispatch?" question below is answered by the very
        # state the guard consults, not by a parallel flag that could drift out
        # of step with it.
        consumed: set[str] | None = None
        try:
            page = self.resolved_page_for(page)
            consumed = self._page_consumed_marketplaces(page)
            if code in consumed:
                raise DomContractError(f"本次浏览器会话 {code} 的最终提交按钮已经触发过")
            page = await self._raise_if_auth(
                page,
                marketplace,
                allow_payment_details_handoff=True,
            )
            # _raise_if_auth may hand off to a different tab, which carries the
            # markers with it; re-read from whichever Page is now current.
            consumed = self._page_consumed_marketplaces(page)
            if code in consumed:
                raise DomContractError(f"本次浏览器会话 {code} 的最终提交按钮已经触发过")
            if not self._is_expected_details_url(str(page.url), marketplace):
                raise DomContractError("最终提交只允许在 /payments/disburse/details 页面")
            self._require_host(page, marketplace)
            await self._verify_identity(page, run.store.expected_seller_id, marketplace)
            verified = await self._verify_details(page, marketplace, expected)
            if verified.payable_amount != expected.payable_amount:
                raise DomContractError("确认页金额与 ARMED 意图不一致")
            buttons = await self._matching_buttons(page)
            enabled = [button for button in buttons if await self._is_enabled(button)]
            if len(enabled) != 1:
                raise DomContractError("确认页最终请求付款按钮不是唯一一个可用按钮")

            # Sole irreversible click.  Mark consumed immediately before
            # dispatch; every subsequent exception is recovery-only.
            consumed.add(code)
            await self._paced_pause(page, minimum_ms=2_800, maximum_ms=4_800)
            await enabled[0].click(no_wait_after=True)
            await self._settle(page)
            body = await self._body_text(page)
            success = bool(self.contract.success_pattern.search(body))
            return SubmissionReceipt(
                submitted_at=utc_now(),
                observed_status="success_banner" if success else "click_dispatched",
            )
        except HumanAuthRequired:
            raise
        except Exception as exc:
            if consumed is not None and code in consumed:
                # The marker is set: either this call already clicked, or an
                # earlier one did.  Both mean the caller must not release.
                raise
            raise SubmissionNotDispatched(str(exc)) from exc

    async def reconcile(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        operation: OperationRecord,
    ) -> ReconcileResult:
        page = self.resolved_page_for(page)
        await self._goto_statements(page, marketplace)
        page = await self._raise_if_auth(page, marketplace)
        self._require_host(page, marketplace)
        await self._verify_identity(page, run.store.expected_seller_id, marketplace)
        if "/payments/allstatements/" not in urlparse(str(page.url)).path.lower():
            await self._goto_statements(page, marketplace)
            page = await self._raise_if_auth(page, marketplace)
            self._require_host(page, marketplace)
        expected = MarketplaceSnapshot(
            marketplace_code=marketplace.code,
            domain=marketplace.domain,
            seller_id=run.store.expected_seller_id,
            payment_account=operation.intent.payout_account_tail,
            currency=operation.intent.currency,
            payable_amount=operation.intent.amount,
            delayed_amount=Decimal("0"),
            settlement_key=operation.intent.settlement_key,
            can_submit=False,
            contract_version=self.contract.version,
            page_fingerprint="reconcile",
        )
        return await self._find_payment(page, expected, require_today=False)

    def _validate_static_configuration(
        self,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
    ) -> None:
        code = marketplace.code.upper()
        allowed = ALLOWED_MARKETPLACES.get(code)
        if allowed is None:
            raise PreflightRejected(f"站点不在 V1 白名单中：{code}")
        domain, currency = allowed
        if not run.store.enabled or not run.store.identity_confirmed:
            raise PreflightRejected("店铺未启用或卖家身份尚未人工确认")
        if run.store.selector_type not in ("oauth", "id") or not run.store.selector_value.strip():
            raise PreflightRejected("店铺没有明确的紫鸟环境选择器")
        if not run.store.expected_seller_id.strip():
            raise PreflightRejected("店铺没有稳定的预期卖家身份")
        if not marketplace.enabled:
            raise PreflightRejected(f"站点 {code} 未启用")
        if marketplace.domain.lower() != domain:
            raise PreflightRejected(f"站点 {code} 域名不是固定白名单值")
        if marketplace.currency.upper() != currency:
            raise PreflightRejected(f"站点 {code} 币种与白名单不一致")

    async def _goto_dashboard(self, page: Any, marketplace: MarketplaceRef) -> None:
        await self._goto(page, marketplace.payments_url)
        await self._wait_for_dashboard_contract(page)

    async def _goto_statements(self, page: Any, marketplace: MarketplaceRef) -> None:
        await self._goto(
            page,
            f"https://{marketplace.domain}/payments/allstatements/index.html",
        )
        await self._wait_for_statements_contract(page)

    async def _wait_for_statements_contract(self, page: Any) -> None:
        """Let the statements list paint before anything tries to read it.

        Same race the dashboard and the details page already guard against:
        ``_goto`` returns on ``domcontentloaded`` while this route is still a
        bare navigation shell.  Field-measured 2026-08-18: immediately after
        the navigation the body held only the nav menu — no rows, no amounts,
        not even the payout text — and the record appeared about two seconds
        later.  Reading in that window is why the read-back reported NOT_FOUND
        for a disbursement that was plainly on the page.

        A timeout deliberately falls through: the structural checks in
        :meth:`_find_payment` stay the authority and NOT_FOUND remains the
        fail-closed answer.
        """

        wait = getattr(page, "wait_for_function", None)
        if not callable(wait):
            return
        selectors = {
            "rows": self.contract.payment_rows,
            "status": self.contract.payment_status,
            "alerts": self.contract.alerts,
            "signin": self.contract.signin_marker,
            "auth": self.contract.auth_marker,
        }
        try:
            await wait(
                """selectors => Boolean(
                    document.querySelector(selectors.rows)
                    || document.querySelector(selectors.status)
                    || document.querySelector(selectors.alerts)
                    || document.querySelector(selectors.signin)
                    || document.querySelector(selectors.auth)
                )""",
                selectors,
                timeout=min(self.navigation_timeout_ms, 15_000),
            )
        except Exception:
            pass

    async def _goto(self, page: Any, url: str) -> None:
        await self._respect_navigation_cooldown(page)
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=self.navigation_timeout_ms,
        )
        await self._settle(page)
        await self._paced_pause(page)
        try:
            self._last_business_navigation_at = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_business_navigation_at = None

    async def _respect_navigation_cooldown(self, page: Any) -> None:
        """Avoid burst navigation when walking several marketplaces."""

        if self._last_business_navigation_at is None:
            return
        wait = getattr(page, "wait_for_timeout", None)
        if not callable(wait):
            return
        now = asyncio.get_running_loop().time()
        minimum_gap_ms = 2_000
        remaining = minimum_gap_ms - int(
            (now - self._last_business_navigation_at) * 1_000
        )
        if remaining > 0:
            await wait(remaining)

    async def _wait_for_dashboard_contract(self, page: Any) -> None:
        wait = getattr(page, "wait_for_function", None)
        if not callable(wait):
            return
        selectors = {
            "rows": self.contract.balance_rows,
            "alerts": self.contract.alerts,
            "signin": self.contract.signin_marker,
            "auth": self.contract.auth_marker,
        }
        try:
            await wait(
                """selectors => Boolean(
                    document.querySelector(selectors.rows)
                    || document.querySelector(selectors.alerts)
                    || document.querySelector(selectors.signin)
                    || document.querySelector(selectors.auth)
                )""",
                selectors,
                timeout=min(self.navigation_timeout_ms, 15_000),
            )
        except Exception:
            # The structural checks immediately after this wait remain the
            # fail-closed authority and provide a more useful error message.
            pass

        # Amazon's empty-marketplace alert is frequently painted after the
        # page shell.  Give the live page one short extra settle window before
        # classifying it as a broken/empty DOM. Fixture pages remain instant.
        if (
            await page.locator(self.contract.balance_rows).count() < 1
            and await page.locator(self.contract.alerts).count() < 1
        ):
            try:
                await wait(
                    """selectors => Boolean(
                        document.querySelector(selectors.rows)
                        || document.querySelector(selectors.alerts)
                    )""",
                    selectors,
                    timeout=min(self.navigation_timeout_ms, 10_000),
                )
            except Exception:
                pass

    async def _wait_for_details_contract(
        self,
        page: Any,
        marketplace: MarketplaceRef,
    ) -> None:
        """Poll until the details DOM can actually satisfy the read contract.

        The URL commits to ``/payments/disburse/details`` before Amazon paints
        the row table and upgrades its ``kat-`` custom elements.  Every wait on
        the way here is URL-shaped and therefore returns the instant the route
        changes, so the reader below used to run against the previous document
        or an unhydrated shell and raise :class:`DomContractError` — which the
        caller then relabels as a human-Passkey requirement.  The dashboard
        already has :meth:`_wait_for_dashboard_contract` for exactly this; the
        details page never had its counterpart.

        This poll never clicks and never navigates.  On timeout it simply falls
        through so the structural checks in
        :meth:`_read_detected_payment_account` stay the fail-closed authority
        and report the precise mismatch.  The predicates below mirror those
        checks exactly, so "waited until satisfied" implies "the read will
        pass".  Amount *equality* is deliberately excluded: a genuine amount
        drift never converges, and waiting on it would only delay a correct
        rejection.
        """

        wait = getattr(page, "wait_for_timeout", None)
        if not callable(wait):
            return
        timeout_ms = min(self.navigation_timeout_ms, 15_000)
        elapsed_ms = 0
        while True:
            current_url = str(getattr(page, "url", "") or "")
            if not self._is_expected_details_url(current_url, marketplace):
                # Left the details route (for example an ``max_auth_age``
                # step-up bounce).  Waiting cannot fix that; let the caller's
                # existing checks classify it.
                logger.info(
                    "Details contract wait left the details route: "
                    "marketplace=%s elapsed_ms=%s",
                    marketplace.code,
                    elapsed_ms,
                    extra={"marketplace": marketplace.code},
                )
                return
            if await self._details_contract_ready(page):
                if elapsed_ms:
                    logger.info(
                        "Details contract ready: marketplace=%s elapsed_ms=%s",
                        marketplace.code,
                        elapsed_ms,
                        extra={"marketplace": marketplace.code},
                    )
                return
            if elapsed_ms >= timeout_ms:
                logger.warning(
                    "Details contract wait exhausted: marketplace=%s elapsed_ms=%s",
                    marketplace.code,
                    elapsed_ms,
                    extra={"marketplace": marketplace.code},
                )
                return
            pause_ms = min(250, timeout_ms - elapsed_ms)
            if pause_ms <= 0:
                return
            try:
                await wait(pause_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            elapsed_ms += pause_ms

    async def _describe_details_dom(self, page: Any) -> str:
        """Structural-only snapshot for the relabel log.  Never dumps content.

        The relabel that follows erases the real cause, and the three
        candidates it hides — a destroyed execution context, a document that
        is not the details page, and a button set that is not yet upgraded —
        are indistinguishable from the exception message alone.  Record which
        one it was using counts and booleans only: no amount, no account tail,
        no seller name and no page text ever reaches the log.

        Every probe is individually guarded.  Diagnostics must never raise on
        top of the failure they are describing.
        """

        parts: list[str] = []
        text: str | None = None
        try:
            text = _multiline_clean(
                await page.locator("body").inner_text(timeout=3_000)
            )
        except Exception as exc:
            parts.append(f"body_read_failed={type(exc).__name__}")
        if text is not None:
            parts.append(f"body_chars={len(text)}")
            parts.append(
                "amount_match="
                f"{bool(self.contract.details_amount_pattern.search(text))}"
            )
            parts.append(
                "account_match="
                f"{bool(self.contract.details_account_pattern.search(text))}"
            )
        try:
            parts.append(
                f"kat_buttons={await page.locator(self.contract.payout_buttons).count()}"
            )
        except Exception as exc:
            parts.append(f"button_scan_failed={type(exc).__name__}")
        try:
            matching = await self._matching_buttons(page)
            enabled = 0
            for button in matching:
                if await self._is_enabled(button):
                    enabled += 1
            parts.append(f"payout_buttons={len(matching)} enabled={enabled}")
        except Exception as exc:
            parts.append(f"payout_scan_failed={type(exc).__name__}")
        return " ".join(parts)

    async def _details_contract_ready(self, page: Any) -> bool:
        """Mirror the details-page read checks without raising. Never clicks.

        "Ready" means "the read below will reach a verdict", not "the payout
        can proceed".  A throttled page never satisfies the button predicate,
        so waiting on it burned the full timeout — 15 seconds per site, every
        run, to learn something the page already said in words.
        """

        body = await self._body_text(page)
        if self.contract.payout_rate_limited_pattern.search(body):
            return True
        if not self.contract.details_amount_pattern.search(body):
            return False
        account_match = self.contract.details_account_pattern.search(body)
        if not account_match:
            return False
        if not 2 <= len("".join(re.findall(r"\d", account_match.group(1)))) <= 8:
            return False
        enabled_count = 0
        for button in await self._matching_buttons(page):
            if await self._is_enabled(button):
                enabled_count += 1
        return enabled_count == 1

    async def _paced_pause(
        self,
        page: Any,
        *,
        minimum_ms: int | None = None,
        maximum_ms: int | None = None,
    ) -> None:
        """Add a bounded, non-bursting pause around real browser actions.

        Fixture DOMs intentionally have no ``wait_for_timeout`` method, so
        unit tests remain deterministic and fast.  A real Playwright page gets
        a modest pause; financial submission uses a longer range below.
        """

        wait = getattr(page, "wait_for_timeout", None)
        if not callable(wait):
            return
        low = self._pacing_range_ms[0] if minimum_ms is None else minimum_ms
        high = self._pacing_range_ms[1] if maximum_ms is None else maximum_ms
        milliseconds = int(self._pacing_random(float(low), float(high)))
        await wait(milliseconds)

    async def _settle(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass

    async def _wait_for_nonfinal_confirmation_transition(
        self,
        page: Any,
        marketplace: MarketplaceRef,
    ) -> None:
        """Wait briefly for the dashboard's one allowed non-final transition.

        ``click(no_wait_after=True)`` intentionally avoids Playwright guessing
        which navigation Amazon will use.  Seller Central may keep the current
        dashboard URL for about a second and only then navigate the same Page
        either to the exact disbursement-details route or to its supported
        sign-in/MFA route.  Calling :meth:`_raise_if_auth` before that redirect
        makes the still-visible dashboard look like a native challenge and
        prevents the assisted-login advancer from seeing the Passkey page.

        The pre-click one-shot marker is already set by the caller, so this
        bounded wait never authorizes another dashboard click.  A timeout
        simply falls through to the existing fail-closed ``WAITING_AUTH`` path.
        """

        timeout_ms = min(self.navigation_timeout_ms, 3_000)
        elapsed_ms = 0
        while True:
            current_url = str(getattr(page, "url", "") or "")
            if self._is_expected_details_url(current_url, marketplace) or (
                AmazonLoginAdvancer.is_supported_login_url(
                    current_url,
                    expected_host=marketplace.domain,
                )
            ):
                return
            if elapsed_ms >= timeout_ms:
                return
            wait = getattr(page, "wait_for_timeout", None)
            if not callable(wait):
                return
            pause_ms = min(250, timeout_ms - elapsed_ms)
            if pause_ms <= 0:
                return
            await wait(pause_ms)
            elapsed_ms += pause_ms

    async def _wait_for_post_auth_payment_details(
        self,
        page: Any,
        marketplace: MarketplaceRef,
    ) -> Any:
        """Wait for Amazon's delayed business-to-details handoff without clicking.

        After OTP/Passkey succeeds, Seller Central can first commit the
        Payments dashboard and only a few seconds later resume the original
        non-final request on ``/payments/disburse/details``.  Publishing an
        auth lease during that short gap incorrectly asks the operator to
        press Continue even though authentication already succeeded.

        This bounded poll never navigates or clicks.  It may wait on an
        intermediate HTTPS route only while the hostname remains the exact
        marketplace host; success still requires the one exact details path.
        Wrong hosts fall through immediately to the existing fail-closed
        HumanAuthRequired path.
        """

        page = self.resolved_page_for(page)
        timeout_ms = min(self.navigation_timeout_ms, 30_000)
        elapsed_ms = 0
        logger.info(
            "Payment details handoff wait started: marketplace=%s timeout_ms=%s",
            marketplace.code,
            timeout_ms,
        )
        while True:
            page = self.resolved_page_for(page)
            current_url = str(getattr(page, "url", "") or "")
            if self._is_expected_details_url(current_url, marketplace):
                logger.info(
                    "Payment details handoff completed: marketplace=%s elapsed_ms=%s",
                    marketplace.code,
                    elapsed_ms,
                )
                return page
            if AmazonLoginAdvancer.is_supported_login_url(
                current_url,
                expected_host=marketplace.domain,
            ):
                # A sign-in route on the marketplace host satisfies the
                # same-host test below, so without this branch an
                # ``max_auth_age`` step-up bounce would spin here for the full
                # timeout while neither the advancer nor _raise_if_auth ever
                # runs.  Hand it straight back to the caller instead.
                logger.info(
                    "Payment details handoff returned to sign-in: marketplace=%s "
                    "elapsed_ms=%s",
                    marketplace.code,
                    elapsed_ms,
                    extra={"marketplace": marketplace.code},
                )
                return page
            try:
                parsed = urlparse(current_url)
                same_host_https = (
                    parsed.scheme.lower() == "https"
                    and (parsed.hostname or "").lower().rstrip(".")
                    == marketplace.domain.lower().rstrip(".")
                )
                pending_blank = current_url.lower() in {"", "about:blank"}
            except Exception:
                same_host_https = False
                pending_blank = False
            if not (same_host_https or pending_blank):
                logger.info(
                    "Payment details handoff stopped outside expected host: marketplace=%s",
                    marketplace.code,
                )
                return page
            if elapsed_ms >= timeout_ms:
                logger.info(
                    "Payment details handoff wait exhausted: marketplace=%s elapsed_ms=%s",
                    marketplace.code,
                    elapsed_ms,
                )
                return page
            wait = getattr(page, "wait_for_timeout", None)
            if not callable(wait):
                logger.info(
                    "Payment details handoff wait unavailable: marketplace=%s",
                    marketplace.code,
                )
                return page
            pause_ms = min(250, timeout_ms - elapsed_ms)
            if pause_ms <= 0:
                return page
            try:
                await wait(pause_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.info(
                    "Payment details handoff wait interrupted: marketplace=%s",
                    marketplace.code,
                )
                return page
            elapsed_ms += pause_ms

    @staticmethod
    def _is_expected_details_url(
        url: str,
        marketplace: MarketplaceRef,
    ) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        return (
            parsed.scheme.lower() == "https"
            and (parsed.hostname or "").lower().rstrip(".")
            == marketplace.domain.lower().rstrip(".")
            and parsed.path.lower() == "/payments/disburse/details"
            and not parsed.params
        )

    def _require_host(self, page: Any, marketplace: MarketplaceRef) -> None:
        observed = _host(str(page.url))
        if observed != marketplace.domain.lower():
            raise PreflightRejected(
                f"域名不匹配：预期 {marketplace.domain}，实际 {observed or '空'}"
            )

    async def _read_identity(self, page: Any, expected: str) -> tuple[str, str]:
        expected_normal = _identity_normal(expected)
        machine, visible = await self._identity_candidates(page)
        observed = [*machine, *visible]

        unique = {
            (_identity_normal(value), source): value
            for value, source in observed
            if _identity_normal(value)
        }
        matches = [
            (original, source)
            for (normal, source), original in unique.items()
            if normal == expected_normal
        ]
        if not matches:
            safe_sources = sorted({source for _, source in observed})
            raise PreflightRejected(
                "页面未能唯一证明建档卖家身份"
                + (f"（探测来源：{', '.join(safe_sources)}）" if safe_sources else "")
            )
        identities = {_identity_normal(value) for value, _ in matches}
        if len(identities) != 1:
            raise PreflightRejected("页面卖家身份探测结果不唯一")
        # Prefer machine ID over visible display name when both match.
        matches.sort(key=lambda item: item[1].endswith("visible_account"))
        return expected, matches[0][1]

    async def _verify_identity(
        self,
        page: Any,
        expected: str,
        marketplace: MarketplaceRef,
    ) -> tuple[str, str]:
        """Verify identity, reusing a proof from this live page session.

        Current Seller Central Payments pages sometimes omit all account
        identity attributes.  On the first such page for a site we obtain a
        read-only proof from the account-switcher page; later navigation in
        the same Playwright page/site may reuse that proof, but a different
        page object, host or expected identity must prove itself again.
        """

        expected_normal = _identity_normal(expected)
        cache_key = marketplace.domain.casefold()
        identity_cache = self._page_verified_identities(page)
        if identity_cache.get(cache_key) == expected_normal:
            return expected, "session:account_switcher"
        try:
            identity, source = await self._read_identity(page, expected)
        except PreflightRejected:
            identity, source = await self._detect_identity_from_account_switcher(
                page, marketplace
            )
            if _identity_normal(identity) != expected_normal:
                raise PreflightRejected("账户切换页卖家身份与建档值不一致")
            # The calling method restores its exact dashboard/statements URL
            # before reading any business data; caching records proof only.
        identity_cache[cache_key] = expected_normal
        return expected, source

    def _page_consumed_marketplaces(self, page: Any) -> set[str]:
        """Return the marketplace codes already submitted on this Page."""

        try:
            current = self._consumed_marketplaces.get(page)
            if current is None:
                current = set()
                self._consumed_marketplaces[page] = current
            return current
        except TypeError:
            # Some lightweight fixtures cannot be weak-referenced.  Keeping the
            # set directly on that fixture preserves the same object-lifetime
            # semantics without falling back to a reusable numeric id.
            attribute = "_ziniao_consumed_marketplaces"
            current = getattr(page, attribute, None)
            if not isinstance(current, set):
                current = set()
                setattr(page, attribute, current)
            return current

    def _page_verified_identities(self, page: Any) -> dict[str, str]:
        """Return seller proofs bound to one Page object's lifetime."""

        try:
            current = self._verified_identities.get(page)
            if current is None:
                current = {}
                self._verified_identities[page] = current
            return current
        except TypeError:
            # Some lightweight fixtures cannot be weak-referenced.  Keeping the
            # cache directly on that fixture preserves the same object-lifetime
            # semantics without falling back to a reusable numeric id.
            attribute = "_ziniao_verified_seller_identities"
            current = getattr(page, attribute, None)
            if not isinstance(current, dict):
                current = {}
                setattr(page, attribute, current)
            return current

    def _clear_page_verified_identities(self, page: Any) -> None:
        try:
            self._verified_identities.pop(page, None)
            return
        except TypeError:
            pass
        current = getattr(page, "_ziniao_verified_seller_identities", None)
        if isinstance(current, dict):
            current.clear()

    async def _detect_identity(self, page: Any) -> tuple[str, str]:
        machine, visible = await self._identity_candidates(page)
        # Machine IDs and display names naturally differ.  Prefer a unique
        # machine ID, and only fall back to a unique visible account name when
        # the page exposes no machine identity at all.
        for candidates, kind in ((machine, "machine"), (visible, "visible")):
            identities: dict[str, list[tuple[str, str]]] = {}
            for value, source in candidates:
                normal = _identity_normal(value)
                if normal:
                    identities.setdefault(normal, []).append((value, source))
            if not identities:
                continue
            if len(identities) != 1:
                raise PreflightRejected(f"页面探测到多个不同的{kind}卖家身份")
            values = next(iter(identities.values()))
            return values[0]
        raise PreflightRejected("Seller Central 页面没有显示可核实的卖家身份")

    async def _detect_identity_from_account_switcher(
        self, page: Any, marketplace: MarketplaceRef
    ) -> tuple[str, str]:
        await self._goto(
            page,
            f"https://{marketplace.domain}/account-switcher/default/merchantMarketplace",
        )
        page = await self._raise_if_auth(page, marketplace)
        self._require_host(page, marketplace)

        # Account data is loaded asynchronously after DOMContentLoaded.
        # Wait for either a current-account suffix or the loading marker to
        # disappear, bounded by the adapter's normal navigation timeout.
        try:
            await page.wait_for_function(
                r"""() => {
                    const body = (document.body && document.body.innerText) || '';
                    const buttons = Array.from(document.querySelectorAll('button'));
                    return buttons.some(button => /[（(]\s*(?:当前|current)\s*[）)]/i.test((button.innerText || '').trim()))
                        || !/(?:正在加载账户|loading accounts)/i.test(body);
                }""",
                timeout=min(self.navigation_timeout_ms, 15_000),
            )
        except Exception:
            pass

        buttons = page.locator("button")
        accounts: dict[str, str] = {}
        current_suffix = re.compile(
            r"\s*[（(]\s*(?:当前|current)\s*[）)]\s*$", re.IGNORECASE
        )
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            text = _clean(await self._element_text(button))
            if not current_suffix.search(text):
                continue
            # The active marketplace row also carries a localized "current"
            # suffix, but its selected-state class identifies it as a market,
            # not the seller account row.
            selected_class = _clean(
                (await button.get_attribute("class")) or ""
            ).casefold()
            if "account-selected" in selected_class:
                continue
            value = current_suffix.sub("", text).strip()
            normal = _identity_normal(value)
            if value and normal:
                accounts[normal] = value
        if len(accounts) == 1:
            return next(iter(accounts.values())), "account_switcher:current"
        if len(accounts) > 1:
            raise PreflightRejected("账户切换页显示多个当前卖家账号")
        raise PreflightRejected("账户切换页没有显示唯一的当前卖家账号")

    async def _identity_candidates(
        self, page: Any
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        machine: list[tuple[str, str]] = []
        visible: list[tuple[str, str]] = []

        # mons_sel_mkid is a marketplace ID, not a merchant/seller identity.
        parsed = urlparse(str(page.url))
        for key, values in parse_qs(parsed.query).items():
            if key.lower() in {"merchantid", "sellerid", "mons_sel_dir_mcid"}:
                machine.extend((str(value), f"url:{key}") for value in values if value)

        candidates = page.locator(self.contract.identity_candidates)
        for index in range(await candidates.count()):
            element = candidates.nth(index)
            for attribute in ("content", "data-merchant-id", "data-seller-id"):
                try:
                    value = _clean(await element.get_attribute(attribute) or "")
                except Exception:
                    value = ""
                if value:
                    machine.append((value, f"dom:{attribute}"))
            for attribute in ("label", "value"):
                try:
                    value = _clean(await element.get_attribute(attribute) or "")
                except Exception:
                    value = ""
                if value:
                    visible.append((value.split("|")[0].strip(), f"dom:{attribute}"))
            try:
                text = _clean(await element.inner_text())
            except Exception:
                text = ""
            if text:
                visible.append((text.split("|")[0].strip(), "dom:visible_account"))
        return machine, visible

    async def _raise_if_auth(
        self,
        page: Any,
        marketplace: MarketplaceRef | None = None,
        *,
        allow_payment_details_handoff: bool = False,
    ) -> Any:
        source_page = page
        assisted_login_advanced = False
        url = str(getattr(page, "url", "") or "").lower()
        if "/ap/signin" in url or "/ap/mfa" in url:
            resolved_page = await self._login_advancer.advance_or_raise(
                page,
                expected_host=marketplace.domain if marketplace is not None else None,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            if resolved_page is None:
                raise HumanAuthRequired(
                    "Amazon 登录或资金认证待人工完成", kind="sign_in"
                )
            page = resolved_page
            assisted_login_advanced = True
            if page is not source_page:
                self._remember_page_alias(source_page, page)
                self._transfer_page_state(source_page, page)
            # Successful assisted progression stays in this invocation.  The
            # checks below inspect the newly-rendered destination before any
            # business-page host/identity validation is allowed to continue.
        if assisted_login_advanced:
            await self._wait_for_assisted_login_dom_handoff(page)
        if await page.locator(self.contract.signin_marker).count() > 0:
            raise HumanAuthRequired("Amazon 登录已失效", kind="sign_in")
        if await page.locator(self.contract.auth_marker).count() > 0:
            raise HumanAuthRequired("页面需要人工安全验证", kind="challenge")
        body = await self._body_text(page)
        if self.contract.auth_text_pattern.search(body[:100_000]):
            raise HumanAuthRequired("页面出现人工验证提示", kind="challenge")
        return page

    async def _wait_for_assisted_login_dom_handoff(self, page: Any) -> None:
        """Let a committed business URL finish replacing the old auth DOM.

        Amazon can update Playwright's ``Page.url`` to the Payments route about
        one second before the old OTP form and verification text disappear.
        The login advancer correctly stops clicking as soon as that exact
        business URL is committed, but immediately classifying the still-old
        DOM here would incorrectly publish ``WAITING_AUTH`` after a successful
        login.  Poll only while the same auth surface remains, for at most
        three seconds.  The normal checks below remain authoritative: a real
        CAPTCHA/OTP/Passkey surface that survives the bound is still rejected.
        """

        await self._settle(page)
        wait = getattr(page, "wait_for_timeout", None)
        if not callable(wait):
            return

        timeout_ms = min(self.navigation_timeout_ms, 3_000)
        elapsed_ms = 0
        while await self._has_auth_surface(page) and elapsed_ms < timeout_ms:
            pause_ms = min(250, timeout_ms - elapsed_ms)
            if pause_ms <= 0:
                break
            await wait(pause_ms)
            elapsed_ms += pause_ms

    async def _has_auth_surface(self, page: Any) -> bool:
        if await page.locator(self.contract.signin_marker).count() > 0:
            return True
        if await page.locator(self.contract.auth_marker).count() > 0:
            return True
        body = await self._body_text(page)
        return bool(self.contract.auth_text_pattern.search(body[:100_000]))

    def resolved_page_for(self, page: Any) -> Any:
        """Return the newest verified same-context Page alias for this adapter."""

        current = page
        seen: set[int] = set()
        for _ in range(8):
            if id(current) in seen:
                break
            seen.add(id(current))
            try:
                successor = self._page_aliases.get(current)
            except TypeError:
                successor = getattr(current, "_ziniao_business_page_alias", None)
            if successor is None or successor is current:
                break
            current = successor
        return current

    def _remember_page_alias(self, source_page: Any, target_page: Any) -> None:
        try:
            self._page_aliases[source_page] = target_page
        except TypeError:
            setattr(source_page, "_ziniao_business_page_alias", target_page)

    def _transfer_page_state(self, source_page: Any, target_page: Any) -> None:
        """Preserve page-scoped duplicate guards across one verified tab handoff."""

        source_id = id(source_page)
        target_id = id(target_page)
        if source_id == target_id:
            return
        # The adopted tab must inherit every site already submitted here, or the
        # handoff would silently hand out a second irreversible click.
        source_consumed = self._page_consumed_marketplaces(source_page)
        if source_consumed:
            self._page_consumed_marketplaces(target_page).update(source_consumed)
            source_consumed.clear()

        for source_key in tuple(self._confirmation_dispatched):
            page_id, marketplace_code = source_key
            if page_id == source_id:
                self._confirmation_dispatched.add((target_id, marketplace_code))
                self._confirmation_dispatched.discard(source_key)

        # The retry budget must travel with the dispatch marker it bounds,
        # otherwise a verified tab handoff would silently hand out a second
        # re-dispatch.
        for source_key in tuple(self._nonfinal_retry_used):
            page_id, marketplace_code = source_key
            if page_id == source_id:
                self._nonfinal_retry_used.add((target_id, marketplace_code))
                self._nonfinal_retry_used.discard(source_key)

        # A newly adopted Page must prove the seller identity itself.  Moving
        # this cache would contradict _verify_identity's page-bound guarantee
        # and could let a different tab inherit the source tab's seller proof.
        self._clear_page_verified_identities(source_page)

    async def _no_data_message(self, page: Any) -> str | None:
        messages: list[str] = []
        alerts = page.locator(self.contract.alerts)
        for index in range(await alerts.count()):
            alert = alerts.nth(index)
            title = await self._element_text(alert)
            description = _clean((await alert.get_attribute("description")) or "")
            combined = _clean(f"{title} {description}")
            if self.contract.no_data_pattern.search(combined):
                messages.append(combined)
        if not messages:
            return None
        payout_count = len(await self._matching_buttons(page))
        if payout_count != 0:
            raise DomContractError("官方无付款数据提示与请求付款按钮同时存在")
        preferred = next(
            (
                message
                for message in messages
                if self.contract.no_data_description_pattern.search(message)
            ),
            messages[0],
        )
        return preferred

    async def _classified_rows(self, page: Any) -> list[tuple[str, Any]]:
        columnar = await self._columnar_balance_rows(page)
        if columnar is not None:
            return columnar
        rows = page.locator(self.contract.balance_rows)
        classified: list[tuple[str, Any]] = []
        for index in range(await rows.count()):
            row = rows.nth(index)
            try:
                text = await self._row_text(row)
            except (AssertionError, AttributeError):
                labels = row.locator(self.contract.balance_label)
                text = (
                    await self._element_text(labels.first)
                    if await labels.count() == 1
                    else ""
                )
            if self.contract.payable_pattern.search(text):
                kind = "PAYABLE"
            elif self.contract.deferred_pattern.search(text):
                kind = "DEFERRED"
            elif self.contract.all_accounts_pattern.search(text):
                kind = "ALL"
            else:
                kind = "OTHER"
            classified.append((kind, row))
        return classified

    async def _columnar_balance_rows(
        self, page: Any
    ) -> list[tuple[str, _CompositeBalanceRow]] | None:
        """Assemble the live Payments layout's three parallel column cards.

        Amazon renders account type, total balance, and available balance as
        separate cards whose three direct rows align by index.  Treating every
        DOM row independently duplicates PAYABLE and disconnects labels from
        amounts/buttons.  This method accepts only the exact 3x3 structure;
        any partial/ambiguous card layout fails closed instead of cross-joining
        unrelated rows such as "recent payouts".
        """

        try:
            account_cards = page.locator(
                "kat-card.linkable-multi-row-card--account-groups"
            )
            cards = page.locator("kat-card.linkable-multi-row-card")
        except (AssertionError, AttributeError):
            return None
        account_count = await account_cards.count()
        generic_count = await cards.count()
        if account_count == 0 and generic_count == 0:
            return None
        if account_count != 1:
            raise DomContractError("账户类型余额卡不唯一")

        total_cards: list[Any] = []
        available_cards: list[Any] = []
        for index in range(generic_count):
            card = cards.nth(index)
            header = card.locator(".linkable-multi-row-card-header")
            header_text = await self._element_text(header)
            if re.search(r"(?:总余额|Total\s+balance)", header_text, re.IGNORECASE):
                total_cards.append(card)
            elif re.search(
                r"(?:可用资金|Available\s+(?:funds?|balance))",
                header_text,
                re.IGNORECASE,
            ):
                available_cards.append(card)
        if len(total_cards) != 1 or len(available_cards) != 1:
            raise DomContractError("总余额卡或可用资金卡不唯一")

        direct = ".linkable-multi-row-card-rows-container > .linkable-multi-row-card-row"
        label_rows = account_cards.nth(0).locator(direct)
        total_rows = total_cards[0].locator(direct)
        available_rows = available_cards[0].locator(direct)
        counts = (
            await label_rows.count(),
            await total_rows.count(),
            await available_rows.count(),
        )
        if counts != (3, 3, 3):
            raise DomContractError(
                f"余额列卡行数必须全部为 3，实际为 {counts}"
            )

        result: list[tuple[str, _CompositeBalanceRow]] = []
        for index in range(3):
            label = label_rows.nth(index)
            text = await self._row_text(label)
            if self.contract.payable_pattern.search(text):
                kind = "PAYABLE"
            elif self.contract.deferred_pattern.search(text):
                kind = "DEFERRED"
            elif self.contract.all_accounts_pattern.search(text):
                kind = "ALL"
            else:
                kind = "OTHER"
            result.append(
                (
                    kind,
                    _CompositeBalanceRow(
                        label, total_rows.nth(index), available_rows.nth(index)
                    ),
                )
            )
        if [kind for kind, _ in result] != ["PAYABLE", "DEFERRED", "ALL"]:
            raise DomContractError("余额列卡账户类型顺序或标签不符合契约")
        return result

    async def _unique_payable_row(self, page: Any) -> Any:
        matches = [row for kind, row in await self._classified_rows(page) if kind == "PAYABLE"]
        if len(matches) != 1:
            raise DomContractError(
                f"严格匹配到 {len(matches)} 个标准订单行，要求恰好 1 个"
            )
        return matches[0]

    async def _payout_buttons(self, container: Any) -> list[Any]:
        buttons = container.locator(self.contract.payout_buttons)
        result: list[Any] = []
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            label = await self._element_text(button)
            if self.contract.payout_button_pattern.search(label):
                result.append(button)
        return result

    async def _matching_buttons(self, page: Any) -> list[Any]:
        return await self._payout_buttons(page)

    async def _is_enabled(self, element: Any) -> bool:
        disabled = await element.get_attribute("disabled")
        aria = await element.get_attribute("aria-disabled")
        if disabled is not None or str(aria).lower() == "true":
            return False
        try:
            return bool(await element.is_enabled())
        except Exception:
            return True

    async def _row_text(self, row: Any) -> str:
        if isinstance(row, _CompositeBalanceRow):
            return _clean(
                " ".join(
                    [
                        await self._element_text(row.label),
                        await self._element_text(row.total),
                        await self._element_text(row.available),
                    ]
                )
            )
        visible = await self._element_text(row)
        attributes: list[str] = []
        labelled = row.locator("[label], kat-alert[description]")
        for index in range(await labelled.count()):
            element = labelled.nth(index)
            for attribute in ("label", "description", "header"):
                value = await element.get_attribute(attribute)
                if value:
                    attributes.append(value)
        return _clean(" ".join([visible, *attributes]))

    async def _element_text(self, element: Any) -> str:
        try:
            text = _clean(await element.inner_text())
        except Exception:
            text = ""
        if text:
            return text
        for attribute in ("label", "description", "header", "value", "content"):
            try:
                value = _clean((await element.get_attribute(attribute)) or "")
            except Exception:
                value = ""
            if value:
                return value
        return ""

    async def _amounts(self, row: Any, currency: str) -> list[Decimal]:
        if isinstance(row, _CompositeBalanceRow):
            values: list[Decimal] = []
            for cell in (row.total, row.available):
                source = await self._row_text(cell)
                cell_values: list[Decimal] = []
                for token in _MONEY_TOKEN.findall(source):
                    upper = token.upper()
                    if not any(
                        mark in upper
                        for mark in (currency.upper(), "$", "£", "€")
                    ):
                        continue
                    try:
                        cell_values.append(parse_amount(token))
                    except DomContractError:
                        continue
                if len(cell_values) != 1:
                    raise DomContractError(
                        "余额列卡的单元格原币金额不是唯一一个"
                    )
                values.append(cell_values[0])
            return values
        # Preserve actual DOM order.  Reading row visible text first and then
        # appending kat-link[label] would move the hidden total behind the
        # visible available amount and swap the two columns.
        elements = row.locator("*")
        tokens: list[str] = []
        for index in range(await elements.count()):
            element = elements.nth(index)
            try:
                direct = await element.evaluate(
                    "el => Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' ')"
                )
            except Exception:
                direct = await self._element_text(element)
            text = _clean(direct or "")
            label = _clean((await element.get_attribute("label")) or "")
            source = text or label
            tokens.extend(_MONEY_TOKEN.findall(source))
        if not tokens:
            tokens = _MONEY_TOKEN.findall(await self._row_text(row))
        values: list[Decimal] = []
        for token in tokens:
            upper = token.upper()
            # Avoid consuming unrelated bare dates/IDs unless the token has a
            # currency marker or the row has no explicit currency markers.
            if not any(mark in upper for mark in (currency.upper(), "$", "£", "€")):
                continue
            try:
                values.append(parse_amount(token))
            except DomContractError:
                continue
        return values

    async def _total_amount(self, row: Any, currency: str) -> Decimal:
        values = await self._amounts(row, currency)
        if not values:
            raise DomContractError("余额行未读到原币金额")
        return values[0]

    async def _available_amount(self, row: Any, currency: str) -> Decimal:
        values = await self._amounts(row, currency)
        if not values:
            raise DomContractError("余额行未读到可用资金")
        # Real multi-row row order is total balance then available funds.
        return values[1] if len(values) >= 2 else values[0]

    async def _dashboard_period(self, page: Any, payable_row: Any) -> str:
        row_text = await self._row_text(payable_row)
        match = self.contract.settlement_pattern.search(row_text)
        if match:
            return match.group(1)
        body = await self._body_text(page)
        # Dashboard commonly exposes a date range rather than numeric ID.  It
        # is stable enough to form the pre-click intent and later statements
        # matching relies on amount + status when the numeric ID is not known.
        ranges = re.findall(
            r"(?:\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*[–—-]\s*(?:\d{4}[/-]\d{1,2}[/-]\d{1,2}|至今|Present|Now)",
            body,
            flags=re.IGNORECASE,
        )
        if ranges:
            return _clean(ranges[0])
        return "OPEN:" + stable_hash(
            {
                "url": str(page.url),
                "row": row_text,
            }
        )[:20]

    async def _verify_details(
        self,
        page: Any,
        marketplace: MarketplaceRef,
        expected: MarketplaceSnapshot,
    ) -> MarketplaceSnapshot:
        # Same race as the setup probe: the route commits to the details page
        # before Amazon paints it, and every wait leading here is URL-shaped.
        # The checks below read exactly what _wait_for_details_contract polls
        # for, so waiting first turns a paint race into a correct read without
        # relaxing a single assertion — a genuine mismatch still raises.
        await self._wait_for_details_contract(page, marketplace)
        body = await self._body_text(page)
        amount_match = self.contract.details_amount_pattern.search(body)
        if not amount_match:
            raise DomContractError("确认页找不到当前结算金额")
        # The confirmation page's figure is authoritative and is returned to
        # the caller below.  It is deliberately NOT compared with the planned
        # amount: the balance keeps accruing between reading the dashboard and
        # opening this page, and Amazon states here that the transferred amount
        # may differ from the displayed balance.  Requiring equality made a
        # normal change abort the payout.  What the money depends on — seller,
        # host, payout account, the single enabled final control — is still
        # verified, above and below.
        amount = parse_amount(amount_match.group(1))
        # Read the destination, never judge it.  Amazon owns this value; this
        # automation cannot select or change it, so comparing it against a
        # locally stored baseline could only ever refuse a payout, never
        # redirect one — and in practice it refused three legitimate ones and
        # caught nothing.  The tail is carried out to the money record instead,
        # so "where did that money go" stays answerable.
        account_match = self.contract.details_account_pattern.search(body)
        observed_account = (
            "".join(re.findall(r"\d", account_match.group(1)))[-8:]
            if account_match
            else ""
        )
        buttons = await self._matching_buttons(page)
        enabled_count = sum([1 for button in buttons if await self._is_enabled(button)])
        if enabled_count != 1:
            # Amazon caps on-demand disbursement at once per rolling 24 hours
            # and only says so here — the dashboard's button was enabled, so
            # nothing earlier could have predicted this.  Reported as a broken
            # page contract it looked like a failure and cost the site its run;
            # it is an ordinary "come back later".  Checked only on the branch
            # that was already going to raise, so a page offering a usable
            # button is never affected by this text.
            rate_limited = self.contract.payout_rate_limited_pattern.search(body)
            if rate_limited:
                retry_after = self.contract.payout_retry_after_pattern.search(body)
                waiting = (
                    _localised_duration(retry_after.group(1)) if retry_after else ""
                )
                raise PayoutRateLimited(
                    "亚马逊限制该账户 24 小时内仅可请求一次提现"
                    + (f"，约 {waiting} 后可再次请求" if waiting else ""),
                    retry_after=waiting or None,
                )
            raise DomContractError("确认页最终请求付款按钮不是唯一一个可用按钮")
        return MarketplaceSnapshot(
            marketplace_code=expected.marketplace_code,
            domain=expected.domain,
            seller_id=expected.seller_id,
            payment_account=observed_account,
            currency=expected.currency,
            payable_amount=amount,
            delayed_amount=expected.delayed_amount,
            settlement_key=expected.settlement_key,
            can_submit=True,
            contract_version=expected.contract_version,
            page_fingerprint=expected.page_fingerprint,
            identity_source=expected.identity_source,
        )

    async def _find_payment(
        self,
        page: Any,
        expected: MarketplaceSnapshot,
        *,
        require_today: bool,
    ) -> ReconcileResult:
        rows = page.locator(self.contract.payment_rows)
        if await rows.count() == 0:
            # Fallback: cards on some locales have no stable class, but each
            # status indicator belongs to one closest bordered/card ancestor.
            indicators = page.locator(self.contract.payment_status)
            candidates: list[Any] = []
            for index in range(await indicators.count()):
                candidate = indicators.nth(index).locator(
                    "xpath=ancestor::*[contains(@class,'card') or contains(@class,'row')][1]"
                )
                if await candidate.count() == 1:
                    candidates.append(candidate.first)
        else:
            candidates = [rows.nth(index) for index in range(await rows.count())]

        amount_matches: list[ReconcileResult] = []
        for row in candidates:
            text = await self._row_text(row)
            amount_match = self.contract.payout_amount_pattern.search(text)
            if not amount_match:
                continue
            try:
                amount = parse_amount(amount_match.group(1))
            except DomContractError:
                continue
            if amount != expected.payable_amount:
                continue
            status_locator = row.locator(self.contract.payment_status)
            status = ""
            if await status_locator.count() == 1:
                status = await self._element_text(status_locator.first)
            if not status:
                status_match = self.contract.payout_status_pattern.search(text)
                status = _clean(status_match.group(1)) if status_match else ""
            settlement_match = self.contract.settlement_pattern.search(text)
            settlement = settlement_match.group(1) if settlement_match else None
            period_end = _period_end(text)

            # Numeric known IDs must match exactly.  Dashboard OPEN/date keys do
            # not yet know Amazon's eventual numeric statement ID.
            if (
                _is_numeric_settlement(expected.settlement_key)
                and settlement != expected.settlement_key
            ):
                continue
            if require_today:
                # Before submission, only a record whose period ends today is
                # the authoritative same-day duplicate.  This prevents an old
                # historical payment with the same amount from blocking forever.
                if period_end != self._today():
                    continue
            details = {
                "amount": str(amount),
                "settlement": settlement,
                "period_end": period_end.isoformat() if period_end else None,
            }
            if any(token in status.lower() for token in self.contract.confirmed_statuses):
                amount_matches.append(
                    ReconcileResult(
                        ReconcileStatus.CONFIRMED,
                        utc_now(),
                        platform_reference=settlement,
                        platform_status=status,
                        details=details,
                    )
                )
            else:
                amount_matches.append(
                    ReconcileResult(
                        ReconcileStatus.PENDING,
                        utc_now(),
                        platform_reference=settlement,
                        platform_status=status or None,
                        details=details,
                    )
                )

        confirmed = [item for item in amount_matches if item.status is ReconcileStatus.CONFIRMED]
        if len(confirmed) == 1:
            return confirmed[0]
        if len(confirmed) > 1:
            return ReconcileResult(
                ReconcileStatus.CONFLICT,
                utc_now(),
                details={"reason": "多条付款记录金额和状态同时匹配"},
            )
        if len(amount_matches) == 1:
            return amount_matches[0]
        if len(amount_matches) > 1:
            return ReconcileResult(
                ReconcileStatus.CONFLICT,
                utc_now(),
                details={"reason": "多条付款记录金额同时匹配"},
            )
        return ReconcileResult(ReconcileStatus.NOT_FOUND, utc_now())

    async def _body_text(self, page: Any) -> str:
        try:
            return _multiline_clean(
                await page.locator("body").inner_text(timeout=3_000)
            )
        except Exception:
            return ""

    async def _fingerprint(self, row: Any) -> str:
        try:
            html = await row.evaluate("element => element.outerHTML")
        except Exception as exc:
            raise DomContractError("标准订单行无法生成结构指纹") from exc
        # Ignore live money/date values; hash tags, classes and KAT labels.
        structure = re.sub(r">[^<]+<", "><", html)
        structure = re.sub(r"\d+(?:[.,]\d+)*", "#", structure)
        return hashlib.sha256(structure.encode("utf-8")).hexdigest()

def parse_amount(text: str) -> Decimal:
    """Parse common CA/UK/AU money strings without binary floats."""
    match = _AMOUNT.search(_clean(text).replace("\u00a0", " "))
    if not match:
        raise DomContractError(f"金额格式无法识别：{text[:80]}")
    raw = match.group("number").strip().replace(" ", "")
    raw = raw.rstrip(".,")
    if "," in raw and "." in raw:
        if raw.rfind(".") > raw.rfind(","):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        tail = raw.rsplit(",", 1)[-1]
        raw = raw.replace(",", "." if len(tail) in (1, 2) else "")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise DomContractError(f"金额格式无法识别：{text[:80]}") from exc
    if match.group("prefix_sign") or match.group("suffix_sign"):
        value = -value
    return value.quantize(Decimal("0.01"))


def _localised_duration(value: str) -> str:
    """Render Amazon's ``06 hrs 47 mins`` as ``6 小时 47 分钟``.

    The countdown ends up in a Feishu card read by operators who do not read
    English, and a leading zero on an hour count reads like a code.
    """

    text = " ".join(str(value or "").split())
    parts: list[str] = []
    for amount, unit in re.findall(
        r"(\d+)\s*(hrs?|hours?|小时|mins?|minutes?|分钟)", text, flags=re.IGNORECASE
    ):
        suffix = (
            "小时"
            if unit.lower().startswith(("hr", "hour")) or unit == "小时"
            else "分钟"
        )
        parts.append(f"{int(amount)} {suffix}")
    return " ".join(parts) if parts else text


def _identity_normal(value: str) -> str:
    return " ".join(str(value).split()).strip().casefold()


def _is_numeric_settlement(value: str) -> bool:
    return bool(re.fullmatch(r"\d{6,}", value or ""))


def _period_end(text: str) -> date | None:
    match = re.search(
        r"\b(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*[–—-]\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})\b",
        text,
    )
    if not match:
        return None
    raw = match.group(2).replace("/", "-")
    try:
        year, month, day = (int(part) for part in raw.split("-"))
        return date(year, month, day)
    except (TypeError, ValueError):
        return None


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _clean(value: str) -> str:
    return " ".join(str(value).split()).strip()


def _multiline_clean(value: str) -> str:
    return "\n".join(
        line for line in (_clean(item) for item in str(value).splitlines()) if line
    )
