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

    _upgrade(database)

    upgraded = sa.create_engine(f"sqlite:///{database.as_posix()}")
    with upgraded.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar() == "0006"
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
            " local_time, days_of_week, timezone, marketplace_codes, "
            " workflow_config, workflow_config_version, enabled, "
            " misfire_grace_seconds, created_at, updated_at) "
            "VALUES (11, 2, 7, 1, 'schedule', 'amazon_disbursement', "
            "'dry_run', '09:00', 'mon', 'Asia/Singapore', '[\"CA\",\"UK\"]', "
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
            "local_time, days_of_week, timezone, marketplace_codes, "
            "workflow_config, workflow_config_version, enabled, "
            "misfire_grace_seconds, created_at, updated_at) VALUES "
            "(11, 1, 1, 1, 'schedule', 'amazon_disbursement', 'dry_run', "
            "'09:00', 'mon', 'Asia/Singapore', '[\"CA\"]', "
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
