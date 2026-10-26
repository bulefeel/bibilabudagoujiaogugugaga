"""Observed contracts and bounded waits for inventory GPSR declarations."""

from dataclasses import dataclass
from pathlib import Path
import math
import tomllib
from typing import Any, Literal

INVENTORY_PATH = "/myinventory/inventory"
INVENTORY_DOMAIN = "sellercentral.amazon.de"
INVENTORY_MARKETPLACES = {
    "DE": "sellercentral.amazon.de", "IT": "sellercentral.amazon.it",
    "FR": "sellercentral.amazon.fr", "ES": "sellercentral.amazon.es",
    "PL": "sellercentral.amazon.pl",
}
MARKETPLACE_LABELS = {
    "DE": ("德国", "德國", "Germany", "Deutschland"),
    "IT": ("意大利", "義大利", "Italy", "Italia"),
    "FR": ("法国", "法國", "France"),
    "ES": ("西班牙", "Spain", "España"),
    "PL": ("波兰", "波蘭", "Poland", "Polska"),
}
SAFETY_ATTESTATION = "safety_attestation"
RESPONSIBLE_PERSON = "responsible_person"
MANUFACTURER = "manufacturer"
PARTY_TYPES = (RESPONSIBLE_PERSON, MANUFACTURER)
REQUIREMENT_TYPES = (SAFETY_ATTESTATION, *PARTY_TYPES)
REQUIREMENT_LABELS = {SAFETY_ATTESTATION: "安全证明", RESPONSIBLE_PERSON: "负责人信息", MANUFACTURER: "制造商信息"}
# Both list entry types were observed to open an identity-bound action panel.
# A suppressed listing can mix GPSR requirements with color/EPR editing; only
# the separately validated compliance branch is eligible for our submission.
ENTRY_LABELS = (
    "解决商品信息问题", "修正刊登資訊問題",
    "修复被禁止显示的商品信息", "修正限制刊登問題",
)
VIOLATION_LABELS = ("解决商品信息违规问题", "處理違規商品刊登資訊")
INVENTORY_URL = (
    f"https://{INVENTORY_DOMAIN}{INVENTORY_PATH}"
    "?fulfilledBy=all&page=1&pageSize=100&sort=date_created_desc&status=approval_required"
)
DECLARATION = (
    "如果您的产品没有任何警告和安全信息，请勾选“是”，"
    "因为即使没有此类信息，也可以安全地按预期使用。"
)
ATTESTATION_SAVE_LABELS = ("保存", "儲存")
SUBMISSION_RECEIPT = "您提交的内容正在审核中。将在七天内做出决定"
DECLARATIONS = (DECLARATION, "如果您的產品沒有任何警告和安全資訊，請勾選「是」，因為即使沒有此類資訊，也可以安全地按預期使用。")
SUBMISSION_RECEIPTS = (SUBMISSION_RECEIPT, "您提交的內容正在審核中。將在七天內做出決定")
ATTESTATION_REVIEW_RECEIPTS = (
    "您的提交正在审核中。七日内作出决定",
    "您的提交正在審核中。七日內作出決定",
)
RESPONSIBLE_PERSON_RECEIPT = "您的提交正在审核中。"
# Responsible-person and manufacturer receipts share exact wording variants.
# Keep these separate from safety declarations and editable submission forms.
RESPONSIBLE_PERSON_RECEIPTS = (
    RESPONSIBLE_PERSON_RECEIPT,
    "您的提交正在審核中。",
    "您提交的内容正在审核中。",
    "您提交的內容正在審核中。",
)
PANEL_SELECTOR = 'kat-panel[data-testid="kat-panel-ActionPanelContent"]'


