"""Amazon Feedback Manager Playwright adapter.

The list is an Angular ``kat-table``; the removal flow is an inline side panel
inside the row rather than a modal.  Katal components keep their internals in
open shadow roots, so every lookup walks light + shadow DOM.

Two properties of the live page shape everything here:

* rows carry no unique id of their own (each renders ``id="feedback-details"``),
  so a row is always resolved by its order id and never by position;
* Amazon does not offer ``request_removal`` on every feedback — the option is
  simply absent from the row's action menu — so availability is read, never
  assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from ..errors import DomContractError
from .config import (
    FEEDBACK_MANAGER_PATH,
    MAX_RATING_ELIGIBLE_FOR_REMOVAL,
    FeedbackDomContract,
    is_known_reason,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeedbackRow:
    """One row of the feedback table, as read off the page."""

    order_id: str
    rating: int
    order_date: str | None
    comment: str
    removal_available: bool
    # False when the action menu itself could not be inspected (element absent,
    # or its shadow root not attached yet).  Without this, a row whose menu was
    # merely slow to render is indistinguishable from one Amazon has withdrawn
    # the action from — and the latter is recorded as a terminal, irreversible
    # "already requested".  An unreadable menu must never make that claim.
    menu_readable: bool = True

    @property
    def is_low_star(self) -> bool:
        return 1 <= self.rating <= MAX_RATING_ELIGIBLE_FOR_REMOVAL


# --------------------------------------------------------------------------
# Shared browser-side helpers.  Prepended to every evaluated snippet.
# --------------------------------------------------------------------------
_JS_PRELUDE = """
const deepAll = (selector) => {
    const out = [];
    const walk = (root) => {
        root.querySelectorAll('*').forEach((el) => {
            if (el.matches && el.matches(selector)) out.push(el);
            if (el.shadowRoot) walk(el.shadowRoot);
        });
    };
    walk(document);
    return out;
};
const deepOne = (selector) => deepAll(selector)[0] || null;
const textOf = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
const orderIdOf = (row, contract) => {
    const link = row.querySelector(contract.order_link);
    if (!link) return '';
    return (link.getAttribute(contract.order_attribute) || textOf(link)).trim();
};
const ratingOf = (row, contract) => {
    const star = row.querySelector(contract.rating);
    if (!star) return null;
    const raw = star.getAttribute(contract.rating_attribute);
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
};
const rowFor = (orderId, contract) => {
    const hits = deepAll(contract.rows).filter(
        (row) => orderIdOf(row, contract) === orderId);
    // Ambiguity is a contract violation, not something to pick a winner from.
    return hits.length === 1 ? hits[0] : null;
};
const buttonsIn = (root) => {
    const out = [];
    const walk = (node) => {
        node.querySelectorAll('*').forEach((el) => {
            const tag = el.tagName.toLowerCase();
            if (tag === 'kat-button' || tag === 'button') out.push(el);
            if (el.shadowRoot) walk(el.shadowRoot);
        });
    };
    walk(root);
    return out;
};
const inPanel = (selector, panel) => {
    const out = [];
    const walk = (node) => {
        node.querySelectorAll('*').forEach((el) => {
            if (el.matches && el.matches(selector)) out.push(el);
            if (el.shadowRoot) walk(el.shadowRoot);
        });
    };
    walk(panel);
    return out[0] || null;
};
"""


_READ_ROWS = (
    _JS_PRELUDE
    + """
(contract) => {
    const rows = deepAll(contract.rows);
    return rows.map((row) => {
        const menu = row.querySelector(contract.actions_menu);
        // The option list lives in the component's shadow root; if that is not
        // attached yet we know nothing about this row's availability.
        const menuReadable = Boolean(menu && menu.shadowRoot);
        const removal = menuReadable
            ? menu.shadowRoot.querySelector(contract.action_request_removal)
            : null;
        const cells = Array.from(row.children);
        // The comment cell is the one without a col_* class.
        const commentCell = cells.find(
            (td) => !/^col_/.test(td.className || '')) || cells[3] || null;
        const dateCell = row.querySelector(contract.order_date);
        return {
            order_id: orderIdOf(row, contract),
            rating: ratingOf(row, contract),
            order_date: dateCell ? textOf(dateCell) : null,
            comment: commentCell ? textOf(commentCell) : '',
            removal_available: Boolean(removal),
            menu_readable: menuReadable,
        };
    }).filter((row) => row.order_id && row.rating !== null);
}
"""
)


_OPEN_PANEL = (
    _JS_PRELUDE
    + """
