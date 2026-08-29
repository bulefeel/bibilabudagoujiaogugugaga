from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import (
    create_sqlite_engine,
    init_database,
    make_session_factory,
)
from ziniao_automation.models import Run, Schedule, Store, StoreMarketplace
from ziniao_automation.scheduler import ScheduleManager


# 09:00 Asia/Singapore. Amazon counts its 24-hour payout cap from the
# previous request, so schedules are an absolute anchor plus a period now.
SCHEDULE_ANCHOR = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
SCHEDULE_ANCHOR_ISO = "2026-01-01T01:00:00+00:00"


class RecordingAutomation:
    def __init__(self) -> None:
        self.run_ids: list[str] = []

    async def enqueue_run(self, run_id: str) -> None:
        self.run_ids.append(run_id)


class ProjectionJob:
    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.next_run_time = datetime(2026, 8, 13, 1, tzinfo=timezone.utc)


class ProjectionScheduler:
    """Small APScheduler stand-in used to test row-level refresh isolation."""

    def __init__(self) -> None:
        self.jobs: dict[str, ProjectionJob] = {}

    def add_job(self, _func, **kwargs):
        job = ProjectionJob(kwargs["id"])
        self.jobs[job.id] = job
        return job

    def get_jobs(self):
        return list(self.jobs.values())

    def remove_job(self, job_id: str):
        self.jobs.pop(job_id, None)


@pytest.fixture()
def database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'schedule-persistence.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


