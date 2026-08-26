"""Async-shaped adapter over the application's synchronous SQLAlchemy store.

Every method owns a short transaction.  In particular ``arm_operation``
commits SQLite before it returns, so workflow code cannot reach the platform
click while ARMED only exists in memory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from ziniao_automation.models import (
    ApprovalRequest,
    OperationGuard,
    Run,
    SiteRun,
    StoreMarketplace,
)
from ziniao_automation.repositories import WorkflowRepository as DbWorkflowRepository

from .errors import InvalidTransition
from .types import (
    ApprovalRecord,
    ApprovalStatus,
    EvidenceRef,
    GuardState,
    MarketplaceRef,
    MarketplaceSnapshot,
    OperationIntent,
    OperationRecord,
    PlanLine,
    RunStatus,
    SiteStatus,
    WorkflowPlan,
    utc_now,
)


class SqlAlchemyWorkflowRepository:
    """Implements :class:`workflows.contracts.WorkflowRepository`."""

    _PLAN_KEY = "workflow_plan_v1"

    def __init__(self, session_factory: sessionmaker[Session] | Any) -> None:
        self.session_factory = session_factory

    async def get_run_status(self, run_id: str) -> RunStatus:
        with self.session_factory() as session:
            value = session.scalar(select(Run.status).where(Run.id == run_id))
            if value is None:
                raise LookupError(f"任务 {run_id} 不存在")
            return RunStatus(value)

    async def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        allowed_from: Sequence[RunStatus] | None = None,
        error: str | None = None,
    ) -> bool:
        with self.session_factory() as session:
            db = DbWorkflowRepository(session)
            allowed = tuple(item.value for item in allowed_from) if allowed_from else (
                str(session.scalar(select(Run.status).where(Run.id == run_id))),
            )
            changed = db.set_run_status(
                run_id,
                status.value,
                allowed_from=allowed,
                error=error,
            )
            session.commit()
            return changed

    async def set_site_status(
        self,
        run_id: str,
        marketplace_code: str,
        status: SiteStatus,
        *,
        error: str | None = None,
    ) -> None:
        with self.session_factory() as session:
            site = _get_or_create_site(session, run_id, marketplace_code)
            site.status = status.value
            site.error = error
            if status in (SiteStatus.PREFLIGHT, SiteStatus.RECONCILING):
                site.started_at = site.started_at or utc_now()
            if status in {
                SiteStatus.CONFIRMED,
                SiteStatus.DRY_RUN_COMPLETE,
                SiteStatus.SKIPPED,
                SiteStatus.NEEDS_HUMAN_AUTH,
                SiteStatus.UNCERTAIN_FINANCIAL,
                SiteStatus.FAILED,
                SiteStatus.CANCELLED,
            }:
                site.finished_at = utc_now()
            session.commit()

    async def get_site_status(
        self, run_id: str, marketplace_code: str
    ) -> SiteStatus | None:
        with self.session_factory() as session:
            value = session.scalar(
                select(SiteRun.status).where(
                    SiteRun.run_id == run_id,
                    SiteRun.marketplace_code == marketplace_code,
                )
            )
            return SiteStatus(value) if value is not None else None

    async def save_site_snapshot(
        self,
        run_id: str,
        marketplace: MarketplaceRef,
        snapshot: MarketplaceSnapshot,
    ) -> None:
        """Save facts needed by history/notifications, independent of execution.

        A zero-balance or no-data snapshot must remain outside the actionable
        ``WorkflowPlan``.  It is still a successful financial read, so persist
        its currency and amounts on ``SiteRun`` without changing the site's
        workflow status or assigning a plan hash.
        """

        with self.session_factory() as session:
            site = _get_or_create_site(session, run_id, marketplace.code)
            if str(site.marketplace_id) != str(marketplace.id):
                raise ValueError("金额快照的任务站点与店铺配置不一致")
            if snapshot.marketplace_code.upper() != marketplace.code.upper():
                raise ValueError("金额快照的站点代码与店铺配置不一致")
            site.currency = snapshot.currency
            site.payable_amount = snapshot.payable_amount
            site.delayed_amount = snapshot.delayed_amount
            site.settlement_key = snapshot.settlement_key
            site.snapshot_hash = snapshot.snapshot_hash
            session.commit()

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        marketplace_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self.session_factory() as session:
            site_id = None
            if marketplace_code:
                site_id = session.scalar(
                    select(SiteRun.id).where(
                        SiteRun.run_id == run_id,
                        SiteRun.marketplace_code == marketplace_code,
                    )
                )
            DbWorkflowRepository(session).append_event(
                run_id,
                event_type,
                site_run_id=site_id,
                message=message,
                details=dict(details or {}),
            )
            session.commit()

    async def save_plan(self, plan: WorkflowPlan) -> None:
        payload = _plan_to_json(plan)
        with self.session_factory() as session:
            db = DbWorkflowRepository(session)
            run = db.get_run(plan.run_id)
            summary = dict(run.result_summary or {})
            summary[self._PLAN_KEY] = payload
            run.result_summary = summary
            for line in plan.lines:
                snapshot = line.snapshot
                db.save_site_plan(
                    run_id=plan.run_id,
                    marketplace_id=int(line.marketplace.id),
                    marketplace_code=line.marketplace.code,
                    currency=snapshot.currency,
                    payable_amount=snapshot.payable_amount,
                    delayed_amount=snapshot.delayed_amount,
                    settlement_key=snapshot.settlement_key,
                    plan_hash=plan.plan_hash,
                    snapshot_hash=snapshot.snapshot_hash,
                    details={
                        "snapshot": snapshot.canonical(),
                        "screenshot_path": line.screenshot_path,
                    },
                )
            session.commit()

    async def get_plan(self, run_id: str) -> WorkflowPlan | None:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise LookupError(f"任务 {run_id} 不存在")
            payload = (run.result_summary or {}).get(self._PLAN_KEY)
            return _plan_from_json(payload) if isinstance(payload, dict) else None

    async def save_evidence(
        self, run_id: str, marketplace_code: str, evidence: EvidenceRef
    ) -> None:
        with self.session_factory() as session:
            site_id = session.scalar(
                select(SiteRun.id).where(
                    SiteRun.run_id == run_id,
                    SiteRun.marketplace_code == marketplace_code,
                )
            )
            size = None
            path = Path(evidence.file_path)
            if path.is_file():
                size = path.stat().st_size
            DbWorkflowRepository(session).add_evidence(
                run_id=run_id,
                site_run_id=site_id,
                kind=evidence.kind,
                file_path=evidence.file_path,
                sha256_hex=evidence.sha256,
                size_bytes=size,
                metadata=dict(evidence.metadata),
            )
            session.commit()

    async def create_approval(
        self, plan: WorkflowPlan, *, expires_at: datetime
    ) -> ApprovalRecord:
        snapshot_hash = _combined_snapshot_hash(plan)
        with self.session_factory() as session:
            row = DbWorkflowRepository(session).create_approval(
                run_id=plan.run_id,
                plan=_plan_to_json(plan),
                plan_hash=plan.plan_hash,
                snapshot_hash=snapshot_hash,
                expires_at=expires_at,
            )
            session.commit()
            return _approval_record(row)

    async def get_approval(self, run_id: str) -> ApprovalRecord | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ApprovalRequest)
                .where(ApprovalRequest.run_id == run_id)
                .order_by(ApprovalRequest.created_at.desc())
                .limit(1)
            )
            return _approval_record(row) if row else None

    async def set_approval_status(
        self,
        run_id: str,
        status: ApprovalStatus,
        *,
        actor: str | None = None,
    ) -> ApprovalRecord | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ApprovalRequest)
                .where(ApprovalRequest.run_id == run_id)
                .order_by(ApprovalRequest.created_at.desc())
                .limit(1)
            )
            if row is None:
                return None
            db = DbWorkflowRepository(session)
            if status is ApprovalStatus.APPROVED:
                row = db.approve(row.id, approved_by=actor or "admin")
            elif status is ApprovalStatus.CANCELLED:
                db.cancel_approval(row.id)
            elif status is ApprovalStatus.INVALIDATED:
                db.invalidate_approval(row.id, "执行前快照发生变化")
            elif status is ApprovalStatus.EXPIRED:
                session.execute(
                    update(ApprovalRequest)
                    .where(ApprovalRequest.id == row.id)
                    .values(
                        status="EXPIRED",
                        invalidated_at=utc_now(),
                        invalid_reason="审批已超时",
                    )
                )
            session.commit()
            session.refresh(row)
            return _approval_record(row)

    async def arm_operation(self, intent: OperationIntent) -> OperationRecord:
        """Create and COMMIT ARMED only while the run still owns RUNNING.

        SQLite's ``BEGIN IMMEDIATE`` serializes the status check and guard
        insert against cancellation/status writers.  On databases with row
        locks, ``FOR UPDATE`` provides the equivalent boundary.  Consequently
        a cancellation that wins first produces no ARMED row, while an arming
        transaction that wins first is durable before the browser click.
        """
        with self.session_factory() as session:
            connection = session.connection()
            is_sqlite = connection.dialect.name == "sqlite"
            if is_sqlite:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            run_status_stmt = select(Run.status).where(Run.id == intent.run_id)
            if not is_sqlite:
                run_status_stmt = run_status_stmt.with_for_update()
            run_status = session.scalar(run_status_stmt)
            if run_status is None:
                session.rollback()
                raise LookupError(f"任务 {intent.run_id} 不存在")
            if run_status != RunStatus.RUNNING.value:
                session.rollback()
                raise InvalidTransition(
                    "资金操作仅允许在任务持有 RUNNING 执行权时进入 ARMED"
                )
            site = session.scalar(
                select(SiteRun).where(
                    SiteRun.run_id == intent.run_id,
                    SiteRun.marketplace_code == intent.marketplace_code,
                )
            )
            if site is None:
                raise LookupError("资金提交前找不到站点运行记录")
            row, created = DbWorkflowRepository(session).arm_operation(
                guard_key=intent.guard_key,
                run_id=intent.run_id,
                site_run_id=site.id,
                store_id=int(intent.store_id),
                workflow="amazon_disbursement",
                marketplace_code=intent.marketplace_code,
                settlement_key=intent.settlement_key,
                amount=intent.amount,
                currency=intent.currency,
                plan_hash=intent.plan_hash,
                snapshot_hash=intent.snapshot_hash,
                payout_account_tail=intent.payout_account_tail,
                metadata={},
            )
            session.commit()  # safety boundary: must precede browser click
            return _operation_record(row, created=created)

    async def get_operation(self, guard_key: str) -> OperationRecord | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(OperationGuard).where(OperationGuard.guard_key == guard_key)
            )
            return _operation_record(row) if row else None

    async def list_operations(
        self,
        run_id: str,
        *,
        states: Sequence[GuardState] | None = None,
    ) -> Sequence[OperationRecord]:
        with self.session_factory() as session:
            stmt = select(OperationGuard).where(OperationGuard.run_id == run_id)
            if states is not None:
                stmt = stmt.where(
                    OperationGuard.state.in_(tuple(item.value for item in states))
                )
            return tuple(
                _operation_record(row)
                for row in session.scalars(stmt.order_by(OperationGuard.armed_at))
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
        with self.session_factory() as session:
            row = session.scalar(
                select(OperationGuard).where(OperationGuard.guard_key == guard_key)
            )
            if row is None:
                raise LookupError("资金记录不存在")
            metadata = dict(row.metadata_json or {})
            metadata.update(dict(details or {}))
            if receipt_id:
                metadata["receipt_id"] = receipt_id
            changed = DbWorkflowRepository(session).transition_guard(
                row.id,
                expected_states=tuple(item.value for item in expected),
                to_state=target.value,
                failure_reason=(
                    str(metadata.get("reason"))
                    if target is GuardState.UNCERTAIN and metadata.get("reason")
                    else None
                ),
                metadata=metadata,
            )
            if not changed:
                session.rollback()
                raise InvalidTransition("资金状态已变化，当前操作未执行")
            session.commit()
            session.refresh(row)
            return _operation_record(row)

    async def release_operation(self, guard_key: str) -> bool:
        """Delete a guard for which no dispatch was ever recorded.

        ``submitted_at`` is stamped only on the transition that follows the sole
        irreversible click, so a NULL there means this system never saw a payout
        leave.  That single predicate is the hard boundary: once it is set the
        row can never be deleted by anyone, automatic or human.

        The state list is belt-and-braces on top of it.  ARMED is what the
        automatic caller hits, holding proof from the page adapter
        (:class:`SubmissionNotDispatched`); UNCERTAIN is the same never-dispatched
        row after a read-back found nothing, which only an operator may clear.

        Deleting rather than transitioning is the point: it frees ``guard_key``
        so the site can be attempted again.  Leaving the row would block the
        site and keep a record of a payout that was never requested.  Both
        predicates live in the DELETE itself, so a guard that reaches SUBMITTED
        between the caller's decision and this write is never removed.
        """

        with self.session_factory() as session:
            deleted = session.execute(
                delete(OperationGuard).where(
                    OperationGuard.guard_key == guard_key,
                    OperationGuard.submitted_at.is_(None),
                    OperationGuard.state.in_(
                        (GuardState.ARMED.value, GuardState.UNCERTAIN.value)
                    ),
                )
            ).rowcount
            session.commit()
            return bool(deleted)


def _get_or_create_site(session: Session, run_id: str, code: str) -> SiteRun:
    site = session.scalar(
        select(SiteRun).where(
            SiteRun.run_id == run_id, SiteRun.marketplace_code == code
        )
    )
    if site is not None:
        return site
    run = session.get(Run, run_id)
    if run is None:
        raise LookupError(f"任务 {run_id} 不存在")
    marketplace = session.scalar(
        select(StoreMarketplace).where(
            StoreMarketplace.store_id == run.store_id,
            StoreMarketplace.code == code,
        )
    )
    if marketplace is None:
        raise LookupError(f"任务店铺没有站点 {code}")
    site = SiteRun(
        run_id=run_id,
        marketplace_id=marketplace.id,
        marketplace_code=code,
    )
    session.add(site)
    session.flush()
    return site


def _operation_record(row: OperationGuard, *, created: bool = False) -> OperationRecord:
    metadata = dict(row.metadata_json or {})
    intent = OperationIntent(
        guard_key=row.guard_key,
        run_id=row.run_id,
        site_run_key=row.site_run_id,
        store_id=str(row.store_id),
        marketplace_code=row.marketplace_code,
        settlement_key=row.settlement_key,
        amount=Decimal(row.amount),
        currency=row.currency,
        plan_hash=row.plan_hash,
        snapshot_hash=row.snapshot_hash,
        payout_account_tail=row.payout_account_tail or "",
    )
    return OperationRecord(
        intent=intent,
        state=GuardState(row.state),
        created=created,
        receipt_id=metadata.get("receipt_id"),
        last_reconciled_at=_aware(row.last_reconciled_at),
        details=metadata,
        submitted_at=_aware(row.submitted_at),
    )


def _approval_record(row: ApprovalRequest) -> ApprovalRecord:
    return ApprovalRecord(
        run_id=row.run_id,
        plan_hash=row.plan_hash,
        status=ApprovalStatus(row.status),
        expires_at=_aware(row.expires_at) or utc_now(),
        plan=dict(row.plan_json or {}),
        approved_by=row.approved_by,
        approved_at=_aware(row.approved_at),
    )


def _plan_to_json(plan: WorkflowPlan) -> dict[str, Any]:
    return {
        "workflow": plan.workflow,
        "run_id": plan.run_id,
        "store_id": plan.store_id,
        "created_at": plan.created_at.isoformat(),
        "lines": [
            {
                "marketplace": {
                    "id": line.marketplace.id,
                    "code": line.marketplace.code,
                    "domain": line.marketplace.domain,
                    "currency": line.marketplace.currency,
                    "enabled": line.marketplace.enabled,
                    "payments_path": line.marketplace.payments_path,
                },
                "snapshot": {
                    **line.snapshot.canonical(),
                    "observed_at": line.snapshot.observed_at.isoformat(),
                },
                "screenshot_path": line.screenshot_path,
            }
            for line in plan.lines
        ],
    }


def _plan_from_json(payload: Mapping[str, Any]) -> WorkflowPlan:
    lines: list[PlanLine] = []
    for item in payload.get("lines", []):
        market = item["marketplace"]
        snap = item["snapshot"]
        marketplace = MarketplaceRef(
            id=str(market["id"]),
            code=market["code"],
            domain=market["domain"],
            currency=market["currency"],
            enabled=bool(market.get("enabled", True)),
            payments_path=market.get("payments_path", "/payments/dashboard/index.html"),
        )
        snapshot = MarketplaceSnapshot(
            marketplace_code=snap["marketplace_code"],
            domain=snap["domain"],
            seller_id=snap["seller_id"],
            # Absent from canonical(); a plan restored from disk carries no
            # destination and does not need one.
            payment_account=snap.get("payment_account", ""),
            currency=snap["currency"],
            payable_amount=Decimal(snap["payable_amount"]),
            delayed_amount=Decimal(snap["delayed_amount"]),
            settlement_key=snap["settlement_key"],
            can_submit=bool(snap["can_submit"]),
            contract_version=snap["contract_version"],
            page_fingerprint=snap["page_fingerprint"],
            skip_reason=snap.get("skip_reason"),
            identity_source=snap.get("identity_source", "unknown"),
            observed_at=_parse_datetime(snap.get("observed_at")) or utc_now(),
        )
        lines.append(
            PlanLine(marketplace, snapshot, item.get("screenshot_path"))
        )
    return WorkflowPlan(
        workflow=str(payload["workflow"]),
        run_id=str(payload["run_id"]),
        store_id=str(payload["store_id"]),
        lines=tuple(lines),
        created_at=_parse_datetime(payload.get("created_at")) or utc_now(),
    )


def _combined_snapshot_hash(plan: WorkflowPlan) -> str:
    from .types import stable_hash

    return stable_hash([line.snapshot.snapshot_hash for line in plan.lines])


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    return _aware(datetime.fromisoformat(str(value)))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _account_digits(value: object) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _validated_account_tail(value: object) -> str:
    account = _account_digits(value)
    if str(value or "").strip() != account or not 2 <= len(account) <= 8:
        raise ValueError("付款账户检测结果不是 2 到 8 位数字尾号")
    return account


def _identity(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()