([contract, orderId, maxRating]) => {
    // The panel is page-global, so the only way to know which row it belongs
    // to is to start from none open and open exactly one.
    const before = deepAll(contract.panel).length;
    if (before !== 0) return {ok: false, reason: 'stale_panel_open', open: before};
    const row = rowFor(orderId, contract);
    if (!row) return {ok: false, reason: 'row_not_found'};
    // Re-check the rating against this very row immediately before acting.
    const rating = ratingOf(row, contract);
    if (rating === null || rating < 1 || rating > maxRating) {
        return {ok: false, reason: 'rating_out_of_range', rating: rating};
    }
    const menu = row.querySelector(contract.actions_menu);
    if (!menu || !menu.shadowRoot) return {ok: false, reason: 'no_actions_menu'};
    const option = menu.shadowRoot.querySelector(contract.action_request_removal);
    if (!option) return {ok: false, reason: 'removal_not_offered'};
    option.click();
    return {ok: true, rating: rating};
}
"""
)


_CLICK_CONTINUE = (
    _JS_PRELUDE
    + """
([contract, continueText]) => {
    const panels = deepAll(contract.panel);
    if (panels.length !== 1) {
        return {ok: false, reason: 'ambiguous_panels', open: panels.length};
    }
    const buttons = buttonsIn(panels[0]);
    const target = buttons.find((el) => {
        const label = (el.getAttribute('label') || textOf(el)).trim();
        return label === continueText;
    });
    if (!target) return {ok: false, reason: 'continue_missing'};
    target.click();
    return {ok: true};
}
"""
)


_SELECT_REASON = (
    _JS_PRELUDE
    + """
async ([contract, category, reasonCode]) => {
    const panels = deepAll(contract.panel);
    if (panels.length !== 1) {
        return {ok: false, reason: 'ambiguous_panels', open: panels.length};
    }
    const panel = panels[0];
    const findIn = (selector) => inPanel(selector, panel);
    const fire = (el, value) => {
        el.value = value;
        el.dispatchEvent(new CustomEvent('change', {
            detail: {value: value}, bubbles: true, composed: true}));
    };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    const categoryEl = findIn(contract.category_dropdown);
    if (!categoryEl) return {ok: false, reason: 'category_dropdown_missing'};
    fire(categoryEl, category);
    await sleep(700);

    const reasonEl = findIn(contract.reason_dropdown);
    if (!reasonEl) return {ok: false, reason: 'reason_dropdown_missing'};
    if (reasonEl.hasAttribute('disabled')) {
        return {ok: false, reason: 'reason_dropdown_disabled'};
    }
    const offered = Array.isArray(reasonEl.options)
        ? reasonEl.options.map((o) => String(o.value)) : [];
    if (!offered.includes(String(reasonCode))) {
        return {ok: false, reason: 'reason_not_offered', offered: offered};
    }
    fire(reasonEl, reasonCode);
    await sleep(700);

    const submitEl = findIn(contract.submit_button);
    return {
        ok: true,
        category_value: String(categoryEl.value || ''),
        reason_value: String(reasonEl.value || ''),
        submit_disabled: submitEl ? submitEl.hasAttribute('disabled') : null,
        submit_present: Boolean(submitEl),
    };
}
"""
)


_SUBMIT = (
    _JS_PRELUDE
    + """
