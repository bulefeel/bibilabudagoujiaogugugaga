"""SQLite-backed schedule projection for APScheduler.

SQLite remains the source of truth.  APScheduler jobs are rebuilt from the
``schedules`` table on every process start and after every schedule mutation.
No pickle/job-store state is used.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import Run, RunEvent, Schedule, Store
from .repositories import ConflictError

logger = logging.getLogger(__name__)


MISFIRE_GRACE_SECONDS = 1800


class RuntimeService(Protocol):
    async def enqueue_run(self, run_id: str) -> None: ...


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
    ) -> None:
        self.session_factory = session_factory
        self.automation_service = automation_service
        self._scheduler = scheduler
        self._scheduler_factory = scheduler_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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

    async def refresh(self) -> None:
        """Rebuild all managed jobs and persist the next fire times."""

        async with self._refresh_lock:
            schedules = self._enabled_schedules()
            wanted: set[str] = set()
            updates: list[tuple[int, datetime | None]] = []
            for row in schedules:
                job_id = self.job_id(row.id)
                wanted.add(job_id)
                hour, minute = _parse_local_time(row.local_time)
                timezone_value = _zone(row.timezone)
                trigger = _cron_trigger(
                    day_of_week=row.days_of_week,
                    hour=hour,
                    minute=minute,
                    timezone_value=timezone_value,
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
                updates.append((row.id, getattr(job, "next_run_time", None)))
            for job in tuple(self.scheduler.get_jobs()):
                if str(job.id).startswith("db-schedule:") and job.id not in wanted:
                    self.scheduler.remove_job(job.id)
            self._set_next_runs(updates, wanted)

    async def refresh_schedule(self, schedule_id: int) -> None:
        """Refresh after an API mutation; a full rebuild is intentionally cheap."""

        del schedule_id
        if self._started:
            await self.refresh()

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
        """Atomically create one due run, then hand it to the serial runtime."""

        run_id: str | None = None
        occurrence = _as_utc(scheduled_for_at) if scheduled_for_at else None
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
                    occurrence = _latest_occurrence(
                        schedule,
                        now=_as_utc(self._clock()),
                    )
                if occurrence is None:
                    return None
                existing = session.scalar(
                    select(Run.id)
                    .where(
                        Run.schedule_id == schedule.id,
                        Run.scheduled_for_at == occurrence,
                    )
                    .limit(1)
                )
                if existing:
                    logger.info(
                        "schedule_occurrence_already_persisted schedule_id=%s "
                        "scheduled_for_at=%s run_id=%s",
                        schedule.id,
                        occurrence.isoformat(),
                        existing,
                    )
                    return None
                store = session.get(Store, schedule.store_id)
                if store is None or not store.enabled or not store.identity_confirmed:
                    self._record_skipped(
                        session, schedule,
                        "店铺未启用或卖家身份尚未确认",
                        scheduled_for_at=occurrence,
                    )
                    session.commit()
                    return None
                from .repositories import ScheduleRepository

                run = ScheduleRepository(session).create_run_from_schedule(
                    schedule.id,
                    require_enabled=True,
                    trigger="schedule",
                    requested_by="scheduler",
                    scheduled_for_at=occurrence,
                )
                session.commit()
                run_id = run.id
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
            except ConflictError:
                session.rollback()
                return None
            except (ValueError, LookupError) as exc:
                session.rollback()
                schedule = session.get(Schedule, schedule_id)
                if schedule is not None:
                    self._record_skipped(
                        session,
                        schedule,
                        str(exc),
                        scheduled_for_at=occurrence,
                    )
                    session.commit()
                return None
        if run_id is not None:
            try:
                await self.automation_service.enqueue_run(run_id)
            except Exception as exc:
                self._mark_enqueue_failure(run_id, exc)
                raise
        return run_id

    async def recover_latest_missed(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Create only the most recent occurrence missed while offline.

        The method is deliberately repeatable: a persisted occurrence is
        found by ``(schedule_id, scheduled_for_at)`` and is not recreated,
        regardless of whether that earlier run is waiting, running or already
        terminal.  We never enumerate every missed day.
        """

        observed_at = _as_utc(now or self._clock())
        created: list[str] = []
        async with self._trigger_lock:
            for schedule in self._enabled_schedules():
                occurrence = _latest_occurrence(schedule, now=observed_at)
                if occurrence is None:
                    continue
                # ``next_run_at`` is the last projection persisted while the
                # process was healthy, so it is the earliest occurrence that
                # could have been missed.  ``updated_at`` is intentionally not
                # used: refreshing next_run_at itself updates that generic
                # timestamp and would otherwise hide a real offline run.
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
                run_id = await self._trigger_schedule_locked(
                    schedule.id,
                    scheduled_for_at=occurrence,
                )
                if run_id is not None:
                    created.append(run_id)
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

            run = ScheduleRepository(session).create_run_from_schedule(
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
            local_time=row.local_time,
            days_of_week=row.days_of_week,
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


def _cron_trigger(*, day_of_week: str, hour: int, minute: int, timezone_value: ZoneInfo) -> Any:
    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 APScheduler 依赖，请先完成本地安装") from exc
    return CronTrigger(
        day_of_week=day_of_week,
        hour=hour,
        minute=minute,
        timezone=timezone_value,
    )


def _parse_local_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"计划时间格式不合法：{value}") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"计划时间超出范围：{value}")
    return hour, minute


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"计划时区不存在：{value}") from exc


def _as_utc(value: datetime) -> datetime:
    """Normalise SQLite's sometimes-naive datetimes to aware UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _persisted_next_run_as_utc(schedule: Any) -> datetime:
    """Read both current UTC and pre-fix local-wall-clock projections.

    Older builds assigned APScheduler's zoned ``next_run_time`` directly to a
    SQLite column.  SQLite discarded the offset and retained e.g. ``09:10``
    instead of the intended ``01:10 UTC``.  The cron minute/hour provide a
    narrow, deterministic signature for that legacy representation.  New
    values are persisted as UTC and therefore normally do not match that wall
    clock signature.
    """

    value = schedule.next_run_at
    if value is None:
        raise ValueError("排期没有下次运行时间")
    if value.tzinfo is not None:
        return _as_utc(value)
    hour, minute = _parse_local_time(schedule.local_time)
    if (value.hour, value.minute) == (hour, minute):
        return value.replace(tzinfo=_zone(schedule.timezone)).astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def _latest_occurrence(schedule: Any, *, now: datetime) -> datetime | None:
    """Return the latest cron occurrence, without generating an old backlog."""

    hour, minute = _parse_local_time(schedule.local_time)
    trigger = _cron_trigger(
        day_of_week=schedule.days_of_week,
        hour=hour,
        minute=minute,
        timezone_value=_zone(schedule.timezone),
    )
    cutoff = _as_utc(now)
    cursor = cutoff - timedelta(days=8)
    previous: datetime | None = None
    candidate = trigger.get_next_fire_time(None, cursor)
    while candidate is not None and _as_utc(candidate) <= cutoff:
        previous = _as_utc(candidate)
        candidate = trigger.get_next_fire_time(candidate, candidate)
    return previous
