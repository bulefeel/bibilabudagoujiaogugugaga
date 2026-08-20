"""Lock-protected repository used by unit tests and local demonstrations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping, Sequence

from .errors import InvalidTransition
from .state_machine import require_guard_transition, require_run_transition
from .types import (
    ApprovalRecord,
    ApprovalStatus,
    EvidenceRef,
    GuardState,
    MarketplaceRef,
    MarketplaceSnapshot,
    OperationIntent,
    OperationRecord,
    RunStatus,
    SiteStatus,
    WorkflowPlan,
    WorkflowRun,
    utc_now,
)


class InMemoryWorkflowRepository:
    """Faithfully models the atomic uniqueness required from SQLite."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.runs: dict[str, RunStatus] = {}
        self.sites: dict[tuple[str, str], SiteStatus] = {}
        self.plans: dict[str, WorkflowPlan] = {}
        self.approvals: dict[str, ApprovalRecord] = {}
        self.operations: dict[str, OperationRecord] = {}
        self.events: list[dict[str, Any]] = []
        self.evidence: list[tuple[str, str, EvidenceRef]] = []
        self.run_definitions: dict[str, WorkflowRun] = {}
        self.site_snapshots: dict[tuple[str, str], MarketplaceSnapshot] = {}

    async def add_run(self, run: WorkflowRun, status: RunStatus = RunStatus.QUEUED) -> None:
        async with self._lock:
            self.runs[run.id] = status
            self.run_definitions[run.id] = run
            for marketplace in run.marketplaces:
                self.sites[(run.id, marketplace.code)] = SiteStatus.PENDING

    async def get_run_status(self, run_id: str) -> RunStatus:
        async with self._lock:
            return self.runs[run_id]

    async def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        allowed_from: Sequence[RunStatus] | None = None,
        error: str | None = None,
    ) -> bool:
        del error
        async with self._lock:
            current = self.runs[run_id]
            if allowed_from is not None and current not in allowed_from:
                return False
            require_run_transition(current, status)
            self.runs[run_id] = status
            return True

    async def set_site_status(
        self,
        run_id: str,
        marketplace_code: str,
        status: SiteStatus,
        *,
        error: str | None = None,
    ) -> None:
        del error
        async with self._lock:
            self.sites[(run_id, marketplace_code)] = status

    async def get_site_status(
        self, run_id: str, marketplace_code: str
    ) -> SiteStatus | None:
        async with self._lock:
            return self.sites.get((run_id, marketplace_code))

    async def save_site_snapshot(
        self,
        run_id: str,
        marketplace: MarketplaceRef,
        snapshot: MarketplaceSnapshot,
    ) -> None:
        async with self._lock:
            if (run_id, marketplace.code) not in self.sites:
                raise LookupError(f"任务 {run_id} 不存在站点 {marketplace.code}")
            self.site_snapshots[(run_id, marketplace.code)] = snapshot

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        marketplace_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            self.events.append(
                {
                    "run_id": run_id,
                    "event_type": event_type,
                    "message": message,
                    "marketplace_code": marketplace_code,
                    "details": dict(details or {}),
                    "created_at": utc_now(),
                }
            )

    async def save_plan(self, plan: WorkflowPlan) -> None:
        async with self._lock:
            self.plans[plan.run_id] = plan

    async def get_plan(self, run_id: str) -> WorkflowPlan | None:
        async with self._lock:
            return self.plans.get(run_id)

    async def save_evidence(
        self, run_id: str, marketplace_code: str, evidence: EvidenceRef
    ) -> None:
        async with self._lock:
            self.evidence.append((run_id, marketplace_code, evidence))

    async def create_approval(
        self, plan: WorkflowPlan, *, expires_at: datetime
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            status=ApprovalStatus.PENDING,
            expires_at=expires_at,
            plan=plan.canonical(),
        )
        async with self._lock:
            self.approvals[plan.run_id] = record
        return record

    async def get_approval(self, run_id: str) -> ApprovalRecord | None:
        async with self._lock:
            return self.approvals.get(run_id)

    async def set_approval_status(
        self,
        run_id: str,
        status: ApprovalStatus,
        *,
        actor: str | None = None,
    ) -> ApprovalRecord | None:
        async with self._lock:
            old = self.approvals.get(run_id)
            if old is None:
                return None
            record = replace(
                old,
                status=status,
                approved_by=actor if status is ApprovalStatus.APPROVED else old.approved_by,
                approved_at=utc_now() if status is ApprovalStatus.APPROVED else old.approved_at,
            )
            self.approvals[run_id] = record
            return record

    async def arm_operation(self, intent: OperationIntent) -> OperationRecord:
        async with self._lock:
            existing = self.operations.get(intent.guard_key)
            if existing is not None:
                return replace(existing, created=False)
            record = OperationRecord(intent=intent, state=GuardState.ARMED, created=True)
            self.operations[intent.guard_key] = record
            return record

    async def get_operation(self, guard_key: str) -> OperationRecord | None:
        async with self._lock:
            return self.operations.get(guard_key)

    async def list_operations(
        self,
        run_id: str,
        *,
        states: Sequence[GuardState] | None = None,
    ) -> Sequence[OperationRecord]:
        wanted = set(states) if states is not None else None
        async with self._lock:
            return tuple(
                record
                for record in self.operations.values()
                if record.intent.run_id == run_id
                and (wanted is None or record.state in wanted)
            )

    async def transition_operation(
        self,
        guard_key: str,
        *,
        expected: Sequence[GuardState],
        target: GuardState,
        receipt_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> OperationRecord:
        async with self._lock:
            old = self.operations[guard_key]
            if old.state not in set(expected):
                raise InvalidTransition(
                    f"资金记录当前为 {old.state.value}，预期为 {[item.value for item in expected]}"
                )
            require_guard_transition(old.state, target)
            record = replace(
                old,
                state=target,
                created=False,
                receipt_id=receipt_id or old.receipt_id,
                last_reconciled_at=utc_now(),
                details={**dict(old.details), **dict(details or {})},
                # Stamped once, on the transition that follows the irreversible
                # click, mirroring the SQLAlchemy repository's column.
                submitted_at=(
                    old.submitted_at
                    if old.submitted_at is not None
                    else (utc_now() if target is GuardState.SUBMITTED else None)
                ),
            )
            self.operations[guard_key] = record
            return record

    async def release_operation(self, guard_key: str) -> bool:
        async with self._lock:
            record = self.operations.get(guard_key)
            if record is None or record.dispatch_recorded:
                return False
            if record.state not in (GuardState.ARMED, GuardState.UNCERTAIN):
                return False
            del self.operations[guard_key]
            return True


