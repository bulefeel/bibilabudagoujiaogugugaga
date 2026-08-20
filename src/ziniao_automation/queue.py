"""Durable single-worker FIFO queue for automation runs.

SQLite is the source of truth.  The worker claims one entry at a time, executes
it outside the claim transaction, and records completion before moving on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .db import utc_now
from .models import (
    ApprovalRequest,
    OperationGuard,
    Run,
    RunEvent,
    RunQueueEntry,
    Schedule,
)
from .notifications import NotificationDeliveryService

logger = logging.getLogger(__name__)

PRIORITY_RECOVERY = 0
PRIORITY_HUMAN = 10
PRIORITY_SCHEDULE = 100
QUEUE_ACTIONS = frozenset({"START", "APPROVE", "CONTINUE_AUTH", "RECONCILE"})


class DurableRunQueue:
    """Atomically enqueue, claim and complete persistent work items."""

    def __init__(self, session_factory: sessionmaker[Session] | Any) -> None:
        self.session_factory = session_factory

    def enqueue(
        self,
        run_id: str,
        *,
        action: str = "START",
        priority: int | None = None,
        scheduled_for_at: datetime | None = None,
    ) -> tuple[int, bool]:
        action = action.upper()
        if action not in QUEUE_ACTIONS:
            raise ValueError(f"unsupported queue action: {action}")
        with self.session_factory() as session:
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            run = session.get(Run, run_id)
            if run is None:
                raise LookupError(f"run {run_id} does not exist")
            active = session.scalar(
                select(RunQueueEntry)
                .where(
                    RunQueueEntry.run_id == run_id,
                    RunQueueEntry.state.in_(("READY", "CLAIMED")),
                )
                .order_by(RunQueueEntry.id.desc())
                .limit(1)
            )
            if active is not None:
                session.commit()
                return active.id, False
            due = scheduled_for_at if scheduled_for_at is not None else run.scheduled_for_at
            entry = RunQueueEntry(
                run_id=run_id,
                action=action,
                priority=priority if priority is not None else self._default_priority(run, action),
                state="READY",
                scheduled_for_at=due,
            )
            session.add(entry)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                with self.session_factory() as retry:
                    winner = retry.scalar(
                        select(RunQueueEntry)
                        .where(
                            RunQueueEntry.run_id == run_id,
                            RunQueueEntry.state.in_(("READY", "CLAIMED")),
                        )
                        .order_by(RunQueueEntry.id.desc())
                        .limit(1)
                    )
                    if winner is None:
                        raise
                    return winner.id, False
            session.add(
                RunEvent(
                    run_id=run_id,
                    event_type="QUEUE_ENQUEUED",
                    to_status="QUEUED",
                    message=f"queue action {action} persisted",
                )
            )
            session.commit()
            return entry.id, True

    @staticmethod
    def _default_priority(run: Run, action: str) -> int:
        if action == "RECONCILE" or run.trigger == "recovery":
            return PRIORITY_RECOVERY
        if action in {"APPROVE", "CONTINUE_AUTH"} or run.trigger != "schedule":
            return PRIORITY_HUMAN
        return PRIORITY_SCHEDULE

    def approve_and_enqueue(
        self,
        run_id: str,
        approval_id: str,
        *,
        actor: str = "admin",
    ) -> int:
        """Atomically approve a saved plan and persist its worker action."""

        now = utc_now()
        with self.session_factory() as session:
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            run = session.get(Run, run_id)
            approval = session.get(ApprovalRequest, approval_id)
            if run is None or approval is None or approval.run_id != run_id:
                raise RuntimeError("没有可处理的审批")
            if run.status != "WAITING_APPROVAL":
                raise RuntimeError("任务当前不在待审核状态")
            if approval.status != "PENDING":
                raise RuntimeError("审批已处理或失效")
            expires = approval.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                approval.status = "EXPIRED"
                approval.invalidated_at = now
                approval.invalid_reason = "审批已超时"
                session.commit()
                raise RuntimeError("金额清单已过期，请重新生成")
            active = session.scalar(
                select(RunQueueEntry.id).where(
                    RunQueueEntry.run_id == run_id,
                    RunQueueEntry.state.in_(("READY", "CLAIMED")),
                )
            )
            if active is not None:
                raise RuntimeError("任务已有排队或执行中的动作")

            approval.status = "APPROVED"
            approval.approved_at = now
            approval.approved_by = actor[:80]
            run.status = "QUEUED"
            run.error = None
            entry = RunQueueEntry(
                run_id=run_id,
                action="APPROVE",
                priority=PRIORITY_HUMAN,
                state="READY",
                scheduled_for_at=run.scheduled_for_at,
            )
            session.add(entry)
            session.flush()
            session.add(
                RunEvent(
                    run_id=run_id,
                    event_type="APPROVAL_QUEUED",
                    from_status="WAITING_APPROVAL",
                    to_status="QUEUED",
                    message="approved plan persisted for the single worker",
                )
            )
            session.commit()
            return entry.id

    def claim_next(self) -> RunQueueEntry | None:
        """Claim the first READY item under a SQLite write reservation."""

        now = utc_now()
        token = uuid4().hex
        with self.session_factory() as session:
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            entry = session.scalar(
                select(RunQueueEntry)
                .where(
                    RunQueueEntry.state == "READY",
                    RunQueueEntry.available_at <= now,
                )
                .order_by(
                    RunQueueEntry.priority.asc(),
                    func.coalesce(
                        RunQueueEntry.scheduled_for_at, RunQueueEntry.enqueued_at
                    ).asc(),
                    RunQueueEntry.id.asc(),
                )
                .limit(1)
            )
            if entry is None:
                session.commit()
                return None
            changed = session.execute(
                update(RunQueueEntry)
                .where(RunQueueEntry.id == entry.id, RunQueueEntry.state == "READY")
                .values(
                    state="CLAIMED",
                    claim_token=token,
                    claimed_at=now,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                session.rollback()
                return None
            session.add(
                RunEvent(
                    run_id=entry.run_id,
                    event_type="QUEUE_CLAIMED",
                    message=f"queue action {entry.action} claimed",
                )
            )
            session.commit()
            session.refresh(entry)
            session.expunge(entry)
            return entry

    def finish(self, entry_id: int, *, cancelled: bool = False) -> bool:
        target = "CANCELLED" if cancelled else "DONE"
        now = utc_now()
        with self.session_factory() as session:
            entry = session.get(RunQueueEntry, entry_id)
            if entry is None or entry.state != "CLAIMED":
                return False
            entry.state = target
            entry.finished_at = now
            entry.claim_token = None
            session.add(
                RunEvent(
                    run_id=entry.run_id,
                    event_type=f"QUEUE_{target}",
                    message=f"queue action {entry.action} {target.lower()}",
                )
            )
            session.commit()
            return True

    def cancel_ready(self, run_id: str) -> bool:
        now = utc_now()
        with self.session_factory() as session:
            result = session.execute(
                update(RunQueueEntry)
                .where(
                    RunQueueEntry.run_id == run_id,
                    RunQueueEntry.state == "READY",
                )
                .values(state="CANCELLED", finished_at=now, updated_at=now)
            )
            if result.rowcount:
                session.add(
                    RunEvent(
                        run_id=run_id,
                        event_type="QUEUE_CANCELLED",
                        message="queued action cancelled before claim",
                    )
                )
            session.commit()
            return bool(result.rowcount)

    def replace_ready_action(
        self, run_id: str, *, action: str, priority: int
    ) -> bool:
        """Change an unclaimed action without changing its FIFO row ID."""

        action = action.upper()
        if action not in QUEUE_ACTIONS:
            raise ValueError(f"unsupported queue action: {action}")
        with self.session_factory() as session:
            result = session.execute(
                update(RunQueueEntry)
                .where(
                    RunQueueEntry.run_id == run_id,
                    RunQueueEntry.state == "READY",
                )
                .values(action=action, priority=priority, updated_at=utc_now())
            )
            session.commit()
            return bool(result.rowcount)

    def restore_claimed(self, financial_run_ids: set[str]) -> None:
        """Restore crash leftovers without changing entry IDs/FIFO position."""

        now = utc_now()
        with self.session_factory() as session:
            claimed = list(
                session.scalars(
                    select(RunQueueEntry)
                    .where(RunQueueEntry.state == "CLAIMED")
                    .order_by(RunQueueEntry.id)
                )
            )
            for entry in claimed:
                entry.state = "READY"
                entry.claim_token = None
                entry.claimed_at = None
                entry.updated_at = now
                if entry.run_id in financial_run_ids:
                    entry.action = "RECONCILE"
                    entry.priority = PRIORITY_RECOVERY
                run = session.get(Run, entry.run_id)
                if (
                    run is not None
                    and entry.run_id not in financial_run_ids
                    and run.status in {"RUNNING", "RECONCILING"}
                ):
                    run.status = "QUEUED"
                    run.error = None
                    run.finished_at = None
            session.commit()

    def ensure_financial_recovery(self, run_id: str) -> None:
        with self.session_factory() as session:
            active = session.scalar(
                select(RunQueueEntry).where(
                    RunQueueEntry.run_id == run_id,
                    RunQueueEntry.state.in_(("READY", "CLAIMED")),
                )
            )
            if active is not None:
                if active.state == "CLAIMED":
                    active.state = "READY"
                    active.claim_token = None
                    active.claimed_at = None
                active.action = "RECONCILE"
                active.priority = PRIORITY_RECOVERY
                session.commit()
                return
        self.enqueue(run_id, action="RECONCILE", priority=PRIORITY_RECOVERY)

    def has_ready_or_claimed(self) -> bool:
        with self.session_factory() as session:
            return session.scalar(
                select(RunQueueEntry.id)
                .where(RunQueueEntry.state.in_(("READY", "CLAIMED")))
                .limit(1)
            ) is not None

    def active_count(self) -> int:
        with self.session_factory() as session:
            return int(
                session.scalar(
                    select(func.count(RunQueueEntry.id)).where(
                        RunQueueEntry.state.in_(("READY", "CLAIMED"))
                    )
                )
                or 0
            )

    def mark_cross_day_notified(self, entry_id: int) -> bool:
        now = utc_now()
        with self.session_factory() as session:
            entry = session.get(RunQueueEntry, entry_id)
            if entry is None or entry.cross_day_notified_at is not None:
                return False
            run = session.get(Run, entry.run_id)
            if (
                run is None
                or run.trigger != "schedule"
                or entry.scheduled_for_at is None
                or not _is_cross_day(entry.scheduled_for_at, run.schedule)
            ):
                return False
            # The database-built card validates the actual start timestamp.
            # Persist it at the exact point the worker has obtained execution
            # rights, immediately before opening the Ziniao environment.
            if run.started_at is None:
                run.started_at = now
            entry.cross_day_notified_at = now
            session.commit()
            return True


def _is_cross_day(scheduled_for: datetime, schedule: Schedule | None) -> bool:
    try:
        zone = ZoneInfo(schedule.timezone if schedule else "Asia/Singapore")
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    due = scheduled_for
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc).astimezone(zone).date() > due.astimezone(zone).date()


class PersistentQueueWorker:
    """Exactly one dispatcher task; it never runs two browser jobs together."""

    def __init__(
        self,
        queue: DurableRunQueue,
        execute: Callable[[RunQueueEntry], Awaitable[None]],
        *,
        notifications: NotificationDeliveryService | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self.queue = queue
        self.execute = execute
        self.notifications = notifications
        self.poll_seconds = poll_seconds
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._stopping = False
        self.current_entry_id: int | None = None

    async def start(self) -> None:
        if self._dispatcher is not None and not self._dispatcher.done():
            return
        self._stopping = False
        self._dispatcher = asyncio.create_task(self._loop(), name="durable-run-worker")
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            entry = self.queue.claim_next()
            if entry is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
                continue
            self.current_entry_id = entry.id
            try:
                if self.queue.mark_cross_day_notified(entry.id) and self.notifications:
                    await self.notifications.notify_cross_day_started(entry.run_id)
                await self.execute(entry)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "durable_queue_item_failed run_id=%s action=%s",
                    entry.run_id[:8],
                    entry.action,
                )
            finally:
                self.queue.finish(entry.id)
                self.current_entry_id = None

    async def wait_idle(self) -> None:
        stable_empty_polls = 0
        while stable_empty_polls < 2:
            active = (
                self.current_entry_id is not None
                or self.queue.has_ready_or_claimed()
            )
            stable_empty_polls = 0 if active else stable_empty_polls + 1
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        self._stopping = True
        self._wake.set()
        task = self._dispatcher
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._dispatcher = None