([contract, orderId, category, reasonCode, maxRating]) => {
    // Everything is re-verified against the live DOM in the same tick as the
    // click.  Any mismatch aborts without touching the button.
    const row = rowFor(orderId, contract);
    if (!row) return {ok: false, reason: 'row_not_found'};
    const rating = ratingOf(row, contract);
    if (rating === null || rating < 1 || rating > maxRating) {
        return {ok: false, reason: 'rating_out_of_range', rating: rating};
    }
    // Exactly one removal panel may be open across the whole table; it is the
    // one this run opened from a clean slate for this very order.
    const panels = deepAll(contract.panel);
    if (panels.length !== 1) {
        return {ok: false, reason: 'ambiguous_panels', open: panels.length};
    }
    const panel = panels[0];
    const findIn = (selector) => inPanel(selector, panel);
    const categoryEl = findIn(contract.category_dropdown);
    const reasonEl = findIn(contract.reason_dropdown);
    const submitEl = findIn(contract.submit_button);
    if (!categoryEl || !reasonEl || !submitEl) {
        return {ok: false, reason: 'controls_missing'};
    }
    if (String(categoryEl.value || '') !== String(category)) {
        return {ok: false, reason: 'category_mismatch', actual: String(categoryEl.value || '')};
    }
    if (String(reasonEl.value || '') !== String(reasonCode)) {
        return {ok: false, reason: 'reason_mismatch', actual: String(reasonEl.value || '')};
    }
    if (submitEl.hasAttribute('disabled')) {
        return {ok: false, reason: 'submit_disabled'};
    }
    submitEl.click();
    return {ok: true};
}
"""
)


_ROW_COUNT = (
    _JS_PRELUDE
    + """
(contract) => deepAll(contract.rows).length
"""
)


_PANEL_OPEN_COUNT = (
    _JS_PRELUDE
    + """
(contract) => deepAll(contract.panel).length
"""
)


_CLOSE_PANELS = (
    _JS_PRELUDE
    + """
