from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ziniao_automation.db import init_database
from ziniao_automation.models import NotificationDelivery, RunQueueEntry


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _head_revision() -> str:
    """The current head, read from the scripts rather than hard-coded.

    Spelling the revision out in each assertion turned every new migration into
    a fleet of unrelated red tests, which trains you to edit assertions instead
    of reading them.  What these tests mean is "the database was brought fully
    up to date".
    """

    from alembic.script import ScriptDirectory

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def _upgrade(database: Path, revision: str = "head") -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(PROJECT_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    # env.py gives this environment variable precedence over Config values.
    import os

    previous = os.environ.get("ZINIAO_DATABASE_URL")
    os.environ["ZINIAO_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    try:
        command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("ZINIAO_DATABASE_URL", None)
        else:
            os.environ["ZINIAO_DATABASE_URL"] = previous


def _legacy_0001_database(database: Path) -> sa.Engine:
    """Create the minimum shape consumed by 0002, without any new objects."""

    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE runs (
                id VARCHAR(36) PRIMARY KEY NOT NULL,
                schedule_id INTEGER,
                trigger VARCHAR(16) NOT NULL,
                status VARCHAR(40) NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_runs_schedule_active ON runs (schedule_id, status)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE site_runs (id VARCHAR(36) PRIMARY KEY NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES ('0001')"
        )
        rows = (
            ("scheduled", 1, "schedule", "QUEUED", "2026-08-13 01:00:00"),
            ("manual", None, "manual", "QUEUED", "2026-08-13 01:01:00"),
            ("recovery", None, "recovery", "QUEUED", "2026-08-13 01:02:00"),
            ("approval", 2, "schedule", "WAITING_APPROVAL", "2026-08-13 01:03:00"),
            ("terminal", 3, "schedule", "SUCCEEDED", "2026-08-13 01:04:00"),
        )
        connection.execute(
            sa.text(
                "INSERT INTO runs (id, schedule_id, trigger, status, created_at) "
                "VALUES (:id, :schedule_id, :trigger, :status, :created_at)"
            ),
            [
                {
                    "id": row[0],
                    "schedule_id": row[1],
                    "trigger": row[2],
                    "status": row[3],
                    "created_at": row[4],
                }
                for row in rows
            ],
        )
    return engine


def test_fresh_upgrade_is_idempotent_with_dynamic_0001_metadata(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")

    # This is the same entry point used by the ASGI lifespan and installer.
    init_database(engine)
    # A second head upgrade must be a no-op rather than trying to recreate
    # tables/indexes that dynamic 0001 already produced.
    _upgrade(database)

    inspector = sa.inspect(engine)
    assert "scheduled_for_at" in {
        column["name"] for column in inspector.get_columns("runs")
    }
    assert {"run_queue_entries", "notification_deliveries"}.issubset(
        inspector.get_table_names()
    )
    assert {
        "uq_run_queue_active_run",
        "ix_run_queue_ready_order",
    }.issubset({item["name"] for item in inspector.get_indexes("run_queue_entries")})
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == _head_revision()


def test_existing_0001_only_replays_queued_runs_with_expected_priority(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    engine = _legacy_0001_database(database)

    init_database(engine)

    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT run_id, action, priority, state, scheduled_for_at "
                "FROM run_queue_entries ORDER BY priority, id"
            )
        ).all()
        assert rows == [
            ("recovery", "START", 0, "READY", None),
            ("manual", "START", 10, "READY", None),
            ("scheduled", "START", 100, "READY", None),
        ]
        assert connection.scalar(
            sa.text("SELECT COUNT(*) FROM run_queue_entries WHERE run_id = 'approval'")
        ) == 0
        assert connection.scalar(
            sa.text("SELECT COUNT(*) FROM run_queue_entries WHERE run_id = 'terminal'")
        ) == 0
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == _head_revision()


def test_unversioned_legacy_database_is_adopted_and_upgraded(tmp_path: Path) -> None:
    """Older installers used create_all and could lack an Alembic stamp."""

    database = tmp_path / "unversioned.db"
    engine = _legacy_0001_database(database)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")

    init_database(engine)

    inspector = sa.inspect(engine)
    assert {"run_queue_entries", "notification_deliveries"}.issubset(
        inspector.get_table_names()
    )
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == _head_revision()
        assert connection.scalar(
            sa.text("SELECT COUNT(*) FROM run_queue_entries")
        ) == 3
        assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"


def test_init_database_keeps_in_memory_schema_on_engine_pool() -> None:
    engine = sa.create_engine("sqlite://")

    init_database(engine)
    init_database(engine)

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == _head_revision()
        assert connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'run_queue_entries'"
            )
        ) == 1


def test_queue_active_item_and_schedule_occurrence_are_unique(tmp_path: Path) -> None:
    database = tmp_path / "constraints.db"
    _upgrade(database)
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")

    # Use SQL here to keep the test focused on database barriers without
    # constructing the entire store/schedule object graph.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO ziniao_accounts "
            "(id, display_name, enabled, created_at, updated_at) "
            "VALUES (1, 'account', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO stores "
            "(id, account_id, name, selector_type, selector_value, "
            "identity_confirmed, enabled, raw_profile, created_at, updated_at) "
            "VALUES (1, 1, 'store', 'id', 'profile', 1, 1, '{}', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schedules "
            "(id, store_id, name, workflow, mode, local_time, days_of_week, timezone, "
            "marketplace_codes, enabled, misfire_grace_seconds, created_at, updated_at) "
            "VALUES (1, 1, 'schedule', 'amazon_disbursement', 'dry_run', '09:00', "
            "'mon', 'Asia/Singapore', '[]', 1, 1800, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        for run_id in ("run-one", "run-two"):
            connection.execute(
                sa.text(
                    "INSERT INTO runs "
                    "(id, store_id, schedule_id, workflow, mode, trigger, status, "
                    "requested_by, scheduled_for_at, result_summary, created_at, updated_at) "
                    "VALUES (:id, 1, 1, 'amazon_disbursement', 'dry_run', 'schedule', "
                    "'QUEUED', 'scheduler', :due, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": run_id,
                    "due": (
                        "2026-08-13 01:00:00"
                        if run_id == "run-one"
                        else "2026-08-13 01:01:00"
                    ),
                },
            )

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(
            RunQueueEntry(
                run_id="run-one",
                action="START",
                priority=100,
                state="READY",
                scheduled_for_at=now,
            )
        )
        session.commit()

        session.add(
            RunQueueEntry(
                run_id="run-one",
                action="RECONCILE",
                priority=0,
                state="READY",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


        session.rollback()

        first = session.scalar(
            sa.select(RunQueueEntry).where(RunQueueEntry.run_id == "run-one")
        )
        first.state = "DONE"
        session.commit()
        session.add(
            RunQueueEntry(
                run_id="run-one",
                action="APPROVE",
                priority=10,
                state="READY",
            )
        )
        session.commit()

        session.add(
            NotificationDelivery(
                run_id="run-one",
                dedupe_key="run-one:waiting-approval",
                kind="WAITING_APPROVAL",
            )
        )
        session.commit()
        session.add(
            NotificationDelivery(
                run_id="run-one",
                dedupe_key="run-one:waiting-approval",
                kind="WAITING_APPROVAL",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

def test_0003_converts_legacy_local_next_run_to_utc(tmp_path: Path) -> None:
    database = tmp_path / "legacy-next-run.db"
    _upgrade(database, "0002")
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO ziniao_accounts "
            "(id, display_name, enabled, created_at, updated_at) "
            "VALUES (1, 'account', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO stores "
            "(id, account_id, name, selector_type, selector_value, "
            "identity_confirmed, enabled, raw_profile, created_at, updated_at) "
            "VALUES (1, 1, 'store', 'id', 'profile', 1, 1, '{}', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schedules "
            "(id, store_id, name, workflow, mode, local_time, days_of_week, timezone, "
            "marketplace_codes, enabled, misfire_grace_seconds, next_run_at, "
            "created_at, updated_at) "
            "VALUES (1, 1, 'schedule', 'amazon_disbursement', 'dry_run', '09:10', "
            "'mon,tue,wed,thu,fri', 'Asia/Singapore', '[]', 1, 1800, "
            "'2026-08-17 09:10:00', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    _upgrade(database)

    with engine.connect() as connection:
        value = connection.scalar(
            sa.text("SELECT next_run_at FROM schedules WHERE id = 1")
        )
        parsed = datetime.fromisoformat(str(value))
        assert parsed == datetime(2026, 8, 17, 1, 10)
        assert connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        ) == _head_revision()


def _database_with_cycle_wide_guard_unique(database: Path) -> sa.Engine:
    """A pre-0004 install: operation_guards unique per settlement CYCLE.

    Written out by hand because 0001 builds from live model metadata, so a fresh
    upgrade never reproduces the shape that real installed databases carry.
    """

    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        # The batch rebuild copies rows with foreign keys enforced, so the
        # parents have to exist exactly as they do in a real installation.
        for table, key in (("runs", "run-1"), ("site_runs", "site-1"), ("stores", "3")):
            connection.exec_driver_sql(
                f"CREATE TABLE {table} (id VARCHAR(36) PRIMARY KEY NOT NULL)"
            )
            connection.exec_driver_sql(
                f"INSERT INTO {table} (id) VALUES ('{key}')"
            )
        connection.exec_driver_sql(
            """
            CREATE TABLE operation_guards (
                id VARCHAR(36) NOT NULL,
                guard_key VARCHAR(255) NOT NULL,
                run_id VARCHAR(36) NOT NULL,
                site_run_id VARCHAR(36) NOT NULL,
                store_id INTEGER NOT NULL,
                workflow VARCHAR(80) NOT NULL,
                marketplace_code VARCHAR(2) NOT NULL,
                settlement_key VARCHAR(180) NOT NULL,
                state VARCHAR(32) NOT NULL,
                amount NUMERIC(18, 2) NOT NULL,
                currency VARCHAR(3) NOT NULL,
                plan_hash VARCHAR(64) NOT NULL,
                snapshot_hash VARCHAR(64) NOT NULL,
                armed_at DATETIME NOT NULL,
                submitted_at DATETIME,
                confirmed_at DATETIME,
                last_reconciled_at DATETIME,
                failure_reason TEXT,
                metadata JSON NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_financial_operation UNIQUE (
                    store_id, workflow, marketplace_code, settlement_key
                ),
                CONSTRAINT ck_guard_state CHECK (
                    state IN ('ARMED','SUBMITTED','CONFIRMED','UNCERTAIN','CANCELLED')
                ),
                CONSTRAINT ck_guard_amount CHECK (amount >= 0),
                UNIQUE (guard_key),
                FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE RESTRICT,
                FOREIGN KEY(site_run_id) REFERENCES site_runs (id) ON DELETE RESTRICT,
                FOREIGN KEY(store_id) REFERENCES stores (id) ON DELETE RESTRICT
            )
            """
        )
        for column in ("run_id", "site_run_id", "state"):
            connection.exec_driver_sql(
                f"CREATE INDEX ix_operation_guards_{column} "
                f"ON operation_guards ({column})"
            )
        connection.exec_driver_sql(
            "INSERT INTO operation_guards (id,guard_key,run_id,site_run_id,store_id,workflow,marketplace_code,settlement_key,state,amount,currency,plan_hash,snapshot_hash,armed_at,submitted_at,confirmed_at,last_reconciled_at,failure_reason,metadata,created_at,updated_at) VALUES "
            "('guard-1','key-day-1','run-1','site-1',3,'amazon_disbursement',"
            "'UK','2026/8/10 - 至今','UNCERTAIN',610.03,'GBP',"
            "'a','b',CURRENT_TIMESTAMP,NULL,NULL,NULL,NULL,'{}',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES ('0003')"
        )
    return engine


def test_0004_drops_the_cycle_wide_guard_unique_and_keeps_everything_else(
    tmp_path: Path,
) -> None:
    """The deadlock fix, against a database that actually has the constraint.

    An Amazon settlement cycle stays open for weeks and only rolls over once a
    payout succeeds, so a cycle-wide unique key let one leftover guard block the
    very payout that would have cleared it — the marketplace became permanently
    unpayable.  ``guard_key`` already includes the disbursement day and carries
    the invariant on its own.

    The rest of the assertions exist because the rebuild is the risk: SQLite
    cannot drop a constraint, so the table is recreated, and a rebuild that
    reflects instead of declaring silently loses CHECK constraints and indexes
    from a table that holds money.
    """

    database = tmp_path / "pre_0004.db"
    engine = _database_with_cycle_wide_guard_unique(database)

    _upgrade(database)

    with engine.connect() as connection:
        schema = connection.scalar(
            sa.text("SELECT sql FROM sqlite_master WHERE name = 'operation_guards'")
        )
        assert "uq_financial_operation" not in schema
        assert "ck_guard_state" in schema
        assert "ck_guard_amount" in schema
        assert "UNIQUE (guard_key)" in schema
        assert schema.count("FOREIGN KEY") == 3

        indexes = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT name FROM sqlite_master WHERE tbl_name = 'operation_guards' "
                    "AND type = 'index' AND name NOT LIKE 'sqlite_%'"
                )
            )
        }
        assert indexes == {
            "ix_operation_guards_run_id",
            "ix_operation_guards_site_run_id",
            "ix_operation_guards_state",
        }

        # The money row survived the rebuild untouched.
        assert connection.execute(
            sa.text("SELECT guard_key, state, amount FROM operation_guards")
        ).all() == [("key-day-1", "UNCERTAIN", 610.03)]

    # The next day is a different guard_key and must now be insertable, which
    # is the whole point: the leftover row above no longer blocks UK forever.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO operation_guards (id,guard_key,run_id,site_run_id,store_id,workflow,marketplace_code,settlement_key,state,amount,currency,plan_hash,snapshot_hash,armed_at,submitted_at,confirmed_at,last_reconciled_at,failure_reason,metadata,created_at,updated_at) VALUES "
            "('guard-2','key-day-2','run-1','site-1',3,'amazon_disbursement',"
            "'UK','2026/8/10 - 至今','ARMED',610.03,'GBP',"
            "'a','b',CURRENT_TIMESTAMP,NULL,NULL,NULL,NULL,'{}',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )

    # Same guard_key twice is still one operation and is still refused.
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO operation_guards (id,guard_key,run_id,site_run_id,store_id,workflow,marketplace_code,settlement_key,state,amount,currency,plan_hash,snapshot_hash,armed_at,submitted_at,confirmed_at,last_reconciled_at,failure_reason,metadata,created_at,updated_at) VALUES "
                "('guard-3','key-day-2','run-1','site-1',3,'amazon_disbursement',"
                "'UK','2026/8/10 - 至今','ARMED',610.03,'GBP',"
                "'a','b',CURRENT_TIMESTAMP,NULL,NULL,NULL,NULL,'{}',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )


def _database_with_payout_account_baseline(database: Path) -> sa.Engine:
    """A pre-0005 install: store_marketplaces still carries the baseline."""

    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE stores (id INTEGER PRIMARY KEY NOT NULL)"
        )
        connection.exec_driver_sql("INSERT INTO stores (id) VALUES (3)")
        connection.exec_driver_sql(
            """
            CREATE TABLE store_marketplaces (
                id INTEGER NOT NULL,
                store_id INTEGER NOT NULL,
                code VARCHAR(2) NOT NULL,
                domain VARCHAR(255) NOT NULL,
                currency VARCHAR(3) NOT NULL,
                expected_payment_account VARCHAR(180),
                enabled BOOLEAN NOT NULL,
                verified_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_store_marketplace_code UNIQUE (store_id, code),
                CONSTRAINT ck_v1_marketplace_code CHECK (code IN ('CA', 'UK', 'AU')),
                CONSTRAINT ck_marketplace_currency CHECK (length(currency) = 3),
                FOREIGN KEY(store_id) REFERENCES stores (id) ON DELETE CASCADE
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_store_marketplaces_store_id "
            "ON store_marketplaces (store_id)"
        )
        connection.exec_driver_sql(
            "INSERT INTO store_marketplaces VALUES "
            "(1,3,'UK','sellercentral.amazon.co.uk','GBP','003',1,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE operation_guards (id VARCHAR(36) PRIMARY KEY NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES ('0004')"
        )
    return engine


def test_0005_drops_the_baseline_and_adds_the_observed_tail(tmp_path: Path) -> None:
    """The gate goes; the evidence stays.

    A locally stored "expected" payout tail could only refuse a payout, never
    redirect one — Amazon owns the destination and this automation just presses
    the button.  What is worth keeping is what Amazon actually showed, so that
    moves to the money record.

    The structural assertions matter because ``site_runs`` references
    ``store_marketplaces``: a batch rebuild would trip that foreign key and
    silently drop constraints, which is why the migration uses SQLite's native
    DROP COLUMN instead.
    """

    database = tmp_path / "pre_0005.db"
    engine = _database_with_payout_account_baseline(database)

    _upgrade(database)

    with engine.connect() as connection:
        schema = connection.scalar(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE name = 'store_marketplaces'"
            )
        )
        assert "expected_payment_account" not in schema
        assert "verified_at" not in schema
        # Everything else about the table survives.
        assert "ck_v1_marketplace_code" in schema
        assert "ck_marketplace_currency" in schema
        assert "uq_store_marketplace_code" in schema
        assert schema.count("FOREIGN KEY") == 1
        assert connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
                "AND name = 'ix_store_marketplaces_store_id'"
            )
        ) == 1
        assert connection.execute(
            sa.text("SELECT code, currency, enabled FROM store_marketplaces")
        ).all() == [("UK", "GBP", 1)]

        guards = connection.scalar(
            sa.text("SELECT sql FROM sqlite_master WHERE name = 'operation_guards'")
        )
        assert "payout_account_tail" in guards
        assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"
