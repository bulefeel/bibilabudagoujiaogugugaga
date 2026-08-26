"""Exceptions with explicit fail-closed meanings."""

from __future__ import annotations


class WorkflowError(RuntimeError):
    code = "workflow_error"


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

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


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
