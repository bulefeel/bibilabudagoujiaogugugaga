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


def _downgrade(database: Path, revision: str) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(PROJECT_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    # env.py gives this environment variable precedence over Config values.
    import os

    previous = os.environ.get("ZINIAO_DATABASE_URL")
    os.environ["ZINIAO_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    try:
        command.downgrade(config, revision)
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


def test_0006_upgrades_real_legacy_shape_without_rebuilding_schedule_audit_links(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-0005.db"
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE schedules ("
            "id INTEGER PRIMARY KEY, marketplace_codes JSON NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE runs ("
            "id VARCHAR(36) PRIMARY KEY, schedule_id INTEGER, "
            "workflow VARCHAR(80) NOT NULL, result_summary JSON NOT NULL, "
            "FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE SET NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES ('0005')"
        )
        connection.exec_driver_sql(
            "INSERT INTO schedules(id, marketplace_codes) VALUES "
            "(1, '[\"UK\"]')"
        )
        # The schedule was edited to UK after this historical CA run.  The
        # migration must preserve the run's own captured input.
        connection.exec_driver_sql(
            "INSERT INTO runs(id, schedule_id, workflow, result_summary) VALUES "
            "('run-legacy', 1, 'amazon_disbursement', "
            "'{\"requested_marketplaces\":[\"CA\"]}')"
        )
    engine.dispose()

    # Partial historical fixture: exercise its migration chain, not the newer
    # full-store history schema (covered by test_store_history_migration.py).
    _upgrade(database, "0010")

    upgraded = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with upgraded.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar() == "0010"
        assert connection.exec_driver_sql(
            "SELECT workflow_config FROM schedules WHERE id=1"
        ).scalar() == '{"marketplace_codes": ["UK"]}'
        assert connection.exec_driver_sql(
            "SELECT workflow_config FROM runs WHERE id='run-legacy'"
        ).scalar() == '{"marketplace_codes": ["CA"]}'
        foreign_keys = connection.exec_driver_sql(
            "PRAGMA foreign_key_list(schedules)"
        ).fetchall()
        assert any(row[2] == "schedule_batches" and row[3] == "batch_id" for row in foreign_keys)
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
    upgraded.dispose()


def test_0006_downgrade_upgrade_round_trip_preserves_rows_and_constraints(
    tmp_path: Path,
) -> None:
    """0006 is reversible without firing cascades or losing audit links.

    The enabled/unconfirmed store is intentional: 0006 permits it for a
    workflow that does not require seller identity.  Downgrading to 0005 must
    disable that row before restoring the old database CHECK, rather than
    asserting an identity that was never verified.
    """

    database = tmp_path / "round-trip-0006.db"
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    init_database(engine)
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
            "VALUES "
            "(1, 1, 'unprofiled', 'id', 'profile-1', 0, 1, '{}', "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "(2, 1, 'profiled', 'id', 'profile-2', 1, 1, '{}', "
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schedule_batches "
            "(id, request_id, definition_hash, definition_json, schedule_count, "
            " created_at, updated_at) "
            "VALUES (7, '00000000-0000-0000-0000-000000000007', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schedules "
            "(id, store_id, batch_id, batch_order, name, workflow, mode, "
            " first_run_at, interval_minutes, timezone, marketplace_codes, "
            " workflow_config, workflow_config_version, enabled, "
            " misfire_grace_seconds, created_at, updated_at) "
            "VALUES (11, 2, 7, 1, 'schedule', 'amazon_disbursement', "
            "'dry_run', '2026-01-01 01:00:00+00:00', 1440, 'Asia/Singapore', '[\"CA\",\"UK\"]', "
            "'{\"marketplace_codes\":[\"CA\",\"UK\"]}', 1, 1, 1800, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO runs "
            "(id, store_id, schedule_id, workflow, mode, trigger, status, "
            " requested_by, scheduled_for_at, result_summary, workflow_config, "
            " workflow_config_version, created_at, updated_at) "
            "VALUES ('run-round-trip', 2, 11, 'amazon_disbursement', "
            "'dry_run', 'schedule', 'QUEUED', 'scheduler', "
            "'2026-08-25 01:00:00', "
            "'{\"requested_marketplaces\":[\"UK\"]}', "
            "'{\"marketplace_codes\":[\"UK\"]}', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO run_queue_entries "
            "(id, run_id, action, priority, business_priority, target_order, "
            " state, scheduled_for_at, enqueued_at, available_at, created_at, "
            " updated_at) "
            "VALUES (21, 'run-round-trip', 'START', 100, 1, 1, 'READY', "
            "'2026-08-25 01:00:00', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    _downgrade(database, "0005")

    downgraded = sa.create_engine(f"sqlite:///{database.as_posix()}")
    inspector = sa.inspect(downgraded)
    assert "schedule_batches" not in inspector.get_table_names()
    assert not {
        "batch_id",
        "batch_order",
        "workflow_config",
        "workflow_config_version",
    } & {column["name"] for column in inspector.get_columns("schedules")}
    assert not {"workflow_config", "workflow_config_version"} & {
        column["name"] for column in inspector.get_columns("runs")
    }
    assert not {"business_priority", "target_order", "batch_scope_id"} & {
        column["name"] for column in inspector.get_columns("run_queue_entries")
    }
    assert {
        item["name"] for item in inspector.get_check_constraints("stores")
    } >= {"ck_store_enabled_requires_identity"}
    queue_indexes = {item["name"]: item for item in inspector.get_indexes("run_queue_entries")}
    assert queue_indexes["ix_run_queue_ready_order"]["column_names"] == [
        "state",
        "priority",
        "scheduled_for_at",
        "enqueued_at",
        "id",
    ]
    with downgraded.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0005"
        assert connection.execute(
            sa.text("SELECT id, enabled, identity_confirmed FROM stores ORDER BY id")
        ).all() == [(1, 0, 0), (2, 1, 1)]
        assert connection.execute(
            sa.text("SELECT id, store_id FROM schedules")
        ).all() == [(11, 2)]
        assert connection.execute(
            sa.text("SELECT id, schedule_id FROM runs")
        ).all() == [("run-round-trip", 11)]
        assert connection.execute(
            sa.text("SELECT id, run_id FROM run_queue_entries")
        ).all() == [(21, "run-round-trip")]
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"
    downgraded.dispose()

    _upgrade(database)

    upgraded = sa.create_engine(f"sqlite:///{database.as_posix()}")
    inspector = sa.inspect(upgraded)
    expected_checks = {
        "schedules": {
            "ck_schedule_workflow_config_version",
            "ck_schedule_batch_order",
        },
        "runs": {"ck_run_workflow_config_version"},
        "run_queue_entries": {
            "ck_run_queue_business_priority",
            "ck_run_queue_target_order",
            "ck_run_queue_batch_scope_id",
        },
    }
    for table, expected in expected_checks.items():
        assert {
            item["name"] for item in inspector.get_check_constraints(table)
        } >= expected
    assert "ck_store_enabled_requires_identity" not in {
        item["name"] for item in inspector.get_check_constraints("stores")
    }
    upgraded_queue_indexes = {
        item["name"]: item for item in inspector.get_indexes("run_queue_entries")
    }
    assert upgraded_queue_indexes["ix_run_queue_ready_order"]["column_names"] == [
        "state",
        "priority",
        "scheduled_for_at",
        "business_priority",
        "enqueued_at",
        "batch_scope_id",
        "target_order",
        "id",
    ]
    with upgraded.begin() as connection:
        # The exact run snapshot wins over the mutable schedule during backfill.
        assert connection.scalar(
            sa.text("SELECT workflow_config FROM schedules WHERE id=11")
        ) == '{"marketplace_codes": ["CA", "UK"]}'
        assert connection.scalar(
            sa.text("SELECT workflow_config FROM runs WHERE id='run-round-trip'")
        ) == '{"marketplace_codes": ["UK"]}'
        assert connection.execute(
            sa.text(
                "SELECT business_priority, target_order, batch_scope_id "
                "FROM run_queue_entries WHERE id=21"
            )
        ).one() == (1, 2147483647, None)
        # Once 0006 is active again, an identity-optional workflow can re-enable
        # the unprofiled store at the database layer.
        connection.exec_driver_sql("UPDATE stores SET enabled=1 WHERE id=1")
        assert connection.scalar(
            sa.text("SELECT enabled FROM stores WHERE id=1")
        ) == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"
    upgraded.dispose()

    # Exercise the requested 0005 -> 0006 -> 0005 sequence as one continuous
    # installed-database round trip, not only each direction in isolation.
    _downgrade(database, "0005")
    round_tripped = sa.create_engine(f"sqlite:///{database.as_posix()}")
    round_trip_inspector = sa.inspect(round_tripped)
    assert "schedule_batches" not in round_trip_inspector.get_table_names()
    assert not {
        "business_priority",
        "target_order",
        "batch_scope_id",
    } & {
        column["name"]
        for column in round_trip_inspector.get_columns("run_queue_entries")
    }
    with round_tripped.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0005"
        assert connection.execute(
            sa.text("SELECT id, schedule_id FROM runs")
        ).all() == [("run-round-trip", 11)]
        assert connection.execute(
            sa.text("SELECT id, run_id FROM run_queue_entries")
        ).all() == [(21, "run-round-trip")]
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"
    round_tripped.dispose()


@pytest.mark.parametrize(
    ("statement", "constraint_name"),
    [
        (
            "UPDATE schedules SET workflow_config_version=0 WHERE id=11",
            "ck_schedule_workflow_config_version",
        ),
        (
            "UPDATE schedules SET batch_order=0 WHERE id=11",
            "ck_schedule_batch_order",
        ),
        (
            "UPDATE runs SET workflow_config_version=0 WHERE id='run-round-trip'",
            "ck_run_workflow_config_version",
        ),
        (
            "UPDATE run_queue_entries SET business_priority=-1 WHERE id=21",
            "ck_run_queue_business_priority",
        ),
        (
            "UPDATE run_queue_entries SET target_order=0 WHERE id=21",
            "ck_run_queue_target_order",
        ),
        (
            "UPDATE run_queue_entries SET batch_scope_id=0 WHERE id=21",
            "ck_run_queue_batch_scope_id",
        ),
    ],
)
def test_0006_upgraded_checks_reject_invalid_values(
    tmp_path: Path,
    statement: str,
    constraint_name: str,
) -> None:
    """An upgraded 0005 database enforces the same new checks as create_all."""

    database = tmp_path / f"constraint-{constraint_name}.db"
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    init_database(engine)
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
            "INSERT INTO schedule_batches "
            "(id, request_id, definition_hash, definition_json, schedule_count, "
            "created_at, updated_at) VALUES "
            "(1, '00000000-0000-0000-0000-000000000001', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schedules "
            "(id, store_id, batch_id, batch_order, name, workflow, mode, "
            "first_run_at, interval_minutes, timezone, marketplace_codes, "
            "workflow_config, workflow_config_version, enabled, "
            "misfire_grace_seconds, created_at, updated_at) VALUES "
            "(11, 1, 1, 1, 'schedule', 'amazon_disbursement', 'dry_run', "
            "'2026-01-01 01:00:00+00:00', 1440, 'Asia/Singapore', '[\"CA\"]', "
            "'{\"marketplace_codes\":[\"CA\"]}', 1, 1, 1800, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO runs "
            "(id, store_id, schedule_id, workflow, mode, trigger, status, "
            "requested_by, result_summary, workflow_config, "
            "workflow_config_version, created_at, updated_at) VALUES "
            "('run-round-trip', 1, 11, 'amazon_disbursement', 'dry_run', "
            "'manual', 'QUEUED', 'admin', '{}', "
            "'{\"marketplace_codes\":[\"CA\"]}', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO run_queue_entries "
            "(id, run_id, action, priority, business_priority, target_order, "
            "state, enqueued_at, available_at, created_at, updated_at) VALUES "
            "(21, 'run-round-trip', 'START', 10, 1, 1, 'READY', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    # Produce a true 0005-shaped database and exercise the upgrade path rather
    # than only testing the schema emitted directly by current ORM metadata.
    _downgrade(database, "0005")
    _upgrade(database)

    upgraded = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with pytest.raises(IntegrityError, match=constraint_name):
        with upgraded.begin() as connection:
            connection.exec_driver_sql(statement)
    upgraded.dispose()


def test_0006_failed_downgrade_rolls_back_every_schema_change(tmp_path: Path) -> None:
    """A failed integrity check leaves a complete 0006 database, not a hybrid."""

    database = tmp_path / "failed-downgrade-0006.db"
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    init_database(engine)
    engine.dispose()
    # 坏数据必须在**降到 0006 之后**才注入。每一条用整表重建配方的迁移都会在
    # 收尾跑 PRAGMA foreign_key_check，而那个检查是全库范围的，所以一条预先存在
    # 的坏外键会让降级链上第一条重建型迁移就中止——于是永远走不到 0006，这个
    # 测试要验的东西一次都没被执行。
    _downgrade(database, "0006")
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with engine.connect() as connection:
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "INSERT INTO stores "
            "(id, account_id, name, selector_type, selector_value, "
            "identity_confirmed, enabled, raw_profile, created_at, updated_at) "
            "VALUES (1, 999, 'broken-parent-fixture', 'id', 'broken-parent', "
            "0, 1, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
    engine.dispose()

    with pytest.raises(
        RuntimeError, match="invalid foreign-key references"
    ):
        _downgrade(database, "0005")

    unchanged = sa.create_engine(f"sqlite:///{database.as_posix()}")
    inspector = sa.inspect(unchanged)
    assert "schedule_batches" in inspector.get_table_names()
    assert {
        "batch_id",
        "batch_order",
        "workflow_config",
        "workflow_config_version",
    }.issubset({column["name"] for column in inspector.get_columns("schedules")})
    assert {"business_priority", "target_order", "batch_scope_id"}.issubset(
        {
            column["name"]
            for column in inspector.get_columns("run_queue_entries")
        }
    )
    assert "ck_store_enabled_requires_identity" not in {
        item["name"] for item in inspector.get_check_constraints("stores")
    }
    with unchanged.connect() as connection:
        # 停在 0006：上面已经先降到了 0006，随后 0006 自己的降级因为完整性检查
        # 失败并整体回滚，所以标记原地不动。
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0006"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"
    unchanged.dispose()


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

    # Partial historical fixture: exercise its migration chain, not the newer
    # full-store history schema (covered by test_store_history_migration.py).
    _upgrade(database, "0010")

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
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0010"


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
            "(id, store_id, name, workflow, mode, first_run_at, interval_minutes, timezone, "
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
            "(id, store_id, name, workflow, mode, first_run_at, interval_minutes, timezone, "
            "marketplace_codes, enabled, misfire_grace_seconds, next_run_at, "
            "created_at, updated_at) "
            "VALUES (1, 1, 'schedule', 'amazon_disbursement', 'dry_run', "
            "'2026-01-01 01:00:00+00:00', 1440, 'Asia/Singapore', '[]', 1, 1800, "
            "'2026-08-17 09:10:00', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    # Stops at 0006 on purpose. 0003 is what this test is about, and 0007 clears
    # next_run_at for every row — running to head would erase the very value
    # being measured. 0007's own handling of it is covered separately.
    _upgrade(database, "0006")

    with engine.connect() as connection:
        value = connection.scalar(
            sa.text("SELECT next_run_at FROM schedules WHERE id = 1")
        )
        parsed = datetime.fromisoformat(str(value))
        assert parsed == datetime(2026, 8, 17, 1, 10)
        assert connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        ) == "0006"


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

    # Partial historical fixture: exercise its migration chain, not the newer
    # full-store history schema (covered by test_store_history_migration.py).
    _upgrade(database, "0010")

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

    # Partial historical fixture: exercise its migration chain, not the newer
    # full-store history schema (covered by test_store_history_migration.py).
    _upgrade(database, "0010")

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
        # UK 是这个 fixture 自己建的，必须原样活过 0005 的改表；DE 那行是链上
        # 更后面的 0010 给每个店铺补的，一并断言出来，免得它悄悄消失也没人发现。
        assert connection.execute(
            sa.text(
                "SELECT code, currency, enabled FROM store_marketplaces "
                "ORDER BY code"
            )
        ).all() == [("DE", "EUR", 1), ("UK", "GBP", 1)]

        guards = connection.scalar(
            sa.text("SELECT sql FROM sqlite_master WHERE name = 'operation_guards'")
        )
        assert "payout_account_tail" in guards
        assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"


def test_0007_backfills_an_anchor_from_the_old_wall_clock_and_clears_projections(
    tmp_path: Path,
) -> None:
    """Every existing rule keeps the time of day it already ran at.

    Changing the period to something that clears Amazon's rolling 24-hour cap is
    a per-schedule decision an operator makes deliberately; a migration must not
    make it for them. So the anchor becomes the next occurrence of the rule's
    own ``local_time`` and the period starts at the daily cadence it already had.

    ``next_run_at`` is wiped because startup projection recomputes it — and
    because that also retires the pre-0003 rows that stored a local wall clock
    in a UTC column, whose detection keyed on the ``local_time`` being dropped.
    """

    database = tmp_path / "cron-to-interval.db"
    # 0001 builds tables from live model metadata, so a fresh database is
    # already the new shape. Use 0007's own downgrade to produce a genuine
    # pre-migration cron table rather than hand-rolling one that might drift
    # from what a real installation looks like.
    _upgrade(database)
    _downgrade(database, "0006")
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
            "(id, store_id, name, workflow, mode, local_time, days_of_week, "
            "timezone, marketplace_codes, workflow_config, "
            "workflow_config_version, enabled, misfire_grace_seconds, "
            "next_run_at, created_at, updated_at) "
            "VALUES (1, 1, 'daily', 'amazon_disbursement', 'dry_run', '14:30', "
            "'mon,tue,wed,thu,fri', 'Asia/Singapore', '[]', '{}', 1, 1, 1800, "
            "'2020-01-01 00:00:00', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    _upgrade(database)

    upgraded = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with upgraded.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(schedules)")
        }
        assert "local_time" not in columns
        assert "days_of_week" not in columns
        assert {"first_run_at", "interval_minutes"} <= columns

        anchor_text, interval, next_run = connection.exec_driver_sql(
            "SELECT first_run_at, interval_minutes, next_run_at FROM schedules WHERE id=1"
        ).one()
        anchor = datetime.fromisoformat(str(anchor_text))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)

        assert interval == 1440
        assert next_run is None, "陈旧的投影必须清掉，由启动时重算"
        assert anchor > datetime.now(timezone.utc), "锚点必须落在未来，否则一升级就立刻补跑"
        # 14:30 Asia/Singapore == 06:30 UTC — same time of day as before.
        assert (anchor.hour, anchor.minute) == (6, 30)
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
    upgraded.dispose()


