"""Ports used by workflow plugins.

Concrete browser, SQLite and notification implementations live outside this
module.  Tests use small fakes, which keeps every financial transition fully
deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncContextManager, Protocol, runtime_checkable

try:
    from ziniao_automation.ziniao.models import ProfileSelector
except ImportError:  # pragma: no cover - supports isolated type checking
    ProfileSelector = Any  # type: ignore[misc,assignment]

from .types import (
    ApprovalRecord,
    ApprovalStatus,
    EvidenceRef,
    GuardState,
    MarketplaceRef,
    MarketplaceSnapshot,
    OperationIntent,
    OperationRecord,
    PreflightResult,
    ReconcileResult,
    RunStatus,
    SiteStatus,
    SubmissionReceipt,
    WorkflowPlan,
    WorkflowReport,
    WorkflowRun,
)


@runtime_checkable
class BrowserHandle(Protocol):
    page: Any
    debugging_host: str
    debugging_port: int


@runtime_checkable
class BrowserSessionProvider(Protocol):
    def session(
        self, selector: ProfileSelector, store_key: str | None = None
    ) -> AsyncContextManager[BrowserHandle]: ...


@runtime_checkable
class FinancialSessionProvider(Protocol):
    def financial_session(
        self, selector: ProfileSelector, store_key: str | None = None
    ) -> AsyncContextManager[BrowserHandle]: ...

    async def wait_for_auth(
        self,
        handle: BrowserHandle,
        auth_key: str,
        *,
        timeout_seconds: float = 1800.0,
    ) -> BrowserHandle: ...

    async def continue_auth(self, auth_key: str) -> bool: ...

    async def cancel_auth(self, auth_key: str) -> bool: ...


@runtime_checkable
class MarketplacePageAdapter(Protocol):
    async def preflight(
        self, page: Any, run: WorkflowRun, marketplace: MarketplaceRef
    ) -> PreflightResult: ...

    async def read_snapshot(
        self, page: Any, run: WorkflowRun, marketplace: MarketplaceRef
    ) -> MarketplaceSnapshot: ...

    async def capture_evidence(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        *,
        label: str,
    ) -> EvidenceRef | None: ...

    async def submit_once(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        expected: MarketplaceSnapshot,
    ) -> SubmissionReceipt: ...

    async def open_confirmation(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        expected: MarketplaceSnapshot,
    ) -> MarketplaceSnapshot: ...

    async def lookup_existing(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        expected: MarketplaceSnapshot,
    ) -> ReconcileResult: ...

    async def reconcile(
        self,
        page: Any,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        operation: OperationRecord,
    ) -> ReconcileResult: ...


@runtime_checkable
class WorkflowRepository(Protocol):
    async def get_run_status(self, run_id: str) -> RunStatus: ...

    async def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        allowed_from: Sequence[RunStatus] | None = None,
        error: str | None = None,
    ) -> bool: ...

    async def set_site_status(
        self,
        run_id: str,
        marketplace_code: str,
        status: SiteStatus,
        *,
        error: str | None = None,
    ) -> None: ...

    async def save_site_snapshot(
        self,
        run_id: str,
        marketplace: MarketplaceRef,
        snapshot: MarketplaceSnapshot,
    ) -> None:
        """Persist read-only financial facts without making a line actionable."""
        ...

    async def get_site_status(
        self, run_id: str, marketplace_code: str
    ) -> SiteStatus | None: ...

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        marketplace_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def save_plan(self, plan: WorkflowPlan) -> None: ...

    async def get_plan(self, run_id: str) -> WorkflowPlan | None: ...

    async def save_evidence(
        self, run_id: str, marketplace_code: str, evidence: EvidenceRef
    ) -> None: ...

    async def create_approval(
        self, plan: WorkflowPlan, *, expires_at: datetime
    ) -> ApprovalRecord: ...

    async def get_approval(self, run_id: str) -> ApprovalRecord | None: ...

    async def set_approval_status(
        self,
        run_id: str,
        status: ApprovalStatus,
        *,
        actor: str | None = None,
    ) -> ApprovalRecord | None: ...

    async def arm_operation(self, intent: OperationIntent) -> OperationRecord: ...

    async def get_operation(self, guard_key: str) -> OperationRecord | None: ...

    async def list_operations(
        self,
        run_id: str,
        *,
        states: Sequence[GuardState] | None = None,
    ) -> Sequence[OperationRecord]: ...

    async def transition_operation(
        self,
        guard_key: str,
        *,
        expected: Sequence[GuardState],
        target: GuardState,
        receipt_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> OperationRecord: ...

    async def release_operation(self, guard_key: str) -> bool: ...


@runtime_checkable
class Notifier(Protocol):
    async def send(self, report: WorkflowReport) -> None: ...


@runtime_checkable
class Workflow(Protocol):
    name: str

    async def preflight(self, run: WorkflowRun, page: Any) -> None: ...

    async def plan(self, run: WorkflowRun, page: Any) -> WorkflowPlan: ...

    async def execute(
        self, run: WorkflowRun, page: Any, plan: WorkflowPlan
    ) -> Sequence[OperationRecord]: ...

    async def reconcile(
        self,
        run: WorkflowRun,
        page: Any,
        operations: Sequence[OperationRecord] | None = None,
    ) -> Sequence[OperationRecord]: ...

    async def report(self, run: WorkflowRun, status: RunStatus) -> WorkflowReport: ...