def seed_daily_schedule(factory, *, created_at: datetime) -> int:
    with factory() as session:
        store = Store(
            name="Persistent schedule store",
            selector_type="oauth",
            selector_value="oauth-persistent-schedule",
            browser_oauth="oauth-persistent-schedule",
            expected_seller_id="EXPECTED-SELLER",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        session.add(
            StoreMarketplace(
                store_id=store.id,
                code="CA",
                domain="sellercentral.amazon.ca",
                currency="CAD",
                enabled=True,
            )
        )
        schedule = Schedule(
            store_id=store.id,
            name="Daily 09:00",
            workflow="amazon_disbursement",
            mode="dry_run",
            first_run_at=SCHEDULE_ANCHOR,
            interval_minutes=1440,
            timezone="Asia/Singapore",
            marketplace_codes=["CA"],
            enabled=True,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(schedule)
        session.commit()
        return schedule.id


def seed_valid_and_corrupt_schedules(factory, *, created_at: datetime) -> tuple[int, int]:
    with factory() as session:
        store = Store(
            name="Mixed schedule store",
            selector_type="oauth",
            selector_value="oauth-mixed-schedule",
            browser_oauth="oauth-mixed-schedule",
            expected_seller_id="EXPECTED-MIXED",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        session.add(
            StoreMarketplace(
                store_id=store.id,
                code="CA",
                domain="sellercentral.amazon.ca",
                currency="CAD",
                enabled=True,
            )
        )
        valid = Schedule(
            store_id=store.id,
            name="Valid schedule",
            workflow="amazon_disbursement",
            mode="dry_run",
            first_run_at=SCHEDULE_ANCHOR,
            interval_minutes=1440,
            timezone="Asia/Singapore",
            marketplace_codes=["CA"],
            workflow_config={"marketplace_codes": ["CA"]},
            workflow_config_version=1,
            enabled=True,
            created_at=created_at,
            updated_at=created_at,
        )
        # Bypass repository validation intentionally: this represents a
        # malformed legacy/imported row that the scheduler must isolate.
        corrupt = Schedule(
            store_id=store.id,
            name="Corrupt schedule",
            workflow="amazon_disbursement",
            mode="dry_run",
            first_run_at=SCHEDULE_ANCHOR,
            interval_minutes=0,
            timezone="Asia/Singapore",
            marketplace_codes=["CA"],
            workflow_config={"marketplace_codes": ["CA"]},
            workflow_config_version=1,
            enabled=True,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add_all((valid, corrupt))
        session.commit()
        return valid.id, corrupt.id


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def test_clock_trigger_persists_the_original_occurrence_and_deduplicates(database):
    async def scenario() -> None:
        created_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
        schedule_id = seed_daily_schedule(database, created_at=created_at)
        occurrence = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
        automation = RecordingAutomation()
        manager = ScheduleManager(database, automation)

        first = await manager.trigger_schedule(
            schedule_id,
            scheduled_for_at=occurrence,
        )
        second = await manager.trigger_schedule(
            schedule_id,
            scheduled_for_at=occurrence,
        )

        assert first is not None
        assert second is None
        assert automation.run_ids == [first]
        with database() as session:
            rows = session.query(Run).filter_by(schedule_id=schedule_id).all()
            assert len(rows) == 1
            assert as_utc(rows[0].scheduled_for_at) == occurrence

    asyncio.run(scenario())


def test_offline_recovery_creates_only_the_latest_missed_occurrence(database):
    """一次停机只补跑最近的那一次，而且只补一次。

    时刻取在 occurrence 之后 10 分钟——在 misfire 宽限（默认 1800 秒）之内，
    所以补跑成立；错过十天里的其余九次一个都不补。
    """

    async def scenario() -> None:
        now = datetime(2026, 8, 13, 1, 10, tzinfo=timezone.utc)
        schedule_id = seed_daily_schedule(
            database,
            created_at=now - timedelta(days=10),
        )
        automation = RecordingAutomation()
        manager = ScheduleManager(database, automation, clock=lambda: now)

        created = await manager.recover_latest_missed(now=now)
        repeated = await manager.recover_latest_missed(now=now)

        assert len(created) == 1
        assert repeated == ()
        assert automation.run_ids == list(created)
        with database() as session:
            rows = session.query(Run).filter_by(schedule_id=schedule_id).all()
            assert len(rows) == 1
            # 09:00 Asia/Singapore is 01:00 UTC.  No run is generated for
            # any of the other nine dates missed during the outage.
            assert as_utc(rows[0].scheduled_for_at) == datetime(
                2026, 8, 13, 1, 0, tzinfo=timezone.utc
            )

    asyncio.run(scenario())


def test_an_occurrence_missed_by_longer_than_the_grace_is_not_caught_up(database):
    """机器关了一夜，早上打开应用不该把凌晨那次补跑掉。

    操作员报的就是这个：装完新版打开页面，之前的定时任务自己跑了起来。
    根因不是执行记录被重置——去重一直是好的——而是
    ``misfire_grace_seconds`` 有两套含义：APScheduler 在进程内按它拒绝迟到的
    触发（最多 30 分钟），而停机补跑这条路完全不看它，11 小时照跑。

    对提现尤其错：亚马逊按**滑动 24 小时**从上一次请求起算，中午补跑会把整个
    窗口拖走，下一次按点的运行反而被拒。等下一次没有损失——余额在亚马逊那边
    继续累积。
    """

    async def scenario() -> None:
        # occurrence 是 01:00 UTC，开机时已经是 11 小时之后。
        now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        schedule_id = seed_daily_schedule(
            database,
            created_at=now - timedelta(days=10),
        )
        automation = RecordingAutomation()
        manager = ScheduleManager(database, automation, clock=lambda: now)

        created = await manager.recover_latest_missed(now=now)

        assert created == ()
        assert automation.run_ids == []
        with database() as session:
            assert session.query(Run).filter_by(schedule_id=schedule_id).count() == 0

    asyncio.run(scenario())


def test_reopening_the_app_repeatedly_never_multiplies_runs(database):
    """反复开关应用不该反复触发——去重和新鲜度两道闸都得在。"""

    async def scenario() -> None:
        first = datetime(2026, 8, 13, 1, 5, tzinfo=timezone.utc)
        schedule_id = seed_daily_schedule(
            database,
            created_at=first - timedelta(days=3),
        )
        automation = RecordingAutomation()

        for offset in (0, 3, 6, 20):
            moment = first + timedelta(minutes=offset)
            manager = ScheduleManager(database, automation, clock=lambda m=moment: m)
            await manager.recover_latest_missed(now=moment)

        with database() as session:
            rows = session.query(Run).filter_by(schedule_id=schedule_id).all()
            assert len(rows) == 1, f"四次启动只该有一个任务，实际 {len(rows)}"

    asyncio.run(scenario())


def test_implicit_clock_callback_uses_planned_time_not_callback_time(database):
    async def scenario() -> None:
        callback_time = datetime(2026, 8, 13, 1, 7, 42, tzinfo=timezone.utc)
        schedule_id = seed_daily_schedule(
            database,
            created_at=callback_time - timedelta(days=2),
        )
        manager = ScheduleManager(
            database,
            RecordingAutomation(),
            clock=lambda: callback_time,
        )

        run_id = await manager.trigger_schedule(schedule_id)

        assert run_id is not None
        with database() as session:
            assert as_utc(session.get(Run, run_id).scheduled_for_at) == datetime(
                2026, 8, 13, 1, 0, tzinfo=timezone.utc
            )

    asyncio.run(scenario())


def test_next_occurrence_is_kept_while_previous_one_is_still_queued(database):
    async def scenario() -> None:
        schedule_id = seed_daily_schedule(
            database,
            created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        automation = RecordingAutomation()
        manager = ScheduleManager(database, automation)

        first = await manager.trigger_schedule(
            schedule_id,
            scheduled_for_at=datetime(2026, 8, 12, 1, tzinfo=timezone.utc),
        )
        second = await manager.trigger_schedule(
            schedule_id,
            scheduled_for_at=datetime(2026, 8, 13, 1, tzinfo=timezone.utc),
        )

        assert first is not None and second is not None
        assert automation.run_ids == [first, second]
        with database() as session:
            rows = (
                session.query(Run)
                .filter_by(schedule_id=schedule_id, status="QUEUED")
                .order_by(Run.scheduled_for_at)
                .all()
            )
            assert [as_utc(row.scheduled_for_at) for row in rows] == [
                datetime(2026, 8, 12, 1, tzinfo=timezone.utc),
                datetime(2026, 8, 13, 1, tzinfo=timezone.utc),
            ]

    asyncio.run(scenario())


def test_saved_next_run_survives_projection_updated_at_noise(database):
    """这条钉的是 updated_at 噪声不能压过 next_run_at。

    时刻取在 occurrence 之后 10 分钟：补跑现在还要过 misfire 新鲜度那一关，
    而本测试要验的不是那一关。
    """

    async def scenario() -> None:
        now = datetime(2026, 8, 13, 1, 10, tzinfo=timezone.utc)
        schedule_id = seed_daily_schedule(
            database,
            created_at=now - timedelta(days=20),
        )
        with database() as session:
            schedule = session.get(Schedule, schedule_id)
            schedule.next_run_at = datetime(2026, 8, 10, 1, tzinfo=timezone.utc)
            # Generic ORM updated_at may reflect a harmless next-run
            # projection write made after the actual missed clock time.
            schedule.updated_at = datetime(2026, 8, 13, 11, tzinfo=timezone.utc)
            session.commit()

        automation = RecordingAutomation()
        manager = ScheduleManager(database, automation, clock=lambda: now)
        created = await manager.recover_latest_missed(now=now)

        assert len(created) == 1
        with database() as session:
            assert as_utc(session.get(Run, created[0]).scheduled_for_at) == datetime(
                2026, 8, 13, 1, tzinfo=timezone.utc
            )

    asyncio.run(scenario())


def test_a_naive_stored_next_run_is_read_as_utc_not_as_a_wall_clock(database):
    """SQLite hands back naive datetimes; that must not shift the grid.

    This replaces a test for a heuristic that no longer exists. Pre-0003 rows
    stored APScheduler's 09:00 +08:00 wall clock after SQLite stripped the
    offset, and the scheduler used to recognise them by comparing against the
    cron's hour and minute. There is no cron to compare against any more, and
    0007 clears next_run_at for every row precisely so that ambiguity is gone —
    a naive value can now only be a UTC instant that lost its marker.

    What still has to hold is that a stored projection does not hide a genuinely
    missed occurrence, which is what the old test was really protecting.
    """

    async def scenario() -> None:
        # 10 分钟，不是 2 小时：本测试要验的是 SQLite 返回的 naive 时间戳，
        # 不是新鲜度限制，所以把间隔收进 misfire 宽限内。
        now = SCHEDULE_ANCHOR + timedelta(days=20, minutes=10)
        schedule_id = seed_daily_schedule(
            database,
            created_at=SCHEDULE_ANCHOR - timedelta(days=1),
        )
        with database() as session:
            schedule = session.get(Schedule, schedule_id)
            # Naive, as SQLite returns it: the grid point 20 days in.
            schedule.next_run_at = (SCHEDULE_ANCHOR + timedelta(days=20)).replace(
                tzinfo=None
            )
            session.commit()

        automation = RecordingAutomation()
        manager = ScheduleManager(database, automation, clock=lambda: now)
        created = await manager.recover_latest_missed(now=now)

        assert len(created) == 1
        with database() as session:
            assert as_utc(session.get(Run, created[0]).scheduled_for_at) == (
                SCHEDULE_ANCHOR + timedelta(days=20)
            )

    asyncio.run(scenario())


def test_refresh_isolates_corrupt_schedule_projection(database):
    async def scenario() -> None:
        created_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
        valid_id, corrupt_id = seed_valid_and_corrupt_schedules(
            database, created_at=created_at
        )
        with database() as session:
            session.get(Schedule, corrupt_id).next_run_at = datetime(
                2026, 8, 12, 1, tzinfo=timezone.utc
            )
            session.commit()

        scheduler = ProjectionScheduler()
        manager = ScheduleManager(
            database,
            RecordingAutomation(),
            scheduler=scheduler,
        )
        report = await manager.refresh()

        assert report.projected_schedule_ids == frozenset({valid_id})
        assert report.failed_schedule_ids == frozenset({corrupt_id})
        assert f"db-schedule:{valid_id}" in scheduler.jobs
        assert f"db-schedule:{corrupt_id}" not in scheduler.jobs
        with database() as session:
            assert session.get(Schedule, corrupt_id).next_run_at is None

    asyncio.run(scenario())


def test_missed_recovery_continues_after_corrupt_schedule(database):
    """一条坏排期不能拖垮同一轮里其余健康排期的补跑。

    时刻同样取在 occurrence 之后 10 分钟：本测试验的是「坏行被跳过、好行照常」，
    不是新鲜度限制。
    """

    async def scenario() -> None:
        now = datetime(2026, 8, 13, 1, 10, tzinfo=timezone.utc)
        valid_id, corrupt_id = seed_valid_and_corrupt_schedules(
            database, created_at=now - timedelta(days=10)
        )
        automation = RecordingAutomation()
        manager = ScheduleManager(database, automation, clock=lambda: now)

        created = await manager.recover_latest_missed(now=now)

        assert len(created) == 1
        assert automation.run_ids == list(created)
        with database() as session:
            valid_runs = session.query(Run).filter_by(schedule_id=valid_id).all()
            corrupt_runs = session.query(Run).filter_by(schedule_id=corrupt_id).all()
            assert len(valid_runs) == 1
            assert corrupt_runs == []

    asyncio.run(scenario())