def test_0007_round_trips_back_to_the_cron_shape(tmp_path: Path) -> None:
    """Downgrade restores the wall clock the anchor came from.

    Weekday selection cannot be recovered — an interval rule has been firing on
    weekends too, so it comes back as '*' rather than silently narrowing to the
    old mon-fri default and skipping runs.
    """

    database = tmp_path / "interval-downgrade.db"
    _upgrade(database)
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
            "(id, store_id, name, workflow, mode, first_run_at, interval_minutes, "
            "timezone, marketplace_codes, workflow_config, "
            "workflow_config_version, enabled, misfire_grace_seconds, "
            "created_at, updated_at) "
            "VALUES (1, 1, 'interval', 'amazon_disbursement', 'dry_run', "
            "'2026-01-01 06:30:00+00:00', 1500, 'Asia/Singapore', '[]', '{}', "
            "1, 1, 1800, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    _downgrade(database, "0006")

    with sa.create_engine(f"sqlite:///{database.as_posix()}").connect() as connection:
        local_time, days = connection.exec_driver_sql(
            "SELECT local_time, days_of_week FROM schedules WHERE id=1"
        ).one()
        assert local_time == "14:30"  # 06:30 UTC in Asia/Singapore
        assert days == "*"
        columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(schedules)")
        }
        assert "first_run_at" not in columns
        assert "interval_minutes" not in columns


