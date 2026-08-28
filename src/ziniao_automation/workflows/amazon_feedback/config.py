"""Observed Feedback Manager DOM contract and the removal reason catalog.

Everything here was read off the live zh_CN Feedback Manager on 2026-08-28
(store 正香-张刚CA, sellercentral.amazon.ca).  Amazon's own values are stable
slugs and numeric codes rather than display text, so the selectors below do not
depend on the seller's interface language.
"""

from __future__ import annotations

from dataclasses import dataclass


FEEDBACK_MANAGER_PATH = "/feedback-manager/index.html"


@dataclass(frozen=True, slots=True)
class FeedbackDomContract:
    """Selectors verified against the live Angular Feedback Manager."""

    version: str = "feedback-manager-v1"

    # Data rows are ``kat-table-row`` elements that all carry the *same*
    # ``id="feedback-details"`` — a template literal, not a unique key — so rows
    # are identified by their order id instead.  The header row is a
    # ``kat-table-row.fb-detail-table-header`` without that id, so keying on the
    # id excludes it for free.  There are no ``<tr>`` elements at rest.
    list_root: str = "feedback-list kat-table"
    rows: str = "kat-table-row#feedback-details"

    rating: str = ".col_feedback_rating kat-star-rating"
    rating_attribute: str = "value"
    order_link: str = ".col_order_id kat-link"
    order_attribute: str = "label"
    order_date: str = ".col_order_date"

    # Per-row action menu.  Its options live in the component's shadow root and
    # are addressed by ``data-action``, which is locale independent.
    actions_menu: str = ".col_actions kat-dropdown-button"
    action_request_removal: str = 'button[data-action="request_removal"]'

    # The removal flow is a side panel, not a modal — and, despite appearing
    # beside the row, it is NOT a descendant of that row: it renders into a
    # separate ``tr.feedback-details.panel-active`` that the component injects
    # while expanded.  So the panel is looked up page-wide, and the link back to
    # a specific row is established by opening from zero: no panel open, click
    # exactly one row's action, then assert exactly one panel exists.
    panel: str = "feedback-removal"
    panel_open: str = "feedback-removal .side-panel.side-panel-open"
    continue_button_pattern: str = "继续"
    category_dropdown: str = "kat-dropdown#category-dropdown"
    reason_dropdown: str = "kat-dropdown#reason-dropdown"
    submit_button: str = "kat-button#submit-button"


# ---------------------------------------------------------------------------
# Reason catalog
# ---------------------------------------------------------------------------
#
# Read off the live ``category-dropdown`` / ``reason-dropdown`` options.  The
# sub-reason codes are NOT contiguous — ``product-feedback`` starts at 202 and
# there is no 201 — so they are transcribed literally rather than generated.

REASON_CATALOG: dict[str, dict[str, str]] = {
    "inappropriate-feedback": {
        "101": "反馈助长了仇恨言论或歧视",
        "102": "反馈包含了淫秽或亵渎语言",
        "103": "反馈包含了个人识别信息",
        "104": "反馈包含无关的字符或符号",
        "105": "反馈是关于不同的订单",
    },
    "product-feedback": {
        "202": "反馈参考了其他卖家出售的商品",
        "203": "反馈是关于商品本身，而不是服务",
        "204": "反馈涉及其他品牌所有者的商品",
    },
    "delivery-related-feedback": {
        "401": "订单通过亚马逊配送服务配送",
        "402": "订单由亚马逊配送",
    },
    "not-listed": {
        "301": "其他",
    },
}

CATEGORY_LABELS: dict[str, str] = {
    "inappropriate-feedback": "不当反馈",
    "product-feedback": "商品反馈",
    "delivery-related-feedback": "配送相关反馈",
    "not-listed": "我的原因并未被列出",
}

# Amazon states it only reviews feedback for obscene language, seller-specific
# personal information, whole-comment-is-a-product-review, or FBA delivery and
# customer service.  Quoted from the live panel so the operator can see why a
# request may be declined even when the form accepts it.
AMAZON_REVIEW_CRITERIA: tuple[str, ...] = (
    "反馈包括淫秽语言。",
    "反馈包括卖家特定的个人识别信息。",
    "整个反馈评论全部是商品评论。",
    "如果反馈涉及亚马逊物流订单的配送或客户服务，则删除此反馈。",
)

MAX_RATING_ELIGIBLE_FOR_REMOVAL = 3


def is_known_reason(category: str, reason_code: str) -> bool:
    """Whitelist gate for anything a classifier proposes."""
    return reason_code in REASON_CATALOG.get(category, {})


def reason_label(category: str, reason_code: str) -> str:
    return REASON_CATALOG[category][reason_code]
