"""SQLite-backed schedule projection for APScheduler.

SQLite remains the source of truth.  APScheduler jobs are rebuilt from the
``schedules`` table on every process start and after every schedule mutation.
No pickle/job-store state is used.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import Run, RunEvent, Schedule, Store
from .repositories import ConflictError

logger = logging.getLogger(__name__)


MISFIRE_GRACE_SECONDS = 1800


class RuntimeService(Protocol):
    async def enqueue_run(self, run_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ScheduleProjectionReport:
    """Per-row outcome from rebuilding the process-local scheduler."""

    projected_schedule_ids: frozenset[int]
    failed_schedule_ids: frozenset[int]


class ScheduleManager:
    """Project enabled database schedules into a process-local async scheduler."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | Any,
        automation_service: RuntimeService,
        *,
        scheduler: Any | None = None,
        scheduler_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        workflow_registry: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.automation_service = automation_service
        self._scheduler = scheduler
        self._scheduler_factory = scheduler_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.workflow_registry = workflow_registry
        self._started = False
        self._refresh_lock = asyncio.Lock()
        # APScheduler prevents two clock firings of one job, while this lock
        # also serialises a simultaneous administrator "run now" request with
        # that clock firing before either creates its QUEUED database row.
        self._trigger_lock = asyncio.Lock()

    @property
    def scheduler(self) -> Any:
        if self._scheduler is None:
            factory = self._scheduler_factory or _default_scheduler
            self._scheduler = factory()
        return self._scheduler

    async def start(self) -> None:
        if self._started:
            return
        # Start paused so a stale in-memory job can never fire between startup
        # and the authoritative SQLite rebuild.
        self.scheduler.start(paused=True)
        self._started = True
        await self.recover_latest_missed()
        await self.refresh()
        self.scheduler.resume()

    async def shutdown(self, *, wait: bool = False) -> None:
        if not self._started:
            return
        self.scheduler.shutdown(wait=wait)
        self._started = False

    async def refresh(self) -> ScheduleProjectionReport:
        """Rebuild all managed jobs and persist the next fire times."""

        async with self._refresh_lock:
            schedules = self._enabled_schedules()
            wanted: set[str] = set()
            updates: list[tuple[int, datetime | None]] = []
            projected_schedule_ids: set[int] = set()
            failed_schedule_ids: set[int] = set()
            for row in schedules:
                try:
                    job_id = self.job_id(row.id)
                    trigger = _interval_trigger(
                        interval_minutes=row.interval_minutes,
                        first_run_at=row.first_run_at,
                    )
                    job = self.scheduler.add_job(
                        self.trigger_schedule,
                        trigger=trigger,
                        args=(row.id,),
                        id=job_id,
                        name=f"{row.name} · {row.store.name}",
                        replace_existing=True,
                        coalesce=True,
                        max_instances=1,
                        misfire_grace_time=min(
                            MISFIRE_GRACE_SECONDS,
                            max(1, int(row.misfire_grace_seconds)),
                        ),
                    )
                    wanted.add(job_id)
                    projected_schedule_ids.add(row.id)
                    updates.append((row.id, getattr(job, "next_run_time", None)))
                except Exception:
                    # One malformed imported/legacy row must not prevent every
                    # other store's valid schedule from being projected.
                    logger.exception(
                        "schedule_projection_rejected schedule_id=%s", row.id
                    )
                    failed_schedule_ids.add(row.id)
                    updates.append((row.id, None))
            for job in tuple(self.scheduler.get_jobs()):
                if str(job.id).startswith("db-schedule:") and job.id not in wanted:
                    self.scheduler.remove_job(job.id)
            self._set_next_runs(updates, wanted)
            return ScheduleProjectionReport(
                projected_schedule_ids=frozenset(projected_schedule_ids),
                failed_schedule_ids=frozenset(failed_schedule_ids),
            )

    async def refresh_schedule(
        self, schedule_id: int
    ) -> ScheduleProjectionReport:
        """Refresh after an API mutation and expose the projection outcome.

        The database mutation is committed before this method is called.  Its
        caller therefore needs the structured report to distinguish "saved in
        SQLite" from "also active in this process-local scheduler".  Returning
        an empty report while the manager is stopped is deliberate: an enabled
        target will then be reported as durable-but-not-yet-projected instead
        of being presented as fully active.
        """

        del schedule_id
        if self._started:
            return await self.refresh()
        return ScheduleProjectionReport(
            projected_schedule_ids=frozenset(),
            failed_schedule_ids=frozenset(),
        )

    async def trigger_schedule(
        self,
        schedule_id: int,
        *,
        scheduled_for_at: datetime | None = None,
    ) -> str | None:
        async with self._trigger_lock:
            return await self._trigger_schedule_locked(
                schedule_id,
                scheduled_for_at=scheduled_for_at,
            )

    async def _trigger_schedule_locked(
        self,
        schedule_id: int,
        *,
        scheduled_for_at: datetime | None = None,
    ) -> str | None:
        """Create one due run, or an entire same-occurrence batch.

        APScheduler invokes one callback per child schedule.  If the first
        callback woke the worker immediately, the queue could claim child 1
        before children 2..N had been inserted.  We therefore detect the
        parent batch while holding ``_trigger_lock``, create all due children
        in one database transaction, and hand their IDs to
        ``AutomationService.enqueue_runs`` (which writes queue rows and wakes
        the worker once).  A later callback sees the persisted occurrence and
        becomes a no-op.
        """

        created_run_ids: list[str] = []
        supplied_occurrence = scheduled_for_at is not None
        occurrence = _as_utc(scheduled_for_at) if supplied_occurrence else None
        observed_now = _as_utc(self._clock())
        with self.session_factory() as session:
            try:
                schedule = session.scalar(
                    select(Schedule)
                    .where(Schedule.id == schedule_id)
                    .with_for_update()
                )
                if schedule is None or not schedule.enabled:
                    return None
                if occurrence is None:
                    occurrence = _latest_occurrence(schedule, now=observed_now)
                if occurrence is None:
                    return None

                candidates = [schedule]
                if schedule.batch_id is not None:
                    siblings = list(
                        session.scalars(
                            select(Schedule)
                            .where(
                                Schedule.batch_id == schedule.batch_id,
                                Schedule.enabled.is_(True),
                            )
                            .order_by(Schedule.batch_order, Schedule.id)
                            .with_for_update()
                        )
                    )
                    candidates = []
                    malformed_siblings: list[tuple[Schedule, Exception]] = []
                    for candidate in siblings:
                        if candidate.id == schedule.id:
                            candidates.append(candidate)
                            continue
                        # Batch children normally share one template. Keep the
                        # guard for later per-child edits: a child whose cron
                        # has a different occurrence is not manufactured here.
                        check_now = occurrence if supplied_occurrence else observed_now
                        try:
                            candidate_occurrence = _latest_occurrence(
                                candidate, now=check_now
                            )
                        except Exception as exc:
                            # One damaged child must not roll back healthy
                            # siblings in the same batch.  Keep it auditable as
                            # a skipped occurrence after the valid children
                            # have been considered.
                            malformed_siblings.append((candidate, exc))
                            logger.exception(
                                "schedule_batch_child_rejected schedule_id=%s",
                                candidate.id,
                            )
                            continue
                        if candidate_occurrence == occurrence:
                            candidates.append(candidate)
                    if schedule not in candidates:
                        candidates.insert(0, schedule)
                else:
                    malformed_siblings = []

                from .repositories import ScheduleRepository

                repository = ScheduleRepository(session, self.workflow_registry)
                for candidate, exc in malformed_siblings:
                    self._record_skipped(
                        session,
                        candidate,
                        f"排期时间或时区无效：{type(exc).__name__}",
                        scheduled_for_at=occurrence,
                    )
                for candidate in candidates:
                    existing = session.scalar(
                        select(Run.id)
                        .where(
                            Run.schedule_id == candidate.id,
                            Run.scheduled_for_at == occurrence,
                        )
                        .limit(1)
                    )
                    if existing:
                        logger.info(
                            "schedule_occurrence_already_persisted schedule_id=%s "
                            "scheduled_for_at=%s run_id=%s",
                            candidate.id,
                            occurrence.isoformat(),
                            existing,
                        )
                        continue

                    store = session.get(Store, candidate.store_id)
                    requires_identity = self._requires_confirmed_identity(
                        candidate.workflow
                    )
                    if store is None or not store.enabled or (
                        requires_identity and not store.identity_confirmed
                    ):
                        self._record_skipped(
                            session,
                            candidate,
                            "店铺未启用或卖家身份尚未确认",
                            scheduled_for_at=occurrence,
                        )
                        continue
                    try:
                        run = repository.create_run_from_schedule(
                            candidate.id,
                            require_enabled=True,
                            trigger="schedule",
                            requested_by="scheduler",
                            scheduled_for_at=occurrence,
                        )
                    except (ConflictError, ValueError, LookupError) as exc:
                        # An individually malformed/conflicting child should
                        # be auditable as SKIPPED without blocking its siblings.
                        self._record_skipped(
                            session,
                            candidate,
                            str(exc),
                            scheduled_for_at=occurrence,
                        )
                        continue
                    created_run_ids.append(run.id)
                session.commit()
            except IntegrityError:
                session.rollback()
                duplicate = session.scalar(
                    select(Run.id)
                    .where(
                        Run.schedule_id == schedule_id,
                        Run.scheduled_for_at == occurrence,
                    )
                    .limit(1)
                )
                if duplicate:
                    return None
                raise
            except (ValueError, LookupError) as exc:
                session.rollback()
                schedule = session.get(Schedule, schedule_id)
                if schedule is not None and occurrence is not None:
                    self._record_skipped(
                        session,
                        schedule,
                        str(exc),
                        scheduled_for_at=occurrence,
                    )
                    session.commit()
                return None

        if created_run_ids:
            try:
                await self._enqueue_created_runs(created_run_ids)
            except Exception as exc:
                for run_id in created_run_ids:
                    self._mark_enqueue_failure(run_id, exc)
                raise
            return created_run_ids[0]
        return None

    def _requires_confirmed_identity(self, workflow: str) -> bool:
        """Read the code-owned workflow identity contract.

        Payout keeps its historical identity gate. Generic read-only or
        standard workflows may explicitly opt out; unknown definitions fail
        closed and are rejected by the repository before opening a browser.
        """

        if self.workflow_registry is None:
            return True
        try:
            definition = self.workflow_registry.definition(workflow)
        except Exception:
            return True
        return bool(getattr(definition, "requires_confirmed_identity", False))

    async def _enqueue_created_runs(self, run_ids: list[str]) -> None:
        """Use atomic batch enqueue when the runtime exposes it."""

        enqueue_many = getattr(self.automation_service, "enqueue_runs", None)
        if callable(enqueue_many):
            await enqueue_many(tuple(run_ids))
            return
        # Compatibility for small test doubles and older integrations. The
        # production AutomationService implements enqueue_runs above.
        for run_id in run_ids:
            await self.automation_service.enqueue_run(run_id)

    async def recover_latest_missed(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Create the most recent missed occurrence, if it is still fresh.

        Two limits apply, and both matter:

        * repeatability — a persisted occurrence is found by
          ``(schedule_id, scheduled_for_at)`` and is not recreated, regardless
          of whether that earlier run is waiting, running or already terminal.
          We never enumerate every missed day.
        * freshness — an occurrence later than ``misfire_grace_seconds`` is
          dropped, the same rule APScheduler applies in-process.  Without it,
          starting the service (which the installer does on every upgrade, and
          which the desktop shortcut does whenever the app is opened on a
          machine that is not left running) executed a schedule hours after the
          moment the operator chose.
        """

        observed_at = _as_utc(now or self._clock())
        created: list[str] = []
        async with self._trigger_lock:
            for schedule in self._enabled_schedules():
                try:
                    occurrence = _latest_occurrence(schedule, now=observed_at)
                    if occurrence is None:
                        continue
                    # ``next_run_at`` comes from legacy SQLite rows as well as
                    # current projections.  Keep all per-row recovery logic in
                    # this guard: a malformed timestamp, timezone, or unknown
                    # workflow must not prevent healthy schedules later in the
                    # same pass from being recovered.
                    eligible_since = (
                        _persisted_next_run_as_utc(schedule)
                        if schedule.next_run_at is not None
                        else max(
                            _as_utc(schedule.created_at),
                            _as_utc(schedule.updated_at),
                        )
                    )
                    if occurrence < eligible_since:
                        continue
                    # ``misfire_grace_seconds`` must mean the same thing on both
                    # paths.  APScheduler already refuses an in-process fire that
                    # is later than the grace (see the add_job call above), but
                    # this offline path used to ignore it entirely: an operator
                    # who configured 30 minutes got an occurrence caught up 11
                    # hours late, simply because the machine had been off.
                    #
                    # That is wrong for the payout workflow in particular.
                    # Amazon rate limits on a ROLLING 24 hours from the previous
                    # request, so a catch-up at an arbitrary hour drags the whole
                    # window with it and the next scheduled occurrence is refused
                    # — which is exactly what the interval trigger exists to
                    # avoid.  Nothing is lost by waiting: the balance keeps
                    # accruing at Amazon and the next occurrence pays it out.
                    grace = timedelta(seconds=max(0, int(schedule.misfire_grace_seconds)))
                    if occurrence < observed_at - grace:
                        logger.info(
                            "schedule_missed_occurrence_expired schedule_id=%s "
                            "occurrence=%s late_seconds=%d grace_seconds=%d",
                            schedule.id,
                            occurrence.isoformat(),
                            int((observed_at - occurrence).total_seconds()),
                            int(grace.total_seconds()),
                        )
                        continue
                    run_id = await self._trigger_schedule_locked(
                        schedule.id,
                        scheduled_for_at=occurrence,
                    )
                    if run_id is not None:
                        created.append(run_id)
                except Exception:
                    logger.exception(
                        "schedule_missed_occurrence_rejected schedule_id=%s",
                        schedule.id,
                    )
                    continue
        return tuple(created)

    async def run_now(self, schedule_id: int, *, requested_by: str = "admin") -> str:
        async with self._trigger_lock:
            return await self._run_now_locked(schedule_id, requested_by=requested_by)

    async def _run_now_locked(self, schedule_id: int, *, requested_by: str) -> str:
        """Execute a saved rule immediately without requiring its timer enabled.

        The repository still enforces the fixed workflow allow-list, enabled
        store and marketplaces, confirmed seller identity, one active instance,
        and the automatic-mode qualification gate.
        """

        with self.session_factory() as session:
            schedule = session.scalar(
                select(Schedule).where(Schedule.id == schedule_id).with_for_update()
            )
            if schedule is None:
                from .repositories import NotFoundError

                raise NotFoundError(f"计划 {schedule_id} 不存在")
            from .repositories import ScheduleRepository

            run = ScheduleRepository(
                session, self.workflow_registry
            ).create_run_from_schedule(
                schedule.id,
                require_enabled=False,
                trigger="manual",
                requested_by=requested_by,
            )
            session.commit()
            run_id = run.id
        try:
            await self.automation_service.enqueue_run(run_id)
        except Exception as exc:
            self._mark_enqueue_failure(run_id, exc)
            raise
        return run_id

    @staticmethod
    def job_id(schedule_id: int) -> str:
        return f"db-schedule:{schedule_id}"

    def _enabled_schedules(self) -> tuple[Schedule, ...]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(Schedule)
                .join(Store, Store.id == Schedule.store_id)
                .where(Schedule.enabled.is_(True), Store.enabled.is_(True))
                .order_by(Schedule.id)
            ).all()
            # Materialise attributes while the session is open.
            return tuple(
                _ScheduleSnapshot.from_model(row, session.get(Store, row.store_id))
                for row in rows
            )  # type: ignore[return-value]

    def _set_next_runs(
        self,
        updates: list[tuple[int, datetime | None]],
        wanted_job_ids: set[str],
    ) -> None:
        with self.session_factory() as session:
            for schedule_id, value in updates:
                row = session.get(Schedule, schedule_id)
                if row is not None:
                    # APScheduler returns the occurrence in the cron zone
                    # (Asia/Singapore for V1).  Persist the instant as UTC;
                    # SQLite otherwise strips ``tzinfo`` but keeps the 09:00
                    # wall clock, which later looks like 17:00 when the HTML
                    # presentation correctly converts UTC back to UTC+8.
                    row.next_run_at = _as_utc(value) if value is not None else None
            # Disabled/deleted projections should not retain a misleading
            # next-run timestamp in the SQLite source of truth.
            for row in session.scalars(select(Schedule).where(Schedule.next_run_at.is_not(None))):
                if self.job_id(row.id) not in wanted_job_ids:
                    row.next_run_at = None
            session.commit()

    def _record_skipped(
        self,
        session: Session,
        schedule: Schedule,
        reason: str,
        *,
        scheduled_for_at: datetime | None = None,
    ) -> None:
        run = Run(
            store_id=schedule.store_id,
            schedule_id=schedule.id,
            workflow=schedule.workflow,
            mode=schedule.mode,
            trigger="schedule",
            status="SKIPPED",
            requested_by="scheduler",
            scheduled_for_at=scheduled_for_at,
            workflow_config=dict(schedule.workflow_config or {}),
            workflow_config_version=int(schedule.workflow_config_version or 1),
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            error=reason,
        )
        session.add(run)
        session.flush()
        session.add(
            RunEvent(
                run_id=run.id,
                event_type="SCHEDULE_SKIPPED",
                to_status="SKIPPED",
                message=reason,
            )
        )
        schedule.last_run_at = datetime.now(timezone.utc)

    def _mark_enqueue_failure(self, run_id: str, exc: Exception) -> None:
        with self.session_factory() as session:
            row = session.get(Run, run_id)
            if row is not None and row.status == "QUEUED":
                row.status = "FAILED"
                row.error = f"调度任务入队失败：{type(exc).__name__}"
                row.finished_at = datetime.now(timezone.utc)
                session.add(
                    RunEvent(
                        run_id=run_id,
                        event_type="SCHEDULE_ENQUEUE_FAILED",
                        from_status="QUEUED",
                        to_status="FAILED",
                        message="任务未进入运行队列",
                    )
                )
                session.commit()


class _ScheduleSnapshot:
    """Detached values used while APScheduler calls back asynchronously."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    @classmethod
    def from_model(cls, row: Schedule, store: Store | None) -> "_ScheduleSnapshot":
        return cls(
            id=row.id,
            name=row.name,
            interval_minutes=row.interval_minutes,
            first_run_at=row.first_run_at,
            timezone=row.timezone,
            misfire_grace_seconds=row.misfire_grace_seconds,
            created_at=row.created_at,
            updated_at=row.updated_at,
            next_run_at=row.next_run_at,
            store=_ScheduleSnapshot(name=store.name if store else f"store-{row.store_id}"),
        )


def _default_scheduler() -> Any:
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError as exc:  # pragma: no cover - dependency install check
        raise RuntimeError("缺少 APScheduler 依赖，请先完成本地安装") from exc
    return AsyncIOScheduler(timezone=timezone.utc)


def _interval_trigger(*, interval_minutes: int, first_run_at: datetime) -> Any:
    """Fire on the grid ``first_run_at + k × interval``.

    Anchored to an absolute instant rather than a wall clock, so the grid is
    immune to DST and a period above 24 hours genuinely stays above 24 hours —
    which is the whole reason this replaced a cron: Amazon measures its payout
    cap from the previous request, not from midnight.

    An anchor in the past is legitimate and common (editing a running rule).
    APScheduler advances to the next future point on the same grid rather than
    replaying the backlog.
    """

    try:
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 APScheduler 依赖，请先完成本地安装") from exc
    minutes = _validated_interval(interval_minutes)
    return IntervalTrigger(
        minutes=minutes,
        start_date=_as_utc(first_run_at),
        timezone=timezone.utc,
    )


def _validated_interval(value: Any) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"运行间隔不是整数分钟：{value!r}") from exc
    if minutes < 1:
        raise ValueError(f"运行间隔必须至少 1 分钟：{minutes}")
    return minutes