def _database_before_de(database: Path) -> sa.Engine:
    """建出 0010 之前的真实形状：store_marketplaces 的 CHECK 只认 CA/UK/AU。

    ⚠️ 不能用「新建库升到 0009」来代替：0001 是按 **live metadata** 建表的，而
    models.py 现在已经带着放宽后的 CHECK，那样造出来的库一开始就是新形状，
    这个测试会变成永远绿的摆设。
    """

    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE stores (
                id INTEGER PRIMARY KEY NOT NULL,
                name VARCHAR(120) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE store_marketplaces (
                id INTEGER PRIMARY KEY NOT NULL,
                store_id INTEGER NOT NULL,
                code VARCHAR(2) NOT NULL,
                domain VARCHAR(255) NOT NULL,
                currency VARCHAR(3) NOT NULL,
                enabled BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
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
            "CREATE TABLE alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES ('0009')"
        )
        for store_id, name in ((1, "店铺甲"), (2, "店铺乙"), (3, "店铺丙")):
            connection.execute(
                sa.text(
                    "INSERT INTO stores (id, name, created_at, updated_at) "
                    "VALUES (:id, :name, :ts, :ts)"
                ),
                {"id": store_id, "name": name, "ts": "2026-09-14 00:00:00"},
            )
        # 甲有两站，乙只有一站，丙一站都没有——三种形状都要能补上 DE。
        for store_id, code, domain, currency in (
            (1, "CA", "sellercentral.amazon.ca", "CAD"),
            (1, "UK", "sellercentral.amazon.co.uk", "GBP"),
            (2, "AU", "sellercentral.amazon.com.au", "AUD"),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO store_marketplaces "
                    "(store_id, code, domain, currency, enabled, created_at, updated_at) "
                    "VALUES (:sid, :code, :domain, :cur, 1, :ts, :ts)"
                ),
                {
                    "sid": store_id,
                    "code": code,
                    "domain": domain,
                    "cur": currency,
                    "ts": "2026-09-14 00:00:00",
                },
            )
    return engine


