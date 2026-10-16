"""EU onboarding reads the current seller, switches the UI marketplace and checks
inventory access. It never scans products, opens sidebars or submits a form.
"""
from types import SimpleNamespace
from dataclasses import replace
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..amazon_inventory_gpsr.config import (
    INVENTORY_DOMAIN, INVENTORY_MARKETPLACES, INVENTORY_PATH, INVENTORY_URL,
    MARKETPLACE_LABELS, InventoryTiming,
)
from ..amazon_inventory_gpsr.page import AmazonInventoryGpsrPage, InventoryContractError, ItemLoadError, _DOM_HELPERS, _LIST
from ..amazon_login import AmazonLoginAdvancer
from ..errors import HumanAuthRequired
from ..setup_diagnostics import classify_switch_landing
from .adapter import AmazonStoreSetupPage, SiteSetupResult
from .config import SETUP_MARKETPLACES
from .identity import IdentityObservation, SetupRejected


IDENTITY_SNAPSHOT = """(p) => { /* setup:inventory-identity */
%s
const headers=[...document.querySelectorAll('#dropdown-account-switcher-container')].filter(visible);
const names=headers.flatMap(h=>[...h.querySelectorAll('.dropdown-account-switcher-header-label-global')].filter(visible).map(text));
const markets=headers.flatMap(h=>[...h.querySelectorAll('.dropdown-account-switcher-header-label-regional')].filter(visible).map(text));
const elements=[...document.querySelectorAll('meta[name="merchantId"],meta[name="sellerId"],[data-merchant-id],[data-seller-id]')];
const machine=elements.flatMap(e=>['content','data-merchant-id','data-seller-id'].map(k=>[e.getAttribute(k),`dom:${k}`])).filter(v=>v[0]);
return {names,markets,machine,headers:headers.length,
  loading:deep(document,'kat-spinner,[aria-busy="true"]').some(visible),
  truncated:names.length+machine.length>p.limit};
}""" % _DOM_HELPERS


