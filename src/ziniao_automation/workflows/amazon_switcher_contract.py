"""Versioned account/marketplace tree contract, observed in Ziniao 2026-09-27.

Account and country buttons share the same class. Only their hierarchy separates
these roles. Neither a single account nor a selected country proves the current
seller. Text/state parsing is pure Python; page reading performs no action.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

VERSION = "account-switcher-tree-v1"
ROOT_SELECTOR = ".full-page-account-switcher"
ACCOUNT_ROW_SELECTOR = (
    ROOT_SELECTOR + " > .full-page-account-switcher-inner"
    " > .full-page-account-switcher-accounts-wrapper"
    " > .full-page-account-switcher-accounts > .full-page-account-switcher-account"
)
ACCOUNT_BUTTON_SELECTOR = ACCOUNT_ROW_SELECTOR + " > button.full-page-account-switcher-account-details"
MARKETPLACE_BUTTON_SELECTOR = (
    ACCOUNT_ROW_SELECTOR + " > .full-page-account-switcher-accounts"
    " > .full-page-account-switcher-account > button.full-page-account-switcher-account-details"
)
RELATIVE_MARKETPLACE_SELECTOR = (
    ":scope > .full-page-account-switcher-accounts > .full-page-account-switcher-account"
    " > button.full-page-account-switcher-account-details"
)
CURRENT_SUFFIX = re.compile(r"\s*[（(]\s*(?:当前|current)\s*[）)]\s*$", re.I)
STATUS_SUFFIX = re.compile(r"^(.+?)\s*[（(]\s*([^()（）]+?)\s*[）)]\s*$")
LOADING_TEXT = re.compile(r"正在加载账户|loading accounts", re.I)
_MARKERS = {
    "当前": "CURRENT", "current": "CURRENT",
    "待注册": "PENDING_REGISTRATION", "待註冊": "PENDING_REGISTRATION",
    "pending registration": "PENDING_REGISTRATION",
    "不可用": "UNAVAILABLE", "unavailable": "UNAVAILABLE",
}


def clean(text: str) -> str:
    return " ".join(text.split())


def parse_marketplace_text(text: str) -> tuple[str, str]:
    text = clean(text)
    match = STATUS_SUFFIX.fullmatch(text)
    if match:
        base, marker = match.groups()
        if any(ch in base for ch in "()（）"):
            return text, "UNKNOWN"
        return clean(base), _MARKERS.get(clean(marker).casefold(), "UNKNOWN")
    # Unknown bracket syntax must not become an available country name.
    return text, "UNKNOWN" if any(ch in text for ch in "()（）") else "AVAILABLE"


@dataclass(frozen=True)
class AccountCandidate:
    text: str

    @property
    def current_identity(self) -> str | None:
        return CURRENT_SUFFIX.sub("", self.text).strip() if CURRENT_SUFFIX.search(self.text) else None


@dataclass(frozen=True)
class MarketplaceCandidate:
    index: int
    account_index: int
    text: str
    disabled: bool = False

    @property
    def name_and_status(self) -> tuple[str, str]:
        name, status = parse_marketplace_text(self.text)
        return name, "UNAVAILABLE" if self.disabled else status


@dataclass(frozen=True)
class SwitcherSnapshot:
    accounts: tuple[AccountCandidate, ...]
    marketplaces: tuple[MarketplaceCandidate, ...]
    layout_known: bool
    loading: bool = False
    truncated: bool = False

    @property
    def fingerprint(self) -> tuple:
        return (self.accounts, self.marketplaces, self.layout_known, self.loading, self.truncated)

    @property
    def ready(self) -> bool:
        return (self.layout_known and not self.loading and not self.truncated
                and bool(self.accounts) and bool(self.marketplaces)
                and all(item.text for item in (*self.accounts, *self.marketplaces)))


def choose_marketplace(snapshot: SwitcherSnapshot, names: frozenset[str]) -> tuple[int | None, str]:
    if snapshot.truncated:
        return None, "candidate_limit"
    if not snapshot.layout_known:
        return None, "layout_unknown"
    if not snapshot.ready:
        return None, "list_not_loaded"
    matches = [item for item in snapshot.marketplaces if item.name_and_status[0] in names]
    # Duplicate countries remain ambiguous even if one is disabled/pending.
    if len(matches) > 1:
        return None, "ambiguous"
    if not matches:
        return None, "not_offered"
    target = matches[0]
    active = [i for i, account in enumerate(snapshot.accounts) if account.current_identity]
    if len(active) > 1 or (len(snapshot.accounts) > 1 and not active):
        return None, "ambiguous"
    if active and target.account_index != active[0]:
        return None, "account_mismatch"
    status = target.name_and_status[1]
    refusal = {"PENDING_REGISTRATION": "registration_required",
               "UNAVAILABLE": "unavailable", "UNKNOWN": "layout_unknown"}.get(status)
    return (None, refusal) if refusal else (target.index, "")


async def read_switcher_snapshot(page: Any, *, candidate_limit: int | None = None) -> SwitcherSnapshot:
    """Read the observed tree with scoped locators, never global buttons."""
    root_count = await page.locator(ROOT_SELECTOR).count()
    body = await page.locator("body").inner_text()
    loading = bool(LOADING_TEXT.search(body))
    unknown = SwitcherSnapshot((), (), False, loading=loading)
    if root_count != 1:
        return unknown
    rows = page.locator(ACCOUNT_ROW_SELECTOR)
    accounts = page.locator(ACCOUNT_BUTTON_SELECTOR)
    row_count, account_count = await rows.count(), await accounts.count()
    if row_count != account_count or account_count < 1:
        return unknown
    market_count = await page.locator(MARKETPLACE_BUTTON_SELECTOR).count()
    if candidate_limit is not None and account_count + market_count > candidate_limit:
        return SwitcherSnapshot((), (), True, truncated=True)
    account_items, market_items = [], []
    for index in range(account_count):
        account_items.append(AccountCandidate(clean(await accounts.nth(index).inner_text())))
        children = rows.nth(index).locator(RELATIVE_MARKETPLACE_SELECTOR)
        for child_index in range(await children.count()):
            if candidate_limit is not None and len(account_items) + len(market_items) >= candidate_limit:
                return SwitcherSnapshot((), (), True, loading=loading, truncated=True)
            element = children.nth(child_index)
            disabled = ((await element.get_attribute("disabled")) is not None
                        or await element.get_attribute("aria-disabled") == "true")
            market_items.append(MarketplaceCandidate(
                len(market_items), index, clean(await element.inner_text()), disabled))
    if len(market_items) != market_count:
        return unknown
    body = await page.locator("body").inner_text()
    return SwitcherSnapshot(tuple(account_items), tuple(market_items), True,
                            loading=bool(LOADING_TEXT.search(body)))