def test_0010_widens_the_check_and_gives_every_store_a_de_row(tmp_path: Path) -> None:
    """运营要求德国站适用于**所有**现有账号，所以补行是迁移的职责，不是运维的记性。

    漏补的后果不是「少一个站点」，而是排期创建时被挡下并报
    「排期包含尚未在该店铺建档的站点」——那条消息根本不会提 DE。
    """

    database = tmp_path / "pre_0010.db"
    engine = _database_before_de(database)

    # Partial historical fixture: exercise its migration chain, not the newer
    # full-store history schema (covered by test_store_history_migration.py).
    _upgrade(database, "0010")

    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT store_id, domain, currency, enabled FROM store_marketplaces "
                "WHERE code = 'DE' ORDER BY store_id"
            )
        ).all()
        # 一站都没有的店铺（丙）也要补上，否则它永远排不了 GPSR。
        assert rows == [
            (1, "sellercentral.amazon.de", "EUR", 1),
            (2, "sellercentral.amazon.de", "EUR", 1),
            (3, "sellercentral.amazon.de", "EUR", 1),
        ]
        # 原有的行一条都不能动。
        assert connection.execute(
            sa.text(
                "SELECT store_id, code FROM store_marketplaces "
                "WHERE code <> 'DE' ORDER BY store_id, code"
            )
        ).all() == [(1, "CA"), (1, "UK"), (2, "AU")]

        schema = connection.scalar(
            sa.text("SELECT sql FROM sqlite_master WHERE name = 'store_marketplaces'")
        )
        # 重建必须保留其余结构，这正是 0005 当年踩过的地方。
        assert "uq_store_marketplace_code" in schema
        assert "ck_marketplace_currency" in schema
        assert schema.count("FOREIGN KEY") == 1
        assert connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
                "AND name = 'ix_store_marketplaces_store_id'"
            )
        ) == 1
        assert connection.execute(sa.text("PRAGMA foreign_key_check")).all() == []
        assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"


