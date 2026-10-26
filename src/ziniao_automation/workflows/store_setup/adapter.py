"""Read-only onboarding layered on existing navigation/login primitives.

This adapter deliberately has no preflight/plan/submit interface. The financial
adapter and its cached identity/DOM gates are not changed by onboarding.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..amazon_login import AmazonLoginAdvancer
from ..amazon_account_switcher import AmazonAccountSwitcher, MARKETPLACE_NAMES
from ..amazon_switcher_contract import VERSION as SWITCHER_CONTRACT_VERSION, read_switcher_snapshot
from ..amazon_disbursement.config import ALLOWED_MARKETPLACES
from ..amazon_inventory_gpsr.config import INVENTORY_MARKETPLACES
from ..amazon_disbursement.page import AmazonPaymentsPage, _identity_normal
from ..setup_diagnostics import classify_switch_landing
from ..errors import HumanAuthRequired
from ..types import MarketplaceRef, StoreRef
from .identity import (
    ADAPTER_VERSION, CURRENT_SUFFIX, LOADING_TEXT, IdentityObservation, SetupRejected,
    DASHBOARD_IDENTITY_SNAPSHOT,
)
from .policy import OnboardingPolicy


@dataclass(frozen=True)
class SiteSetupResult:
    identity_check: Literal["PASSED"]
    access_check: Literal["PASSED"]
    payment_capability: Literal["SUPPORTED", "NO_PAYMENT_DATA", "UNKNOWN", "NOT_CHECKED"]
    identity_source: str
    adapter_version: str = ADAPTER_VERSION
    inventory_capability: Literal["SUPPORTED", "UNKNOWN", "NOT_CHECKED"] = "NOT_CHECKED"

    def __post_init__(self) -> None:
        if self.identity_check != "PASSED" or self.access_check != "PASSED":
            raise ValueError("successful setup result requires both checks to pass")
        if self.payment_capability not in {"SUPPORTED", "NO_PAYMENT_DATA", "UNKNOWN", "NOT_CHECKED"}:
            raise ValueError("invalid payment capability")
        if self.inventory_capability not in {"SUPPORTED", "UNKNOWN", "NOT_CHECKED"}:
            raise ValueError("invalid inventory capability")

    def to_payload(self) -> dict[str, str]:
        return {"identity_check": self.identity_check, "access_check": self.access_check,
                "payment_capability": self.payment_capability, "inventory_capability": self.inventory_capability,
                "identity_source": self.identity_source, "adapter_version": self.adapter_version}


class AmazonStoreSetupPage:
    def __init__(self, policy: OnboardingPolicy, *, navigation: AmazonPaymentsPage | None = None,
                 inventory_policy_path: Path | None = None) -> None:
        self.policy = policy
        self._inventory_policy_path = inventory_policy_path
        self._navigation = navigation or AmazonPaymentsPage(
            login_advancer=AmazonLoginAdvancer(otp_prefill_wait_timeout_ms=policy.otp_autofill_timeout_ms),
            account_switcher=AmazonAccountSwitcher(
            candidate_limit=policy.diagnostic_candidate_limit,
            readiness_timeout_seconds=policy.candidate_ready_timeout_ms / 1000,
            readiness_interval_seconds=policy.sample_interval_ms / 1000,
            stable_sample_count=policy.stable_sample_count,
        ))
        self.last_identity_evidence: dict[str, str | int] = {}
        self._inventory_setup = None

    def _inventory_adapter(self):
        if self._inventory_setup is None:
            from .inventory_adapter import InventoryStoreSetupPage
            self._inventory_setup = InventoryStoreSetupPage(self.policy, inventory_policy_path=self._inventory_policy_path)
        return self._inventory_setup

    def resolved_page_for(self, page: Any) -> Any:
        page = self._navigation.resolved_page_for(page)
        return self._inventory_setup.resolved_page_for(page) if self._inventory_setup else page

    def _classify(self, page: Any, marketplace: MarketplaceRef) -> str:
        closed = getattr(page, "is_closed", None)
        if callable(closed) and closed():
            raise SetupRejected("SESSION_CLOSED", category="CLOSED_PAGE")
        category = classify_switch_landing(str(page.url), domain=marketplace.domain,
                                          expected_path=marketplace.payments_path)
        if category in {"DOMAIN_MISMATCH", "INACTIVE_ACCOUNT"}:
            raise SetupRejected(category, category=category)
        return category

    @staticmethod
    def _validate_site(marketplace: MarketplaceRef) -> None:
        expected = ALLOWED_MARKETPLACES.get(marketplace.code)
        if (expected != (marketplace.domain, marketplace.currency) or not marketplace.enabled
                or marketplace.payments_path != "/payments/dashboard/index.html"):
            raise SetupRejected("DOMAIN_MISMATCH", category="DOMAIN_MISMATCH")

    async def _handle_auth(self, page: Any, marketplace: MarketplaceRef,
                           *, select_site: bool) -> Any:
        try:
            return await self._navigation._raise_if_auth(
                page, marketplace, select_marketplace_on_switcher=select_site)
        except HumanAuthRequired as exc:
            # Login can finish at an account-status page while its old form is
            # disappearing. The live landing takes precedence over the login
            # advancer's AUTH_PAGE_CHANGED; it is not another OTP request.
            # Only closed, wrong-domain and exact inactiveAccount destinations
            # change classification. A real login challenge is preserved.
            category = self._classify(self.resolved_page_for(page), marketplace)
            if exc.kind == "marketplace_switch":
                raise SetupRejected(exc.diagnostic.reason_code,
                    count=exc.diagnostic.candidate_count or 0,
                    category=exc.diagnostic.page_category, stage="marketplace_switch") from None
            if (category == "LOGIN" and exc.diagnostic.reason_code == "AUTH_INPUT_NOT_READY"
                    and exc.diagnostic.stage == "otp_signin"):
                # Only onboarding ends this site attempt. Real CAPTCHA/device
                # challenges retain the existing human handoff. No OTP is read,
                # generated, requested or submitted when autofill never arrives.
                raise SetupRejected("AUTH_OTP_NOT_FILLED", category="LOGIN", stage="otp_signin") from None
            raise

    async def _open_dashboard(self, page: Any, marketplace: MarketplaceRef,
                              *, select_site: bool) -> Any:
        # Do not call _goto_dashboard: its readiness gate is payments-specific.
        page = self.resolved_page_for(page)
        await self._navigation._goto(page, marketplace.payments_url)
        page = await self._handle_auth(page, marketplace, select_site=select_site)
        category = self._classify(page, marketplace)
        if category != "TARGET_PAGE" and not (not select_site and category == "SITE_SWITCHER"):
            raise SetupRejected("UNKNOWN_PAGE", category=category)
        return page

    async def _sample(self, page: Any, marketplace: MarketplaceRef,
                      *, switcher: bool) -> IdentityObservation:
        category = self._classify(page, marketplace)
        if category != ("SITE_SWITCHER" if switcher else "TARGET_PAGE"):
            raise SetupRejected("UNKNOWN_PAGE", category=category)
        if switcher:
            snapshot = await read_switcher_snapshot(
                page, candidate_limit=self.policy.diagnostic_candidate_limit)
            if snapshot.truncated:
                raise SetupRejected("IDENTITY_CANDIDATE_LIMIT", category=category)
            current = [account.current_identity for account in snapshot.accounts
                       if account.current_identity]
            if len(current) > 1:
                # Identically named accounts are still distinct rows. Do not
                # let normalized-value deduplication hide ambiguous ownership.
                raise SetupRejected("IDENTITY_AMBIGUOUS", count=len(current), category=category)
            # Country rows are never part of this collection. No suffix means
            # no proof of current identity, even with exactly one account.
            machine = []
            visible = [(value, "account_switcher:current") for value in current]
            container = snapshot.layout_known
            loading = snapshot.loading
        else:
            locator = page.locator(self._navigation.contract.identity_candidates)
            snapshot = await locator.evaluate_all(
                DASHBOARD_IDENTITY_SNAPSHOT, self.policy.diagnostic_candidate_limit)
            if snapshot["truncated"]:
                raise SetupRejected("IDENTITY_CANDIDATE_LIMIT", category=category)
            machine, visible = [], []
            for key, values in parse_qs(urlparse(str(page.url)).query).items():
                if key.lower() in {"merchantid", "sellerid", "mons_sel_dir_mcid"}:
                    machine.extend((value, f"url:{key}") for value in values if value)
            for item in snapshot["items"]:
                machine.extend((value, source) for value, source in item["machine"] if value)
                visible.extend((" ".join(value.split()).split("|")[0].strip(), source)
                               for value, source in item["visible"] if value)
            if len(machine) + len(visible) > self.policy.diagnostic_candidate_limit:
                raise SetupRejected("IDENTITY_CANDIDATE_LIMIT", category=category)
            body = await page.locator("body").inner_text()
            loading = bool(LOADING_TEXT.search(body))
            container = bool(snapshot["items"] or machine)
        if self._classify(page, marketplace) != category:
            raise SetupRejected("IDENTITY_PAGE_NOT_READY", category=category)
        return IdentityObservation(tuple(machine), tuple(visible), container, loading)

    async def _stable(self, page: Any, marketplace: MarketplaceRef,
                      *, switcher: bool = False) -> IdentityObservation:
        previous = None
        stable = 0
        last = IdentityObservation()
        reading = False
        try:
            async with asyncio.timeout(self.policy.candidate_ready_timeout_ms / 1000):
                while True:
                    reading = True
                    last = await self._sample(page, marketplace, switcher=switcher)
                    reading = False
                    if not last.loading:
                        for group in last.groups():
                            if len(group) > 1:
                                raise SetupRejected("IDENTITY_AMBIGUOUS", count=len(group))
                    ready = last.container_present and not last.loading and any(last.groups())
                    fingerprint = last.fingerprint
                    stable = (stable + 1 if ready and fingerprint == previous else int(ready))
                    if stable >= self.policy.stable_sample_count:
                        return last
                    previous = fingerprint if ready else None
                    await asyncio.sleep(self.policy.sample_interval_ms / 1000)
        except TimeoutError:
            reason = ("IDENTITY_PAGE_NOT_READY" if reading or last.loading or any(last.groups())
                      else "IDENTITY_CURRENT_NOT_FOUND" if last.container_present
                      else "IDENTITY_LAYOUT_UNKNOWN")
            raise SetupRejected(reason, category="SITE_SWITCHER" if switcher else "TARGET_PAGE") from None

    def _record(self, observation: IdentityObservation, value: tuple[str, str]) -> tuple[str, str]:
        source = value[1]
        # The old source reader can include a URL key; expose only fixed kinds.
        source = source if source in {"account_switcher:current", "dom:visible_account",
                                      "dom:content", "dom:data-merchant-id", "dom:data-seller-id",
                                      "dom:label", "dom:value"} else "url:merchant_identity"
        self.last_identity_evidence = {
            "adapter_version": ADAPTER_VERSION, "identity_source": source,
            "candidate_count": sum(len(group) for group in observation.groups()),
            "selection_basis": "stable_unique_current",
        }
        if source == "account_switcher:current":
            self.last_identity_evidence["identity_contract_version"] = SWITCHER_CONTRACT_VERSION
        return value[0], source

    async def _switcher_identity(self, page: Any, marketplace: MarketplaceRef) -> IdentityObservation:
        page = self.resolved_page_for(page)
        if self._classify(page, marketplace) != "SITE_SWITCHER":
            await self._navigation._goto(
                page, f"https://{marketplace.domain}/account-switcher/default/merchantMarketplace")
        page = await self._handle_auth(page, marketplace, select_site=False)
        return await self._stable(page, marketplace, switcher=True)

    def _require_open(self, page: Any) -> None:
        for candidate in (page, self.resolved_page_for(page)):
            closed = getattr(candidate, "is_closed", None)
            if callable(closed) and closed():
                raise SetupRejected("SESSION_CLOSED", category="CLOSED_PAGE") from None

    async def detect_identity(self, page: Any, marketplace: MarketplaceRef) -> tuple[str, str]:
        try:
            if marketplace.code in INVENTORY_MARKETPLACES and marketplace.code not in ALLOWED_MARKETPLACES:
                adapter = self._inventory_adapter()
                result = await adapter.detect_identity(page, marketplace)
                self.last_identity_evidence = dict(adapter.last_identity_evidence)
                return result
            return await self._detect_identity(page, marketplace)
        except PlaywrightTimeoutError:
            self._require_open(page)
            raise SetupRejected("NAVIGATION_TIMEOUT", stage="identity_read") from None
        except Exception:
            # A target may close inside goto/count, not just before a sample.
            # Do not retain its raw protocol error or retry on another store.
            self._require_open(page)
            raise

    async def _detect_identity(self, page: Any, marketplace: MarketplaceRef) -> tuple[str, str]:
        self._validate_site(marketplace)
        page = await self._open_dashboard(page, marketplace, select_site=False)
        if self._classify(page, marketplace) == "SITE_SWITCHER":
            try:
                observation = await self._switcher_identity(page, marketplace)
            except SetupRejected as exc:
                if exc.diagnostic.reason_code not in {"IDENTITY_CURRENT_NOT_FOUND", "IDENTITY_PAGE_NOT_READY"}:
                    raise
                # An available account without '(current)' is not a binding.
                # Enter the exact requested site through the already-validated
                # switcher, then require fresh current-identity evidence there.
                page = await self._open_dashboard(page, marketplace, select_site=True)
                observation = await self._dashboard_or_current_identity(page, marketplace)
        else:
            observation = await self._dashboard_or_current_identity(page, marketplace)
        return self._record(observation, observation.choose())

    async def _dashboard_or_current_identity(self, page: Any, marketplace: MarketplaceRef) -> IdentityObservation:
        try:
            return await self._stable(page, marketplace)
        except SetupRejected as exc:
            if exc.diagnostic.reason_code not in {"IDENTITY_CURRENT_NOT_FOUND", "IDENTITY_LAYOUT_UNKNOWN", "IDENTITY_PAGE_NOT_READY"}:
                raise
            return await self._switcher_identity(page, marketplace)

    async def _payment_capability(self, page: Any) -> str:
        # Structure only: never read an amount, account tail or payment control.
        if await self._navigation._no_data_message(page) is not None:
            return "NO_PAYMENT_DATA"
        if await page.locator(self._navigation.contract.balance_rows).count() > 0:
            return "SUPPORTED"
        return "UNKNOWN"

    async def _header_proves_site(self, page: Any, marketplace: MarketplaceRef,
                                 expected: str) -> bool:
        # The existing identity contract already reads these account headers as
        # "seller | marketplace". Neither URL alone nor a hidden meta id proves
        # business access when the payment DOM is missing.
        headers = page.locator(
            '.partner-info, .partner-name, [data-testid="account-switcher"], '
            'kat-dropdown[name="account-switcher"]')
        count = await headers.count()
        if count > self.policy.diagnostic_candidate_limit:
            return False
        countries = {_identity_normal(name) for name in MARKETPLACE_NAMES[marketplace.domain]}
        proofs = []
        for index in range(count):
            element = headers.nth(index)
            text = await element.get_attribute("label") or await element.inner_text()
            parts = [part.strip() for part in text.split("|")]
            if len(parts) == 2 and _identity_normal(parts[0]) == _identity_normal(expected):
                proofs.append(_identity_normal(CURRENT_SUFFIX.sub("", parts[1])) in countries)
        return bool(proofs) and all(proofs)

    async def _wait_for_site_evidence(self, page: Any, marketplace: MarketplaceRef,
                                      expected: str) -> str:
        try:
            async with asyncio.timeout(self.policy.candidate_ready_timeout_ms / 1000):
                while True:
                    category = self._classify(page, marketplace)
                    if category != "TARGET_PAGE":
                        raise SetupRejected("UNKNOWN_PAGE", category=category, stage="site_access")
                    capability = await self._payment_capability(page)
                    if capability != "UNKNOWN" or await self._header_proves_site(page, marketplace, expected):
                        return capability
                    await asyncio.sleep(self.policy.sample_interval_ms / 1000)
        except TimeoutError:
            raise SetupRejected("SITE_ACCESS_UNPROVEN", category="TARGET_PAGE",
                                stage="site_access") from None

    async def check_site(self, page: Any, store: StoreRef,
                         marketplace: MarketplaceRef) -> SiteSetupResult:
        try:
            # Overlapping sites (DE) use the payment landing for basic setup.
            # Inventory readiness is not a prerequisite for payment access.
            if marketplace.code in INVENTORY_MARKETPLACES and marketplace.code not in ALLOWED_MARKETPLACES:
                return await self._inventory_adapter().check_site(page, store, marketplace)
            return await self._check_site(page, store, marketplace)
        except PlaywrightTimeoutError:
            self._require_open(page)
            raise SetupRejected("NAVIGATION_TIMEOUT", stage="site_access") from None
        except Exception:
            self._require_open(page)
            raise

    async def _check_site(self, page: Any, store: StoreRef,
                          marketplace: MarketplaceRef) -> SiteSetupResult:
        self._validate_site(marketplace)
        if not store.identity_confirmed or not store.expected_seller_id:
            raise SetupRejected("IDENTITY_CURRENT_NOT_FOUND")
        page = await self._open_dashboard(page, marketplace, select_site=True)
        first = None
        try:
            first = await self._stable(page, marketplace)
        except SetupRejected as exc:
            if exc.diagnostic.reason_code not in {"IDENTITY_CURRENT_NOT_FOUND", "IDENTITY_LAYOUT_UNKNOWN"}:
                raise
        expected = _identity_normal(store.expected_seller_id)
        matches = first is not None and any(expected in group for group in first.groups())
        if matches:
            identity = self._record(first, first.choose(store.expected_seller_id))
            dashboard_proof = first
        else:
            # The old binding may be a display name or a machine id; never
            # guess its type. Compare all overlapping representations across
            # the fallback, then require an exact match to the existing binding.
            switched = await self._switcher_identity(page, marketplace)
            if first is not None:
                first.require_consistent_with(switched)
            identity = self._record(switched, switched.choose(store.expected_seller_id))
            page = await self._open_dashboard(page, marketplace, select_site=True)
            try:
                returned = await self._stable(page, marketplace)
            except SetupRejected as exc:
                if exc.diagnostic.reason_code not in {"IDENTITY_CURRENT_NOT_FOUND", "IDENTITY_LAYOUT_UNKNOWN"}:
                    raise
                returned = None
            if returned is None and first is not None:
                raise SetupRejected("IDENTITY_PAGE_NOT_READY")
            if returned is not None:
                switched.require_consistent_with(returned)
                if first is not None:
                    first.require_consistent_with(returned)
            dashboard_proof = returned
        category = self._classify(page, marketplace)
        if category != "TARGET_PAGE":
            raise SetupRejected("UNKNOWN_PAGE", category=category)
        capability = await self._wait_for_site_evidence(page, marketplace, identity[0])
        if self._classify(page, marketplace) != "TARGET_PAGE":
            raise SetupRejected("UNKNOWN_PAGE", stage="site_access")
        # Recheck the live page after reading its structure. A stale successful
        # identity observation must not survive a background seller/site change.
        try:
            async with asyncio.timeout(self.policy.candidate_ready_timeout_ms / 1000):
                final = await self._sample(page, marketplace, switcher=False)
        except TimeoutError:
            raise SetupRejected("IDENTITY_PAGE_NOT_READY") from None
        if final.loading or (dashboard_proof is not None and not any(final.groups())):
            raise SetupRejected("IDENTITY_PAGE_NOT_READY")
        for group in final.groups():
            if len(group) > 1:
                raise SetupRejected("IDENTITY_AMBIGUOUS", count=len(group))
        if dashboard_proof is not None:
            dashboard_proof.require_consistent_with(final)
        else:
            switched.require_consistent_with(final)
        return SiteSetupResult("PASSED", "PASSED", capability, identity[1])