@dataclass(frozen=True, slots=True)
class InventoryGpsrPolicy:
    """Seconds; injected once by application configuration, shared by all items."""

    list_timeout_seconds: float = 45.0
    panel_timeout_seconds: float = 45.0
    confirmation_timeout_seconds: float = 90.0
    poll_seconds: float = 1.0
    stable_seconds: float = 3.0
    action_interval_seconds: float = 3.0
    evidence_timeout_seconds: float = 5.0
    http_retry_attempts: int = 2
    http_retry_delay_seconds: float = 5.0
    click_timeout_seconds: float = 10.0
    http_diagnostic_timeout_seconds: float = 2.0
    csrf_reload_attempts: int = 1
    csrf_recovery_timeout_seconds: float = 90.0
    lazy_no_progress_seconds: float = 45.0
    lazy_page_timeout_seconds: float = 180.0
    announcement_timeout_seconds: float = 12.0
    announcement_stable_seconds: float = 1.0
    announcement_max_closes: int = 3
    # Menu readiness is shorter than business-content stability: a transient
    # flyout is not a sidebar. The overall budget remains list_timeout_seconds.
    marketplace_menu_stable_seconds: float = 1.0
    marketplace_menu_reopen_delay_seconds: float = 3.0
    marketplace_menu_reopen_attempts: int = 1
    scan_restart_attempts: int = 1
    scan_restart_delay_seconds: float = 15.0
    consecutive_read_failure_limit: int = 6
    no_progress_timeout_seconds: float = 600.0

    @classmethod
    def from_file(cls, path: Path) -> "InventoryGpsrPolicy":
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        fields = set(cls.__dataclass_fields__)
        if set(raw) != {"timing"} or not isinstance(raw["timing"], dict) or set(raw["timing"]) != fields:
            raise ValueError("Inventory policy must define exactly the documented timing fields")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in raw["timing"].values()):
            raise ValueError("Inventory policy seconds must be numeric")
        return cls(**raw["timing"])

    def __post_init__(self) -> None:
        if any(not math.isfinite(v) for v in (
            self.list_timeout_seconds, self.panel_timeout_seconds,
            self.confirmation_timeout_seconds, self.poll_seconds,
            self.stable_seconds, self.action_interval_seconds,
            self.evidence_timeout_seconds, self.http_retry_delay_seconds,
            self.click_timeout_seconds,
            self.http_diagnostic_timeout_seconds, self.csrf_recovery_timeout_seconds,
            self.lazy_no_progress_seconds, self.lazy_page_timeout_seconds,
            self.announcement_timeout_seconds, self.announcement_stable_seconds,
            self.marketplace_menu_stable_seconds, self.marketplace_menu_reopen_delay_seconds,
            self.scan_restart_delay_seconds, self.no_progress_timeout_seconds,
        )):
            raise ValueError("Inventory policy seconds must be finite")
        if min(self.list_timeout_seconds, self.panel_timeout_seconds,
               self.confirmation_timeout_seconds, self.poll_seconds,
               self.evidence_timeout_seconds, self.click_timeout_seconds,
               self.http_diagnostic_timeout_seconds, self.csrf_recovery_timeout_seconds) <= 0:
            raise ValueError("Inventory timeouts and polling interval must be positive")
        if not 0 < self.lazy_no_progress_seconds <= self.lazy_page_timeout_seconds:
            raise ValueError("Lazy-loading deadlines must be positive and ordered")
        if not 0 <= self.stable_seconds < min(
            self.list_timeout_seconds, self.panel_timeout_seconds,
            self.confirmation_timeout_seconds,
        ):
            raise ValueError("Inventory stable time must fit within every stage timeout")
        if type(self.http_retry_attempts) is not int or not 0 <= self.http_retry_attempts <= 2:
            raise ValueError("Inventory HTTP retries must be an integer between 0 and 2")
        if type(self.csrf_reload_attempts) is not int or not 0 <= self.csrf_reload_attempts <= 1:
            raise ValueError("Inventory CSRF reloads must be an integer between 0 and 1 per attempt")
        if self.http_retry_delay_seconds < 0:
            raise ValueError("Inventory HTTP retry delay must not be negative")
        if self.action_interval_seconds < 0:
            raise ValueError("Inventory action interval must not be negative")
        if not 0 <= self.announcement_stable_seconds < self.announcement_timeout_seconds:
            raise ValueError("Announcement stable time must fit within its positive timeout")
        if type(self.announcement_max_closes) is not int or not 1 <= self.announcement_max_closes <= 5:
            raise ValueError("Announcement close budget must be an integer between 1 and 5")
        if not 0 <= self.marketplace_menu_stable_seconds < self.list_timeout_seconds:
            raise ValueError("Marketplace menu stable time must fit within list timeout")
        if self.marketplace_menu_reopen_delay_seconds <= 0:
            raise ValueError("Marketplace menu reopen delay must be positive")
        if type(self.marketplace_menu_reopen_attempts) is not int or not 0 <= self.marketplace_menu_reopen_attempts <= 1:
            raise ValueError("Marketplace menu reopen budget must be zero or one")
        if type(self.scan_restart_attempts) is not int or not 0 <= self.scan_restart_attempts <= 2:
            raise ValueError("Inventory scan restart budget must be between zero and two")
        if self.scan_restart_delay_seconds <= 0 or self.no_progress_timeout_seconds <= 0:
            raise ValueError("Inventory recovery deadlines must be positive")
        if type(self.consecutive_read_failure_limit) is not int or not 1 <= self.consecutive_read_failure_limit <= 20:
            raise ValueError("Consecutive read failure limit must be between one and twenty")


InventoryTiming = InventoryGpsrPolicy


@dataclass(frozen=True, slots=True)
class InventoryItem:
    sku: str
    asin: str
    page_number: int
    position: int
    title: str = ""
    requirement_type: str = SAFETY_ATTESTATION


@dataclass(frozen=True, slots=True)
class InventoryPageSnapshot:
    page_number: int
    total_items: int
    items_per_page: int
    items: tuple[InventoryItem, ...]


@dataclass(frozen=True, slots=True)
class Inspection:
    status: Literal["eligible", "already_submitted", "no_violation_entry", "no_attestation",
                    "already_linked", "multiple_records", "no_records", "invalid_record", "not_applicable"]
    reason: str = ""
    page: Any = None
    initial_selected: bool | None = None
    # True only after the identity-bound requirements list was fully inspected.
    requirements_complete: bool = False