([contract, cancelText]) => {
    let closed = 0;
    deepAll(contract.panel).forEach((panel) => {
        const buttons = [];
        const walk = (root) => {
            root.querySelectorAll('*').forEach((el) => {
                const tag = el.tagName.toLowerCase();
                if (tag === 'kat-button' || tag === 'button') buttons.push(el);
                if (el.shadowRoot) walk(el.shadowRoot);
            });
        };
        walk(panel);
        const cancel = buttons.find((el) => {
            const label = (el.getAttribute('label') || textOf(el)).trim();
            return label === cancelText;
        });
        if (cancel) { cancel.click(); closed += 1; }
    });
    return closed;
}
"""
)


class AmazonFeedbackPage:
    """Page operations for the Feedback Manager.

    Only :meth:`submit_removal` has an irreversible effect.
    """

    def __init__(
        self,
        contract: FeedbackDomContract | None = None,
        *,
        settle_seconds: float = 1.5,
        list_timeout_seconds: float = 25.0,
        cancel_text: str = "取消",
    ) -> None:
        self.contract = contract or FeedbackDomContract()
        self.settle_seconds = settle_seconds
        self.list_timeout_seconds = list_timeout_seconds
        self.cancel_text = cancel_text

    # -- contract payload handed to the browser ---------------------------
    @property
    def _contract_payload(self) -> dict[str, Any]:
        contract = self.contract
        return {
            "rows": contract.rows,
            "rating": contract.rating,
            "rating_attribute": contract.rating_attribute,
            "order_link": contract.order_link,
            "order_attribute": contract.order_attribute,
            "order_date": contract.order_date,
            "actions_menu": contract.actions_menu,
            "action_request_removal": contract.action_request_removal,
            "panel": contract.panel,
            "category_dropdown": contract.category_dropdown,
            "reason_dropdown": contract.reason_dropdown,
            "submit_button": contract.submit_button,
        }

    async def open_list(self, page: Any, domain: str) -> bool:
        """Navigate to the feedback manager and wait for its rows to arrive.

        The table shell renders before Angular has fetched anything, so a fixed
        pause reads an empty list as "this store has no feedback" — which looks
        exactly like success and silently does nothing.  Poll for rows instead
        and report whether any ever appeared, so the caller can tell an
        genuinely empty list from one that never loaded.
        """

        url = f"https://{domain}{FEEDBACK_MANAGER_PATH}"
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        try:
            await page.wait_for_selector(
                self.contract.list_root, timeout=45_000, state="attached"
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a contract error
            raise DomContractError(
                f"反馈管理页未出现预期的表格结构（契约 {self.contract.version}）"
            ) from exc

        deadline = self.list_timeout_seconds
        waited = 0.0
        step = self.settle_seconds
        while waited < deadline:
            await page.wait_for_timeout(int(step * 1000))
            waited += step
            if await page.evaluate(_ROW_COUNT, self._contract_payload):
                # One more settle so a partially rendered batch finishes.
                await page.wait_for_timeout(int(step * 1000))
                return True
        return False

    async def read_rows(self, page: Any) -> list[FeedbackRow]:
        raw = await page.evaluate(_READ_ROWS, self._contract_payload)
        rows: list[FeedbackRow] = []
        for item in raw:
            rows.append(
                FeedbackRow(
                    order_id=str(item["order_id"]),
                    rating=int(item["rating"]),
                    order_date=item.get("order_date") or None,
                    comment=str(item.get("comment") or ""),
                    removal_available=bool(item.get("removal_available")),
                    menu_readable=bool(item.get("menu_readable", True)),
                )
            )
        return rows

    async def wait_until_action_gone(
        self, page: Any, order_id: str, *, timeout_seconds: float = 12.0
    ) -> bool:
        """Poll until the row stops offering 「请求审核」.

        Amazon withdraws the action once a review has been requested, but the
        Angular list re-renders asynchronously, so a single read right after the
        click reports the old state and turns a successful submission into
        「未确认」.  Returns False on timeout, which the caller treats as
        unconfirmed — never as a reason to submit again.
        """

        waited = 0.0
        step = self.settle_seconds
        while waited < timeout_seconds:
            rows = {row.order_id: row for row in await self.read_rows(page)}
            row = rows.get(order_id)
            if row is not None and row.menu_readable and not row.removal_available:
                return True
            await page.wait_for_timeout(int(step * 1000))
            waited += step
        return False

    async def open_removal_panel(self, page: Any, order_id: str) -> dict[str, Any]:
        """Open the inline removal panel and advance past 继续.

        Returns a result dict; ``ok`` False carries a machine-readable
        ``reason`` so the caller can route it without parsing prose.
        """

        opened = await page.evaluate(
            _OPEN_PANEL,
            [self._contract_payload, order_id, MAX_RATING_ELIGIBLE_FOR_REMOVAL],
        )
        if not opened.get("ok"):
            return opened
        await page.wait_for_timeout(int(self.settle_seconds * 1000))

        # Opening from zero must have produced exactly one panel; that is what
        # ties this page-global panel to the row we clicked.
        count = await self.open_panel_count(page)
        if count != 1:
            return {"ok": False, "reason": "ambiguous_panels", "open": count}

        advanced = await page.evaluate(
            _CLICK_CONTINUE,
            [self._contract_payload, self.contract.continue_button_pattern],
        )
        if not advanced.get("ok"):
            return advanced
        await page.wait_for_timeout(int(self.settle_seconds * 1000))
        return {"ok": True, "rating": opened.get("rating")}

    async def select_reason(
        self, page: Any, order_id: str, category: str, reason_code: str
    ) -> dict[str, Any]:
        """Set both dropdowns and read the values back."""

        if not is_known_reason(category, reason_code):
            raise ValueError(f"未知的请求原因：{category}/{reason_code}")
        result = await page.evaluate(
            _SELECT_REASON, [self._contract_payload, category, reason_code]
        )
        if not result.get("ok"):
            return result
        if result.get("category_value") != category:
            return {"ok": False, "reason": "category_readback_mismatch", **result}
        if result.get("reason_value") != reason_code:
            return {"ok": False, "reason": "reason_readback_mismatch", **result}
        if result.get("submit_disabled") is not False:
            return {"ok": False, "reason": "submit_still_disabled", **result}
        return result

    async def submit_removal(
        self, page: Any, order_id: str, category: str, reason_code: str
    ) -> dict[str, Any]:
        """⚠️ Irreversible.  Re-verifies every invariant in the click's own tick."""

        result = await page.evaluate(
            _SUBMIT,
            [
                self._contract_payload,
                order_id,
                category,
                reason_code,
                MAX_RATING_ELIGIBLE_FOR_REMOVAL,
            ],
        )
        if result.get("ok"):
            await page.wait_for_timeout(int(self.settle_seconds * 1000))
        return result

    async def open_panel_count(self, page: Any) -> int:
        return int(await page.evaluate(_PANEL_OPEN_COUNT, self._contract_payload))

    async def close_panels(self, page: Any) -> int:
        """Return the table to its resting state after any outcome."""

        closed = int(
            await page.evaluate(
                _CLOSE_PANELS, [self._contract_payload, self.cancel_text]
            )
        )
        await page.wait_for_timeout(int(self.settle_seconds * 1000))
        return closed
