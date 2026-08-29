"""Small, explicit value objects allowed to leave the local machine.

The workflow/database models intentionally do not appear in these objects.
Callers must select each field explicitly, which prevents a result-summary or
ORM dictionary from being serialised into a Feishu request by accident.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - avoids importing workflow code at runtime
    from ..workflows.types import WorkflowReport


class NotificationKind(StrEnum):
    """The only business events that may produce a Feishu card."""

    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_AUTH = "WAITING_AUTH"
    AUTH_TIMEOUT = "AUTH_TIMEOUT"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_PARTIAL = "RUN_PARTIAL"
    # A run in which every marketplace was skipped.  This used to send nothing
    # at all, which made Amazon's rolling 24-hour disbursement cap look exactly
    # like a crash: the browser opened, both sites were read, the browser
    # closed, and no card, log line or status ever said why.
    RUN_SKIPPED = "RUN_SKIPPED"
    UNCERTAIN_FINANCIAL = "UNCERTAIN_FINANCIAL"
    CROSS_DAY_STARTED = "CROSS_DAY_STARTED"


@dataclass(frozen=True, slots=True)
class SafeSiteNotice:
    """A deliberately small, already-selected site result.

    ``account_tail`` and ``reference_suffix`` may be supplied in their original
    form by a builder.  The renderer still takes only their final four
    characters as a defence-in-depth measure.
    """

    code: str
    currency: str = ""
    payable: Decimal | int | float | str | None = None
    delayed: Decimal | int | float | str | None = None
    outcome: str = ""
    account_tail: str = ""
    reference_suffix: str = ""
    # Why this site ended the way it did, in the operator's own language.
    # A bare outcome cannot distinguish "no money to move" from "Amazon is
    # throttling us" from "the payout account no longer matches" — all three
    # rendered as SKIPPED/失败 and left the operator guessing, or guessing wrong.
    reason: str = ""
    # True when Amazon showed a different payout tail than the previous payout
    # for this same store+marketplace.  Reported, never acted on: the operator
    # decides whether that matters, and the payout has already gone out.
    account_changed: bool = False


@dataclass(frozen=True, slots=True)
class SafeFeedbackNotice:
    """One feedback entry, as it appears on a card.

    ``comment`` is buyer wording, and this module otherwise refuses to carry
    free text to Feishu at all.  It is here because the operator asked to see
    *which* review was submitted — a count alone cannot be checked against the
    seller account.  The builder truncates it and the renderer still runs it
    through the same redaction pass as every other operator-facing string.
    """

    order_id: str
    rating: int
    comment: str = ""
    reason_label: str = ""
    state: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.rating, int):
            object.__setattr__(self, "rating", int(self.rating))


@dataclass(frozen=True, slots=True)
class SafeRunNotice:
    """Whitelist DTO used by all external notification renderers."""

    kind: NotificationKind
    title: str
    summary: str
    run_short_id: str
    store_name: str
    # The code-registered workflow key.  Renderers need it because the default
    # card copy is written for payouts; a run that moves no money must not be
    # told to "以绿色提现结果已确认通知为准".  It is a fixed identifier from the
    # registry, never operator or page data.
    workflow: str = ""
    mode: str = ""
    trigger: str = ""
    schedule_name: str = ""
    scheduled_for: datetime | None = None
    started_at: datetime | None = None
    queue_delay_minutes: int | None = None
    next_action: str = ""
    sites: tuple[SafeSiteNotice, ...] = field(default_factory=tuple)
    deadline_at: datetime | None = None
    # Present only for the feedback workflow; empty for every payout notice.
    feedback_items: tuple[SafeFeedbackNotice, ...] = field(default_factory=tuple)
    # How many entries were left out of ``feedback_items`` by the card's cap.
    feedback_omitted: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NotificationKind):
            object.__setattr__(self, "kind", NotificationKind(self.kind))
        if not isinstance(self.sites, tuple):
            object.__setattr__(self, "sites", tuple(self.sites))
        if any(not isinstance(site, SafeSiteNotice) for site in self.sites):
            raise TypeError("sites must contain only SafeSiteNotice values")
        if not isinstance(self.feedback_items, tuple):
            object.__setattr__(self, "feedback_items", tuple(self.feedback_items))
        if any(
            not isinstance(item, SafeFeedbackNotice) for item in self.feedback_items
        ):
            raise TypeError(
                "feedback_items must contain only SafeFeedbackNotice values"
            )
        for name in ("scheduled_for", "started_at", "deadline_at"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, datetime):
                raise TypeError(f"{name} must be a datetime or None")
        if self.queue_delay_minutes is not None:
            delay = int(self.queue_delay_minutes)
            object.__setattr__(self, "queue_delay_minutes", max(0, delay))
        # A full UUID/hash is an internal identifier and must not leave the
        # workstation.  The short prefix remains enough to find the local run.
        object.__setattr__(self, "run_short_id", str(self.run_short_id)[:8])

    @classmethod
    def from_workflow_report(
        cls, report: "WorkflowReport"
    ) -> "SafeRunNotice | None":
        """Convert the legacy report through an explicit compatibility list.

        A generic success without a confirmed operation becomes a neutral
        completion notice.  It is deliberately distinct from the green
        payment-confirmation notice.  Unknown keys in ``report.fields`` are
        never copied.
        """

        from ..workflows.types import RunStatus

        fields = report.fields
        raw_operations = fields.get("operations", ())
        operations = raw_operations if isinstance(raw_operations, (list, tuple)) else ()
        confirmed = any(
            isinstance(item, Mapping) and str(item.get("state", "")).upper() == "CONFIRMED"
            for item in operations
        )

        kind_by_status = {
            RunStatus.WAITING_APPROVAL: NotificationKind.WAITING_APPROVAL,
            RunStatus.WAITING_AUTH: NotificationKind.WAITING_AUTH,
            RunStatus.NEEDS_HUMAN_AUTH: NotificationKind.AUTH_TIMEOUT,
            RunStatus.UNCERTAIN_FINANCIAL: NotificationKind.UNCERTAIN_FINANCIAL,
            RunStatus.FAILED: NotificationKind.RUN_FAILED,
            RunStatus.PARTIAL: NotificationKind.RUN_PARTIAL,
            RunStatus.SKIPPED: NotificationKind.RUN_SKIPPED,
        }
        if report.status is RunStatus.SUCCEEDED:
            kind = (
                NotificationKind.PAYMENT_CONFIRMED
                if confirmed
                else NotificationKind.RUN_COMPLETED
            )
        else:
            kind = kind_by_status.get(report.status)
            if kind is None:
                return None

        sites: list[SafeSiteNotice] = []
        seen: set[str] = set()
        for item in operations:
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("marketplace", "")).strip().upper()
            if not code:
                continue
            seen.add(code)
            sites.append(
                SafeSiteNotice(
                    code=code,
                    currency=str(item.get("currency", "")),
                    payable=item.get("amount"),
                    delayed=item.get("delayed_amount"),
                    outcome=str(item.get("state", "")),
                    account_tail=str(item.get("account_tail", "")),
                    reference_suffix=str(item.get("reference", "")),
                )
            )
        raw_marketplaces = fields.get("marketplaces", ())
        marketplaces = (
            raw_marketplaces if isinstance(raw_marketplaces, (list, tuple)) else ()
        )
        for value in marketplaces:
            code = str(value).strip().upper()
            if code and code not in seen:
                sites.append(SafeSiteNotice(code=code))

        is_neutral_completion = kind is NotificationKind.RUN_COMPLETED
        return cls(
            kind=kind,
            title=(
                "任务检查已完成（非提现确认）"
                if is_neutral_completion
                else report.title
            ),
            summary=(
                "流程已完成，但未发现已确认的资金提交记录；这不代表提现成功。"
                if is_neutral_completion
                else report.summary
            ),
            run_short_id=str(report.run_id)[:8],
            store_name=str(fields.get("store", "")),
            mode=str(fields.get("mode", "")),
            trigger=str(fields.get("schedule_trigger", fields.get("trigger", ""))),
            schedule_name=str(fields.get("schedule_name", "")),
            scheduled_for=_datetime_or_none(fields.get("scheduled_for")),
            started_at=_datetime_or_none(fields.get("started_at")),
            queue_delay_minutes=_non_negative_int(fields.get("queue_delay_minutes")),
            next_action=str(fields.get("next_action", "")),
            sites=tuple(sites),
            deadline_at=_datetime_or_none(fields.get("deadline_at")),
        )


def _datetime_or_none(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _non_negative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None