def test_0010_accepts_de_but_still_refuses_an_unlisted_code(tmp_path: Path) -> None:
    """放宽不等于拆掉。

    这条断言的用途是：谁要是嫌 CHECK 麻烦直接删了它，这里会红。
    """

    database = tmp_path / "pre_0010.db"
    engine = _database_before_de(database)
    # Partial historical fixture: exercise its migration chain, not the newer
    # full-store history schema (covered by test_store_history_migration.py).
    _upgrade(database, "0010")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO stores (id, name, created_at, updated_at) "
                "VALUES (9, :name, :ts, :ts)"
            ),
            {"name": "新店", "ts": "2026-09-14 00:00:00"},
        )
        # DE 本身可用。
        connection.execute(
            sa.text(
                "INSERT INTO store_marketplaces "
                "(store_id, code, domain, currency, enabled, created_at, updated_at) "
                "VALUES (9, 'DE', 'sellercentral.amazon.de', 'EUR', 1, :ts, :ts)"
            ),
            {"ts": "2026-09-14 00:00:00"},
        )

    for rejected in ("US", "FR", "JP"):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO store_marketplaces "
                        "(store_id, code, domain, currency, enabled, created_at, updated_at) "
                        "VALUES (3, :code, 'example.test', 'EUR', 1, :ts, :ts)"
                    ),
                    {"code": rejected, "ts": "2026-09-14 00:00:00"},
                )


