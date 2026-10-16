"""Exceptions with explicit fail-closed meanings."""

from __future__ import annotations

from .setup_diagnostics import SetupDiagnostic


class WorkflowError(RuntimeError):
    code = "workflow_error"


class PageRecoveryRequired(WorkflowError):
    """Inventory UI is unavailable; retain progress until an explicit resume."""

    code = "page_recovery_required"

    def __init__(self, message: str, *, http_status: int | None = None,
                 reason_code: str = "inventory_page_unavailable") -> None:
        if reason_code == "inventory_page_unavailable":
            if type(http_status) is not int or (http_status not in {403, 429} and not 500 <= http_status <= 599):
                raise ValueError("Unsupported recoverable inventory HTTP status")
        elif reason_code not in {"inventory_scan_unstable", "inventory_no_progress"} or http_status is not None:
            raise ValueError("Unsupported inventory recovery reason")
        super().__init__(message)
        self.http_status = http_status
        self.reason_code = reason_code

    @property
    def public_reason(self) -> str:
        """Fixed diagnostic text, never raw exception/response contents."""
        if self.reason_code == "inventory_scan_unstable":
            return "库存扫描数量持续变化，有限重建清单未完成"
        if self.reason_code == "inventory_no_progress":
            return "库存连续读取失败或长时间无有效进展"
        return f"库存页面返回 HTTP {self.http_status}"


class InvalidTransition(WorkflowError):
    code = "invalid_transition"


class WorkflowNotRegistered(WorkflowError, LookupError):
    """A code allow-list lookup miss.

    It remains a WorkflowError for engine callers and is also a LookupError so
    repository/scheduler boundaries can normalize it without importing the
    eager ``workflows`` package (which would create a circular import).
    """

    code = "workflow_not_registered"


class PreflightRejected(WorkflowError):
    code = "preflight_rejected"


class DomContractError(PreflightRejected):
    code = "dom_contract_error"


class PayoutRateLimited(PreflightRejected):
    """Amazon itself refuses another payout for this account right now.

    On-demand disbursement is capped at once per rolling 24 hours, and the cap
    is disclosed only on the confirmation page — the dashboard's button stays
    enabled.  This is an ordinary, expected outcome like a zero balance, not a
    contract violation: the site is skipped and tried again next run.
    """

    code = "payout_rate_limited"

    def __init__(self, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SubmissionNotDispatched(DomContractError):
    """The sole irreversible payout click provably never happened.

    ``submit_once`` records a page-scoped consumption marker on the line
    immediately above ``click()``.  Any failure raised while that marker is
    still unset therefore happened before the click, on straight-line code with
    nothing concurrent in between — a fact the caller can audit rather than
    infer.  It is the only signal that permits releasing an ARMED guard: the
    guard would otherwise record something that did not occur, block a same-day
    retry, and make the next read-back report a payout nobody ever requested.
    """

    code = "submission_not_dispatched"


class HumanAuthRequired(WorkflowError):
    code = "human_auth_required"

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        dispatch_recorded: bool | None = None,
        reason_code: str = "AUTH_UNKNOWN",
        stage: str = "unknown",
        action: str | None = None,
        attempts: int | None = None,
        candidate_count: int | None = None,
        page_category: str = "UNKNOWN_PAGE",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        if reason_code == "AUTH_UNKNOWN" and kind == "marketplace_switch":
            reason_code = "MARKETPLACE_UNKNOWN"
        self.diagnostic = SetupDiagnostic(
            reason_code=reason_code, stage=stage, action=action,
            attempts=attempts, candidate_count=candidate_count,
            page_category=page_category,
        )
        # 这一下不可逆点击到底点没点下去。
        #
        # ``None`` = 不知道(默认,也是**唯一安全的默认**):调用方一个字都不许动那条
        # 资金记录。只有从 ``submit_once`` 里抛出来时才会被填成真/假 —— 那里有
        # ``consumed`` 标记,它就写在 ``click()`` 的上一行,是本仓库对「点没点」
        # 唯一有凭据的答案。``False`` 才允许撤销 ARMED 锁定(2026-09-26 自审:
        # 原来拿 ``submitted_at`` 是否为空当判据,而它是**点击之后**才写的)。
        self.dispatch_recorded = dispatch_recorded


class ApprovalRequired(WorkflowError):
    code = "approval_required"


class ApprovalExpired(WorkflowError):
    code = "approval_expired"


class PlanChanged(WorkflowError):
    code = "plan_changed"


class DuplicateFinancialOperation(WorkflowError):
    code = "duplicate_financial_operation"


class PlatformDisbursementExists(WorkflowError):
    """A matching same-day platform record makes this site a normal skip."""

    code = "platform_disbursement_exists"

    def __init__(self, message: str, *, result: object) -> None:
        super().__init__(message)
        self.result = result


class SubmissionAlreadyConsumed(WorkflowError):
    code = "submission_already_consumed"


class FinancialStateUncertain(WorkflowError):
    code = "uncertain_financial"
