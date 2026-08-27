"""Pure transition tables for runs, sites and financial guards."""

from __future__ import annotations

from collections.abc import Mapping, Set
from enum import StrEnum
from typing import TypeVar

from .errors import InvalidTransition
from .types import GuardState, RunStatus, SiteStatus

E = TypeVar("E", bound=StrEnum)


RUN_TRANSITIONS: Mapping[RunStatus, Set[RunStatus]] = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.SKIPPED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_AUTH,
        RunStatus.RECONCILING,
        RunStatus.SUCCEEDED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.NEEDS_HUMAN_AUTH,
        RunStatus.UNCERTAIN_FINANCIAL,
        RunStatus.CANCELLED,
        # A run that opened the browser, read every marketplace and found
        # nothing to send — a zero balance, or Amazon's rolling 24-hour cap on
        # all of them.  ``_finish_operations`` has always computed this, and
        # the SQLAlchemy repository has always written it (it gates on
        # ``allowed_from`` rather than this table), so the omission here only
        # ever showed up as in-memory tests disagreeing with production.
        RunStatus.SKIPPED,
    },
    RunStatus.WAITING_APPROVAL: {
        # Approval is persisted first, then the durable single worker claims
        # QUEUED -> RUNNING when it reaches this item.
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.CANCELLED,
        RunStatus.WAITING_AUTH,
    },
    RunStatus.WAITING_AUTH: {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.RECONCILING,
        RunStatus.NEEDS_HUMAN_AUTH,
        RunStatus.UNCERTAIN_FINANCIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.RECONCILING: {
        RunStatus.WAITING_AUTH,
        RunStatus.SUCCEEDED,
        RunStatus.PARTIAL,
        RunStatus.UNCERTAIN_FINANCIAL,
        RunStatus.FAILED,
    },
    RunStatus.NEEDS_HUMAN_AUTH: {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.RECONCILING,
        RunStatus.CANCELLED,
    },
    RunStatus.SUCCEEDED: set(),
    RunStatus.PARTIAL: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
    RunStatus.UNCERTAIN_FINANCIAL: {
        RunStatus.RECONCILING,
        # Manual close-out. Read-back is bounded to about a minute while Amazon
        # often publishes a disbursement only the next day, so a run could stay
        # here for ever: it kept blocking its schedule, and settling each guard
        # by hand fixed the money question without ever touching the run. These
        # three are reachable only once every guard on the run is terminal.
        RunStatus.SUCCEEDED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
    },
    RunStatus.SKIPPED: set(),
}

SITE_TRANSITIONS: Mapping[SiteStatus, Set[SiteStatus]] = {
    SiteStatus.PENDING: {
        SiteStatus.PREFLIGHT,
        SiteStatus.SKIPPED,
        SiteStatus.CANCELLED,
    },
    SiteStatus.PREFLIGHT: {
        SiteStatus.PLANNED,
        SiteStatus.WAITING_AUTH,
        SiteStatus.FAILED,
        SiteStatus.SKIPPED,
    },
    SiteStatus.PLANNED: {
        SiteStatus.WAITING_APPROVAL,
        SiteStatus.ARMED,
        SiteStatus.DRY_RUN_COMPLETE,
        SiteStatus.SKIPPED,
        SiteStatus.FAILED,
    },
    SiteStatus.WAITING_APPROVAL: {
        SiteStatus.PREFLIGHT,
        SiteStatus.CANCELLED,
    },
    SiteStatus.WAITING_AUTH: {
        SiteStatus.PREFLIGHT,
        SiteStatus.NEEDS_HUMAN_AUTH,
        SiteStatus.CANCELLED,
    },
    SiteStatus.ARMED: {
        SiteStatus.SUBMITTED,
        SiteStatus.RECONCILING,
        SiteStatus.UNCERTAIN_FINANCIAL,
    },
    SiteStatus.SUBMITTED: {
        SiteStatus.RECONCILING,
        SiteStatus.CONFIRMED,
        SiteStatus.UNCERTAIN_FINANCIAL,
    },
    SiteStatus.RECONCILING: {
        SiteStatus.CONFIRMED,
        SiteStatus.UNCERTAIN_FINANCIAL,
    },
    SiteStatus.UNCERTAIN_FINANCIAL: {SiteStatus.RECONCILING},
    SiteStatus.CONFIRMED: set(),
    SiteStatus.DRY_RUN_COMPLETE: set(),
    SiteStatus.SKIPPED: set(),
    SiteStatus.NEEDS_HUMAN_AUTH: {SiteStatus.PREFLIGHT, SiteStatus.CANCELLED},
    SiteStatus.FAILED: set(),
    SiteStatus.CANCELLED: set(),
}

GUARD_TRANSITIONS: Mapping[GuardState, Set[GuardState]] = {
    GuardState.ARMED: {
        GuardState.SUBMITTED,
        GuardState.CONFIRMED,
        GuardState.UNCERTAIN,
    },
    GuardState.SUBMITTED: {GuardState.CONFIRMED, GuardState.UNCERTAIN},
    GuardState.UNCERTAIN: {GuardState.CONFIRMED, GuardState.UNCERTAIN},
    GuardState.CONFIRMED: set(),
}


def require_transition(current: E, target: E, table: Mapping[E, Set[E]]) -> None:
    if current == target:
        return
    if target not in table.get(current, set()):
        raise InvalidTransition(f"状态不允许从 {current.value} 变为 {target.value}")


def require_run_transition(current: RunStatus, target: RunStatus) -> None:
    require_transition(current, target, RUN_TRANSITIONS)


def require_site_transition(current: SiteStatus, target: SiteStatus) -> None:
    require_transition(current, target, SITE_TRANSITIONS)


def require_guard_transition(current: GuardState, target: GuardState) -> None:
    require_transition(current, target, GUARD_TRANSITIONS)