def test_0010_backfill_leaves_an_existing_de_row_alone(tmp_path: Path) -> None:
    """已经有 DE 的店铺不能被补成第二行，也不能被改写。

    唯一约束会让重复插入直接炸掉整条迁移，所以 WHERE NOT EXISTS 是承重的。
    """

    database = tmp_path / "pre_0010.db"
    engine = _database_before_de(database)
    # 先把约束放宽再塞一行 DE，模拟「运营已经自己加过了」。
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA writable_schema=ON")
        connection.execute(
            sa.text(
                "UPDATE sqlite_master SET sql = REPLACE(sql, "
                ":old_check, :new_check) WHERE name = 'store_marketplaces'"
            ),
            {
                "old_check": "CHECK (code IN ('CA', 'UK', 'AU'))",
                "new_check": "CHECK (code IN ('CA', 'UK', 'AU', 'DE'))",
            },
        )
        connection.exec_driver_sql("PRAGMA writable_schema=OFF")
    engine.dispose()
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO store_marketplaces "
                "(store_id, code, domain, currency, enabled, created_at, updated_at) "
                "VALUES (1, 'DE', 'sellercentral.amazon.de', 'EUR', 0, :ts, :ts)"
            ),
            {"ts": "2026-09-14 00:00:00"},
        )

    # Partial historical fixture: exercise its migration chain, not the newer
    # full-store history schema (covered by test_store_history_migration.py).
    _upgrade(database, "0010")

    with engine.connect() as connection:
        # 已存在的那行保持 enabled=0 不被改写；其余两家补 enabled=1。
        assert connection.execute(
            sa.text(
                "SELECT store_id, enabled FROM store_marketplaces "
                "WHERE code = 'DE' ORDER BY store_id"
            )
        ).all() == [(1, 0), (2, 1), (3, 1)]


