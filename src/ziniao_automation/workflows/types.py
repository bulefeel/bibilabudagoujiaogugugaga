"""Stable value objects shared by workflow plugins and the web/API layer.

The workflow package intentionally does not import SQLAlchemy or FastAPI.  The
same deterministic engine can therefore be exercised with an in-memory
repository and later wired to SQLite by a small adapter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Mapping

from ziniao_automation.presentation_time import to_display_datetime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunMode(StrEnum):
    DRY_RUN = "dry_run"
    APPROVAL = "approval"
    AUTO = "auto"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_AUTH = "WAITING_AUTH"
    RECONCILING = "RECONCILING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_HUMAN_AUTH = "NEEDS_HUMAN_AUTH"
    UNCERTAIN_FINANCIAL = "UNCERTAIN_FINANCIAL"
    SKIPPED = "SKIPPED"


class SiteStatus(StrEnum):
    PENDING = "PENDING"
    PREFLIGHT = "PREFLIGHT"
    PLANNED = "PLANNED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_AUTH = "WAITING_AUTH"
    ARMED = "ARMED"
    SUBMITTED = "SUBMITTED"
    RECONCILING = "RECONCILING"
    CONFIRMED = "CONFIRMED"
    DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
    SKIPPED = "SKIPPED"
    NEEDS_HUMAN_AUTH = "NEEDS_HUMAN_AUTH"
    UNCERTAIN_FINANCIAL = "UNCERTAIN_FINANCIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class GuardState(StrEnum):
    ARMED = "ARMED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    UNCERTAIN = "UNCERTAIN"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class ReconcileStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class StoreRef:
    id: str
    name: str
    selector_type: Literal["oauth", "id"]
    selector_value: str
    expected_seller_id: str
    enabled: bool = True
    identity_confirmed: bool = False

    @property
    def browser_oauth(self) -> str | None:
        """Compatibility read; numeric profile IDs are never inferred."""
        return self.selector_value if self.selector_type == "oauth" else None


@dataclass(frozen=True, slots=True)
class MarketplaceRef:
    id: str
    code: str
    domain: str
    currency: str
    # Deliberately carries no payout-account baseline.  Amazon owns where the
    # money goes; this automation only presses Request disbursement and never
    # selects or edits a destination, so a locally stored "expected" tail could
    # only ever block a payout, never redirect one.  The tail is still read and
    # recorded per operation (see OperationIntent.payout_account_tail) as an
    # audit trail.
    enabled: bool = True
    payments_path: str = "/payments/dashboard/index.html"

    @property
    def payments_url(self) -> str:
        path = self.payments_path if self.payments_path.startswith("/") else f"/{self.payments_path}"
        return f"https://{self.domain}{path}"


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    id: str
    workflow: str
    mode: RunMode
    store: StoreRef
    marketplaces: tuple[MarketplaceRef, ...]
    workflow_config: Mapping[str, Any] = field(default_factory=dict)
    workflow_config_version: int = 1
    requested_by: str = "admin"
    artifact_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class PreflightResult:
    marketplace_code: str
    seller_id: str
    payment_account: str
    observed_domain: str
    contract_version: str


@dataclass(frozen=True, slots=True)
class MarketplaceSnapshot:
    marketplace_code: str
    domain: str
    seller_id: str
    payment_account: str
    currency: str
    payable_amount: Decimal
    delayed_amount: Decimal
    settlement_key: str
    can_submit: bool
    contract_version: str
    page_fingerprint: str
    skip_reason: str | None = None
    identity_source: str = "unknown"
    observed_at: datetime = field(default_factory=utc_now, compare=False)

    def canonical(self) -> dict[str, Any]:
        """Fields that must remain identical between approval and execution.

        ``payment_account`` is deliberately absent.  It is empty when read from
        the dashboard and holds the observed tail only after the confirmation
        page is open, so binding it would make every run abort at the very step
        that first learns the value.
        """
        return {
            "marketplace_code": self.marketplace_code.upper(),
            "domain": self.domain.lower(),
            "seller_id": self.seller_id,
            "currency": self.currency.upper(),
            "payable_amount": decimal_text(self.payable_amount),
            "delayed_amount": decimal_text(self.delayed_amount),
            "settlement_key": self.settlement_key,
            "can_submit": self.can_submit,
            "contract_version": self.contract_version,
            "page_fingerprint": self.page_fingerprint,
            "skip_reason": self.skip_reason,
            "identity_source": self.identity_source,
        }

    @property
    def snapshot_hash(self) -> str:
        return stable_hash(self.canonical())

    @property
    def binding_hash(self) -> str:
        """What must stay identical between planning and execution.

        Deliberately excludes the two money figures.  A payable balance keeps
        growing while a run works — more orders settle — and the amount is an
        *observation*, not a control input: pressing Request disbursement
        transfers whatever Amazon considers payable at that instant, which its
        own note on the confirmation page says may differ from the displayed
        balance.  Binding the plan to that number made an ordinary, expected
        change abort the run.

        Everything that decides *which account* is being operated stays bound:
        seller, marketplace, domain, currency, settlement cycle, submit-ability,
        contract version and the page's structural fingerprint — which is
        already amount-free because ``_fingerprint`` strips digits and text
        before hashing.
        """

        payload = {
            key: value
            for key, value in self.canonical().items()
            if key not in ("payable_amount", "delayed_amount")
        }
        return stable_hash(payload)


@dataclass(frozen=True, slots=True)
class PlanLine:
    marketplace: MarketplaceRef
    snapshot: MarketplaceSnapshot
    screenshot_path: str | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            **self.snapshot.canonical(),
            "marketplace_id": self.marketplace.id,
        }


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    workflow: str
    run_id: str
    store_id: str
    lines: tuple[PlanLine, ...]
    created_at: datetime = field(default_factory=utc_now, compare=False)

    def canonical(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "run_id": self.run_id,
            "store_id": self.store_id,
            "lines": [
                line.canonical()
                for line in sorted(self.lines, key=lambda item: item.marketplace.code)
            ],
        }

    @property
    def plan_hash(self) -> str:
        return stable_hash(self.canonical())


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    run_id: str
    plan_hash: str
    status: ApprovalStatus
    expires_at: datetime
    plan: Mapping[str, Any]
    approved_by: str | None = None
    approved_at: datetime | None = None

    @property
    def usable(self) -> bool:
        return (
            self.status is ApprovalStatus.APPROVED
            and self.expires_at > utc_now()
        )


@dataclass(frozen=True, slots=True)
class OperationIntent:
    guard_key: str
    run_id: str
    site_run_key: str
    store_id: str
    marketplace_code: str
    settlement_key: str
    amount: Decimal
    currency: str
    plan_hash: str
    snapshot_hash: str
    # The masked tail Amazon showed on the confirmation page for THIS payout.
    # Recorded, never compared: it is the only durable answer to "where did
    # that money go", and it is worth having if a transfer is ever disputed.
    payout_account_tail: str = ""


@dataclass(frozen=True, slots=True)
class OperationRecord:
    intent: OperationIntent
    state: GuardState
    created: bool = False
    receipt_id: str | None = None
    last_reconciled_at: datetime | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    # Set the moment the guard is moved to SUBMITTED, i.e. only after the sole
    # irreversible click returned.  ``None`` therefore means this system never
    # recorded a dispatch for this operation — the difference between "the
    # payout is in flight and Amazon has not published it yet" and "we do not
    # know that anything was ever sent".  Reporting and release both hinge on
    # it, so it is read from the column rather than inferred from metadata.
    submitted_at: datetime | None = None

    @property
    def dispatch_recorded(self) -> bool:
        return self.submitted_at is not None


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    submitted_at: datetime
    receipt_id: str | None = None
    observed_status: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    status: ReconcileStatus
    checked_at: datetime
    platform_reference: str | None = None
    platform_status: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    file_path: str
    sha256: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowReport:
    run_id: str
    status: RunStatus
    title: str
    summary: str
    fields: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PrepareResult:
    run_id: str
    status: RunStatus
    plan: WorkflowPlan


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    run_id: str
    status: RunStatus
    operations: tuple[OperationRecord, ...] = ()


def decimal_text(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.01"))
    return format(normalized, "f")


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def disbursement_day_key(moment: datetime | None = None) -> str:
    """The operator-local calendar day that owns one disbursement.

    Persistence is UTC, but "one payout per day" is a statement about the
    operator's day, and the UTC boundary falls at 08:00 in the UTC+8 zone this
    tool runs in — right between two morning schedules.
    """

    local = to_display_datetime(moment or utc_now())
    assert local is not None  # to_display_datetime only returns None for None
    return local.date().isoformat()


def operation_guard_key(
    *,
    workflow: str,
    store_id: str,
    marketplace_code: str,
    settlement_key: str,
    disbursement_date: str,
) -> str:
    # Deliberately excludes run_id: a newly-created run cannot submit the same
    # store/site/day for a second time.
    #
    # ``disbursement_date`` is part of the key because a settlement cycle stays
    # open for weeks while funds keep accumulating, and a seller may legitimately
    # request a payout on each of those days.  Keying on the cycle alone let the
    # first payout of a cycle through and then failed every later run with
    # "资金防重复记录已存在" — which is not what a weekday schedule means.  Per
    # day also matches the platform-side idempotency check, which already uses
    # ``require_today=True``; the two halves of the duplicate protection were
    # previously at different granularities.
    return stable_hash(
        {
            "workflow": workflow,
            "store_id": store_id,
            "marketplace_code": marketplace_code.upper(),
            "settlement_key": settlement_key,
            "disbursement_date": disbursement_date,
        }
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