class InventoryStoreSetupPage(AmazonStoreSetupPage):
    def __init__(self, policy, *, inventory=None, inventory_policy_path=None):
        super().__init__(policy)
        interval = policy.sample_interval_ms / 1000
        timeout = policy.candidate_ready_timeout_ms / 1000
        base = InventoryTiming.from_file(inventory_policy_path) if inventory_policy_path else InventoryTiming()
        self.inventory = inventory or AmazonInventoryGpsrPage(
            timing=replace(base, list_timeout_seconds=timeout, panel_timeout_seconds=timeout,
                poll_seconds=interval, stable_seconds=interval * (policy.stable_sample_count - 1)),
            login_advancer=AmazonLoginAdvancer(otp_prefill_wait_timeout_ms=policy.otp_autofill_timeout_ms),
        )
        self._pages = WeakKeyDictionary()

    def resolved_page_for(self, page):
        while page in self._pages and self._pages[page] is not page:
            page = self._pages[page]
        return self.inventory.resolved_page_for(page)

    def _remember_page(self, old, new):
        if old is not new:
            self._pages[old] = new
        return new

    @staticmethod
    def _validate_site(marketplace):
        if (marketplace.code not in INVENTORY_MARKETPLACES or not marketplace.enabled
                or SETUP_MARKETPLACES[marketplace.code] != (marketplace.domain, marketplace.currency)):
            raise SetupRejected("DOMAIN_MISMATCH", category="DOMAIN_MISMATCH")

    def _classify(self, page, marketplace):
        if page.is_closed():
            raise SetupRejected("SESSION_CLOSED", category="CLOSED_PAGE")
        try:
            host = self.inventory._trusted_host(page)
        except InventoryContractError:
            raise SetupRejected("DOMAIN_MISMATCH", category="DOMAIN_MISMATCH") from None
        category = classify_switch_landing(str(page.url), domain=host, expected_path=INVENTORY_PATH)
        if category in {"INACTIVE_ACCOUNT", "DOMAIN_MISMATCH"}:
            raise SetupRejected(category, category=category)
        if category == "TARGET_PAGE" and urlparse(str(page.url)).path != INVENTORY_PATH:
            raise SetupRejected("UNKNOWN_PAGE", category="UNKNOWN_PAGE")
        return category

    async def _open_inventory(self, page, marketplace):
        self._validate_site(marketplace)
        source = page
        page = self.resolved_page_for(page)
        # A known EU origin may serve several business marketplaces. Reuse it;
        # URL suffix is never accepted as evidence of the selected country.
        host = urlparse(str(page.url)).hostname
        if host in INVENTORY_MARKETPLACES.values():
            self._classify(page, marketplace)
            if AmazonLoginAdvancer.is_supported_login_url(str(page.url), expected_host=host):
                resolved = await self.inventory.login.advance_or_raise(page, expected_host=host)
                if resolved is None:
                    raise HumanAuthRequired("建档等待当前商城登录完成", kind="signin")
                page = self._remember_page(source, resolved)
                host = self.inventory._trusted_host(page)
            if self.inventory._has_full_inventory_query(page):
                return self._remember_page(source, page)
        else:
            # Initial blank/another supported-region page: navigate once to the
            # requested EU inventory page, not a guessed login/challenge URL.
            host = marketplace.domain
        outcome = await self.inventory.landing.land_on(page,
            url=INVENTORY_URL.replace(INVENTORY_DOMAIN, host), expected_path=INVENTORY_PATH, domain=host)
        page = self._remember_page(source, outcome.page or page)
        category = self._classify(page, marketplace)
        if not outcome.ok:
            if outcome.reason == "auth_required":
                raise HumanAuthRequired("建档等待当前商城登录完成", kind="signin")
            raise SetupRejected("MARKETPLACE_UNKNOWN", category=category, stage="marketplace_switch")
        if category != "TARGET_PAGE":
            raise SetupRejected("UNKNOWN_PAGE", category=category)
        return page

    async def _sample(self, page, marketplace, *, switcher=False):
        if self._classify(page, marketplace) != "TARGET_PAGE":
            raise SetupRejected("UNKNOWN_PAGE")
        raw = await self.inventory._evaluate(page, IDENTITY_SNAPSHOT, {"limit": self.policy.diagnostic_candidate_limit})
        if raw["truncated"]:
            raise SetupRejected("IDENTITY_CANDIDATE_LIMIT")
        if raw["headers"] > 1 or len(raw["names"]) > 1:
            raise SetupRejected("IDENTITY_AMBIGUOUS", count=len(raw["names"]))
        labels = {label for names in MARKETPLACE_LABELS.values() for label in names}
        valid = raw["headers"] == 1 and len(raw["markets"]) == 1 and raw["markets"][0] in labels
        return IdentityObservation(
            machine=tuple(tuple(x) for x in raw["machine"]) if valid else (),
            visible=tuple((name, "dom:visible_account") for name in raw["names"]) if valid else (),
            container_present=valid, loading=raw["loading"],
        )

    async def detect_identity(self, page, marketplace):
        try:
            page = await self._open_inventory(page, marketplace)
            observation = await self._stable(page, marketplace)
            return self._record(observation, observation.choose())
        except (TimeoutError, PlaywrightTimeoutError, ItemLoadError):
            self._require_open(page)
            raise SetupRejected("NAVIGATION_TIMEOUT", stage="identity_read") from None

    async def check_site(self, page, store, marketplace):
        try:
            return await self._check_inventory_site(page, store, marketplace)
        except (TimeoutError, PlaywrightTimeoutError, ItemLoadError):
            self._require_open(page)
            raise SetupRejected("NAVIGATION_TIMEOUT", stage="site_access") from None

    async def _check_inventory_site(self, page, store, marketplace):
        if not store.identity_confirmed or not store.expected_seller_id:
            raise SetupRejected("IDENTITY_CURRENT_NOT_FOUND")
        page = await self._open_inventory(page, marketplace)
        first = await self._stable(page, marketplace)
        first.choose(store.expected_seller_id)
        run = SimpleNamespace(store=store)
        try:
            switched = await self.inventory._switch_marketplace(page, run, marketplace)
            page = self._remember_page(page, switched)
            page = await self._open_inventory(page, marketplace)
            self.inventory._remember(page, page, run, marketplace)
            # Access evidence only. Do not use _wait_list: that scrolls all rows.
            async def access():
                self._classify(page, marketplace)
                return await self.inventory._evaluate(page, _LIST)
            await self.inventory._poll(access,
                lambda s: s.get("ready") and not s.get("loading")
                    and s.get("filters", []).count("ApprovalRequired") == 1,
                self.policy.candidate_ready_timeout_ms / 1000,
                message="库存页访问或需要批准筛选未确认", contract=True)
            await self.inventory._identity(page, run)
        except InventoryContractError:
            self._classify(self.resolved_page_for(page), marketplace)
            # Do not turn a late seller change into an ordinary access failure.
            current = await self._stable(self.resolved_page_for(page), marketplace)
            current.choose(store.expected_seller_id)
            raise SetupRejected("INVENTORY_ACCESS_UNPROVEN", stage="site_access") from None
        final = await self._stable(page, marketplace)
        first.require_consistent_with(final)
        identity = self._record(final, final.choose(store.expected_seller_id))
        await self.inventory._identity(page, run)
        return SiteSetupResult("PASSED", "PASSED", "NOT_CHECKED", identity[1])