def test_0010_round_trip_narrows_the_check_and_drops_only_de(tmp_path: Path) -> None:
    """降级要能真的回到旧形状，否则这条迁移没法回滚。"""

    database = tmp_path / "pre_0010.db"
    engine = _database_before_de(database)
    # Partial historical fixture: exercise its migration chain, not the newer
    # full-store history schema (covered by test_store_history_migration.py).
    _upgrade(database, "0010")
    _downgrade(database, "0009")

    with engine.connect() as connection:
        schema = connection.scalar(
            sa.text("SELECT sql FROM sqlite_master WHERE name = 'store_marketplaces'")
        )
        assert "'DE'" not in schema
        assert connection.scalar(
            sa.text("SELECT COUNT(*) FROM store_marketplaces WHERE code = 'DE'")
        ) == 0
        assert connection.execute(
            sa.text(
                "SELECT store_id, code FROM store_marketplaces ORDER BY store_id, code"
            )
        ).all() == [(1, "CA"), (1, "UK"), (2, "AU")]
        assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"

    _upgrade(database, "0010")
    with engine.connect() as connection:
        assert connection.scalar(
            sa.text("SELECT COUNT(*) FROM store_marketplaces WHERE code = 'DE'")
        ) == 3


def test_a_fresh_install_carries_the_widened_check(tmp_path: Path) -> None:
    """全新库由 0001 按 live metadata 建表，所以它天生就该认 DE。

    升级库和新装库必须长成同一个样子——两者分叉正是这批迁移测试存在的理由。
    """

    database = tmp_path / "fresh.db"
    _upgrade(database)
    engine = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with engine.connect() as connection:
        schema = connection.scalar(
            sa.text("SELECT sql FROM sqlite_master WHERE name = 'store_marketplaces'")
        )
        assert "'DE'" in schema
        assert connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        ) == _head_revision()


def test_disbursement_expansion_keeps_other_eu_sites_inventory_only() -> None:
    """US/DE support payout; IT/FR/ES/PL retain their existing workflow scope."""

    from ziniao_automation.repositories import V1_MARKETPLACES
    from ziniao_automation.workflows.amazon_disbursement.config import (
        ALLOWED_MARKETPLACES,
    )

    assert V1_MARKETPLACES["DE"] == ("sellercentral.amazon.de", "EUR")
    assert ALLOWED_MARKETPLACES["DE"] == V1_MARKETPLACES["DE"]
    assert ALLOWED_MARKETPLACES["US"] == V1_MARKETPLACES["US"]
    assert not {"IT", "FR", "ES", "PL"} & set(ALLOWED_MARKETPLACES)
    # 探针白名单必须是建档白名单的真子集，不能各自漂移。
    assert set(ALLOWED_MARKETPLACES) < set(V1_MARKETPLACES)