def _anchor_of(schedule: Any) -> datetime:
    value = schedule.first_run_at
    if value is None:
        raise ValueError("排期没有首次运行时间")
    return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    """Normalise SQLite's sometimes-naive datetimes to aware UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _persisted_next_run_as_utc(schedule: Any) -> datetime:
    """Normalise a stored projection to aware UTC.

    Migration 0007 clears ``next_run_at`` for every row, and the heuristic that
    used to recognise pre-0003 local-wall-clock values keyed on ``local_time``,
    which no longer exists.  A naive value can now only be a plain UTC timestamp
    SQLite stripped the offset from.
    """

    value = schedule.next_run_at
    if value is None:
        raise ValueError("排期没有下次运行时间")
    return _as_utc(value)


def _latest_occurrence(schedule: Any, *, now: datetime) -> datetime | None:
    """Latest grid point at or before ``now``, without generating a backlog.

    Pure arithmetic on ``first_run_at + k × interval``.  The cron version had to
    walk the trigger forward from an eight-day cursor; an evenly spaced grid
    gives the same answer with a division, and without that window silently
    capping how far back recovery could see.
    """

    anchor = _anchor_of(schedule)
    interval = timedelta(minutes=_validated_interval(schedule.interval_minutes))
    cutoff = _as_utc(now)
    if cutoff < anchor:
        return None
    elapsed = (cutoff - anchor) // interval
    return anchor + elapsed * interval
