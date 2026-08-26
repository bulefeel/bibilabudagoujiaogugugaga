"""Add multi-store schedule batches and immutable workflow inputs.

Revision ID: 0006
Revises: 0005

``0001`` creates tables from the live ORM metadata on a fresh install, while
an installed database stamped at ``0005`` has the older shape.  Every change
is therefore introspection guarded.  The three existing tables are rebuilt on
SQLite when their shape changes: merely appending columns cannot add the CHECK
constraints declared by the ORM, and native ``DROP COLUMN`` cannot remove a
column that is referenced by one of those constraints during downgrade.

The rebuild is performed with foreign-key enforcement temporarily disabled in
one explicit SQLite transaction.  This is required because ``schedules`` and
``runs`` have audit children; SQLite otherwise applies their ON DELETE actions
when Alembic swaps a table, losing links even though all rows are copied back.
Foreign-key integrity is checked before the transaction is committed and the
connection's original PRAGMA setting is restored on both success and failure.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Iterator

import sqlalchemy as sa
from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _indexes(table_name: str) -> dict[str, list[str]]:
    if table_name not in _tables():
        return {}
    return {
        str(item["name"]): [str(value) for value in item.get("column_names") or ()]
        for item in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _checks(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if item.get("name")
    }


def _uniques(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
        if item.get("name")
    }


def _foreign_keys(table_name: str) -> list[dict[str, Any]]:
    if table_name not in _tables():
        return []
    return list(sa.inspect(op.get_bind()).get_foreign_keys(table_name))


def _has_batch_foreign_key() -> bool:
    return any(
        item.get("referred_table") == "schedule_batches"
        and list(item.get("constrained_columns") or ()) == ["batch_id"]
        for item in _foreign_keys("schedules")
    )


@contextmanager
def _sqlite_rebuild_transaction() -> Iterator[None]:
    """Make all rebuild DDL atomic without firing audit-link delete actions."""

    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return

    # Alembic enters the revision inside a logical transaction.  PRAGMA
    # foreign_keys cannot change while SQLite has a transaction open, so end
    # that empty wrapper before changing the connection setting.  The actual
    # migration below gets its own explicit, atomic transaction.
    if bind.in_transaction():
        bind.commit()
    original_foreign_keys = int(
        bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    )
    bind.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    disabled = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    bind.commit()
    if disabled != 0:
        raise RuntimeError("SQLite foreign-key enforcement could not be suspended")

    try:
        bind.exec_driver_sql("BEGIN IMMEDIATE")
        yield
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            sample = ", ".join(str(tuple(row)) for row in violations[:3])
            raise RuntimeError(
                "Migration would leave invalid foreign-key references: " + sample
            )
        bind.commit()
    except BaseException:
        if bind.in_transaction():
            bind.rollback()
        raise
    finally:
        if bind.in_transaction():
            bind.rollback()
        restore = "ON" if original_foreign_keys else "OFF"
        bind.exec_driver_sql(f"PRAGMA foreign_keys={restore}")
        restored = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
        bind.commit()
        if restored != original_foreign_keys:
            raise RuntimeError("SQLite foreign-key enforcement was not restored")


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _json_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).upper() for item in value if str(item).strip()))


def _create_batch_table() -> None:
    if "schedule_batches" in _tables():
        return
    op.create_table(
        "schedule_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("schedule_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schedule_count >= 0", name="ck_schedule_batch_count"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_schedule_batches_request_id"),
    )


def _upgrade_stores() -> None:
    """Allow enabled stores whose identity is irrelevant to their workflow.

    Identity remains a workflow-level precondition for payouts.  The old
    store-wide CHECK made it impossible for a future read-only workflow to use
    an enabled store before seller identity had been profiled.
    """

    if (
        "stores" not in _tables()
        or "ck_store_enabled_requires_identity" not in _checks("stores")
    ):
        return
    with op.batch_alter_table("stores", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_store_enabled_requires_identity", type_="check"
        )


def _upgrade_schedules() -> None:
    if "schedules" not in _tables():
        return

    columns = _columns("schedules")
    checks = _checks("schedules")
    uniques = _uniques("schedules")
    required_checks = {
        "ck_schedule_workflow_config_version",
        "ck_schedule_batch_order",
    }
    needs_rebuild = (
        not {
            "batch_id",
            "batch_order",
            "workflow_config",
            "workflow_config_version",
        }.issubset(columns)
        or not required_checks.issubset(checks)
        or "uq_schedule_batch_order" not in uniques
        or not _has_batch_foreign_key()
    )
    if needs_rebuild:
        # The first draft of 0006 used a unique index with this name.  If a
        # locally interrupted/pre-release database has that shape, replace it
        # with the ORM's actual table-level unique constraint.
        indexes = _indexes("schedules")
        if (
            "uq_schedule_batch_order" in indexes
            and "uq_schedule_batch_order" not in uniques
        ):
            op.drop_index("uq_schedule_batch_order", table_name="schedules")

        with op.batch_alter_table("schedules", recreate="always") as batch_op:
            if "batch_id" not in columns:
                batch_op.add_column(sa.Column("batch_id", sa.Integer(), nullable=True))
            if "batch_order" not in columns:
                batch_op.add_column(sa.Column("batch_order", sa.Integer(), nullable=True))
            if "workflow_config" not in columns:
                batch_op.add_column(
                    sa.Column(
                        "workflow_config",
                        sa.JSON(),
                        nullable=False,
                        server_default=sa.text("'{}'"),
                    )
                )
            if "workflow_config_version" not in columns:
                batch_op.add_column(
                    sa.Column(
                        "workflow_config_version",
                        sa.Integer(),
                        nullable=False,
                        server_default=sa.text("'1'"),
                    )
                )
            if "ck_schedule_workflow_config_version" not in checks:
                batch_op.create_check_constraint(
                    "ck_schedule_workflow_config_version",
                    "workflow_config_version >= 1",
                )
            if "ck_schedule_batch_order" not in checks:
                batch_op.create_check_constraint(
                    "ck_schedule_batch_order",
                    "batch_order IS NULL OR batch_order >= 1",
                )
            if "uq_schedule_batch_order" not in uniques:
                batch_op.create_unique_constraint(
                    "uq_schedule_batch_order", ["batch_id", "batch_order"]
                )
            if not _has_batch_foreign_key():
                batch_op.create_foreign_key(
                    "fk_schedules_batch_id_schedule_batches",
                    "schedule_batches",
                    ["batch_id"],
                    ["id"],
                    ondelete="SET NULL",
                )

    if "ix_schedules_batch_id" not in _indexes("schedules"):
        op.create_index("ix_schedules_batch_id", "schedules", ["batch_id"])


def _upgrade_runs() -> None:
    if "runs" not in _tables():
        return
    columns = _columns("runs")
    checks = _checks("runs")
    if {
        "workflow_config",
        "workflow_config_version",
    }.issubset(columns) and "ck_run_workflow_config_version" in checks:
        return

    with op.batch_alter_table("runs", recreate="always") as batch_op:
        if "workflow_config" not in columns:
            batch_op.add_column(
                sa.Column(
                    "workflow_config",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )
        if "workflow_config_version" not in columns:
            batch_op.add_column(
                sa.Column(
                    "workflow_config_version",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("'1'"),
                )
            )
        if "ck_run_workflow_config_version" not in checks:
            batch_op.create_check_constraint(
                "ck_run_workflow_config_version", "workflow_config_version >= 1"
            )


def _upgrade_queue() -> None:
    if "run_queue_entries" not in _tables():
        return
    columns = _columns("run_queue_entries")
    checks = _checks("run_queue_entries")
    expected_index = [
        "state",
        "priority",
        "scheduled_for_at",
        "business_priority",
        "enqueued_at",
        "batch_scope_id",
        "target_order",
        "id",
    ]
    indexes = _indexes("run_queue_entries")
    if (
        "ix_run_queue_ready_order" in indexes
        and indexes["ix_run_queue_ready_order"] != expected_index
    ):
        op.drop_index("ix_run_queue_ready_order", table_name="run_queue_entries")

    required_checks = {
        "ck_run_queue_business_priority",
        "ck_run_queue_target_order",
        "ck_run_queue_batch_scope_id",
    }
    if (
        not {"business_priority", "target_order", "batch_scope_id"}.issubset(columns)
        or not required_checks.issubset(checks)
    ):
        with op.batch_alter_table(
            "run_queue_entries", recreate="always"
        ) as batch_op:
            if "business_priority" not in columns:
                batch_op.add_column(
                    sa.Column(
                        "business_priority",
                        sa.Integer(),
                        nullable=False,
                        server_default=sa.text("'1'"),
                    )
                )
            if "target_order" not in columns:
                batch_op.add_column(
                    sa.Column(
                        "target_order",
                        sa.Integer(),
                        nullable=False,
                        server_default=sa.text("'2147483647'"),
                    )
                )
            if "batch_scope_id" not in columns:
                batch_op.add_column(
                    sa.Column("batch_scope_id", sa.Integer(), nullable=True)
                )
            if "ck_run_queue_business_priority" not in checks:
                batch_op.create_check_constraint(
                    "ck_run_queue_business_priority", "business_priority >= 0"
                )
            if "ck_run_queue_target_order" not in checks:
                batch_op.create_check_constraint(
                    "ck_run_queue_target_order", "target_order >= 1"
                )
            if "ck_run_queue_batch_scope_id" not in checks:
                batch_op.create_check_constraint(
                    "ck_run_queue_batch_scope_id",
                    "batch_scope_id IS NULL OR batch_scope_id >= 1",
                )

    indexes = _indexes("run_queue_entries")
    if indexes.get("ix_run_queue_ready_order") != expected_index:
        if "ix_run_queue_ready_order" in indexes:
            op.drop_index("ix_run_queue_ready_order", table_name="run_queue_entries")
        op.create_index(
            "ix_run_queue_ready_order", "run_queue_entries", expected_index
        )


def _backfill() -> None:
    bind = op.get_bind()

    if {"id", "marketplace_codes", "workflow_config"}.issubset(
        _columns("schedules")
    ):
        for schedule_id, raw_codes in bind.execute(
            sa.text("SELECT id, marketplace_codes FROM schedules")
        ):
            config = {"marketplace_codes": _json_list(raw_codes)}
            bind.execute(
                sa.text(
                    "UPDATE schedules SET workflow_config = :config, "
                    "workflow_config_version = 1 WHERE id = :id"
                ),
                {"config": json.dumps(config, ensure_ascii=False), "id": schedule_id},
            )

    run_columns = _columns("runs")
    schedule_columns = _columns("schedules")
    if {"id", "schedule_id", "result_summary", "workflow_config"}.issubset(
        run_columns
    ):
        schedule_configs: dict[int, tuple[dict[str, Any], int]] = {}
        if {"id", "workflow_config", "workflow_config_version"}.issubset(
            schedule_columns
        ):
            schedule_configs = {
                int(schedule_id): (_json_object(raw_config), int(version or 1))
                for schedule_id, raw_config, version in bind.execute(
                    sa.text(
                        "SELECT id, workflow_config, workflow_config_version "
                        "FROM schedules"
                    )
                )
            }
        for run_id, schedule_id, raw_summary in bind.execute(
            sa.text("SELECT id, schedule_id, result_summary FROM runs")
        ):
            # Prefer the sites captured when that run was created; the linked
            # schedule may have been edited many times afterwards.
            summary = _json_object(raw_summary)
            stored = (
                schedule_configs.get(int(schedule_id))
                if schedule_id is not None
                else None
            )
            if "requested_marketplaces" in summary:
                config = {
                    "marketplace_codes": _json_list(
                        summary.get("requested_marketplaces", [])
                    )
                }
                version = 1
            elif stored is not None:
                config, version = stored
            else:
                config = {
                    "marketplace_codes": _json_list(
                        summary.get("requested_marketplaces", [])
                    )
                }
                version = 1
            bind.execute(
                sa.text(
                    "UPDATE runs SET workflow_config = :config, "
                    "workflow_config_version = :version WHERE id = :id"
                ),
                {
                    "config": json.dumps(config, ensure_ascii=False),
                    "version": version,
                    "id": run_id,
                },
            )

    if "business_priority" in _columns("run_queue_entries"):
        if "workflow" in _columns("runs"):
            bind.execute(
                sa.text(
                    "UPDATE run_queue_entries SET business_priority = "
                    "CASE WHEN run_id IN "
                    "(SELECT id FROM runs WHERE workflow = 'amazon_disbursement') "
                    "THEN 1 ELSE 100 END"
                )
            )
        else:
            # Minimal legacy fixtures and the earliest installer schema did
            # not persist workflow; every such run was the sole V1 payout.
            bind.execute(
                sa.text("UPDATE run_queue_entries SET business_priority = 1")
            )
    if (
        "target_order" in _columns("run_queue_entries")
        and "schedule_id" in _columns("runs")
        and {"id", "batch_order"}.issubset(_columns("schedules"))
    ):
        bind.execute(
            sa.text(
                "UPDATE run_queue_entries SET target_order = COALESCE(("
                "SELECT s.batch_order FROM runs AS r "
                "JOIN schedules AS s ON s.id = r.schedule_id "
                "WHERE r.id = run_queue_entries.run_id"
                "), 2147483647)"
            )
        )
    if (
        "batch_scope_id" in _columns("run_queue_entries")
        and "schedule_id" in _columns("runs")
        and {"id", "batch_id"}.issubset(_columns("schedules"))
    ):
        bind.execute(
            sa.text(
                "UPDATE run_queue_entries SET batch_scope_id = ("
                "SELECT s.batch_id FROM runs AS r "
                "JOIN schedules AS s ON s.id = r.schedule_id "
                "WHERE r.id = run_queue_entries.run_id"
                ")"
            )
        )


def _create_upgrade_indexes() -> None:
    if (
        "schedule_batches" in _tables()
        and "ix_schedule_batches_created_at" not in _indexes("schedule_batches")
    ):
        op.create_index(
            "ix_schedule_batches_created_at", "schedule_batches", ["created_at"]
        )


def upgrade() -> None:
    with _sqlite_rebuild_transaction():
        _create_batch_table()
        _upgrade_stores()
        _upgrade_schedules()
        _upgrade_runs()
        _upgrade_queue()
        _backfill()
        _create_upgrade_indexes()


def _downgrade_queue() -> None:
    if "run_queue_entries" not in _tables():
        return
    indexes = _indexes("run_queue_entries")
    if "ix_run_queue_ready_order" in indexes:
        op.drop_index("ix_run_queue_ready_order", table_name="run_queue_entries")

    columns = _columns("run_queue_entries")
    checks = _checks("run_queue_entries")
    if {"business_priority", "target_order", "batch_scope_id"} & columns:
        with op.batch_alter_table(
            "run_queue_entries", recreate="always"
        ) as batch_op:
            for name in (
                "ck_run_queue_business_priority",
                "ck_run_queue_target_order",
                "ck_run_queue_batch_scope_id",
            ):
                if name in checks:
                    batch_op.drop_constraint(name, type_="check")
            for column in ("batch_scope_id", "target_order", "business_priority"):
                if column in columns:
                    batch_op.drop_column(column)

    old_index_columns = [
        "state",
        "priority",
        "scheduled_for_at",
        "enqueued_at",
        "id",
    ]
    if set(old_index_columns).issubset(_columns("run_queue_entries")):
        op.create_index(
            "ix_run_queue_ready_order",
            "run_queue_entries",
            old_index_columns,
        )


def _downgrade_runs() -> None:
    if "runs" not in _tables():
        return
    columns = _columns("runs")
    checks = _checks("runs")
    if not {"workflow_config_version", "workflow_config"} & columns:
        return
    with op.batch_alter_table("runs", recreate="always") as batch_op:
        if "ck_run_workflow_config_version" in checks:
            batch_op.drop_constraint(
                "ck_run_workflow_config_version", type_="check"
            )
        for column in ("workflow_config_version", "workflow_config"):
            if column in columns:
                batch_op.drop_column(column)


def _downgrade_schedules() -> None:
    if "schedules" not in _tables():
        return
    indexes = _indexes("schedules")
    for name in ("uq_schedule_batch_order", "ix_schedules_batch_id"):
        if name in indexes:
            op.drop_index(name, table_name="schedules")

    columns = _columns("schedules")
    checks = _checks("schedules")
    uniques = _uniques("schedules")
    removable = {
        "workflow_config_version",
        "workflow_config",
        "batch_order",
        "batch_id",
    }
    if not removable & columns:
        return
    with op.batch_alter_table("schedules", recreate="always") as batch_op:
        if "uq_schedule_batch_order" in uniques:
            batch_op.drop_constraint("uq_schedule_batch_order", type_="unique")
        for name in (
            "ck_schedule_workflow_config_version",
            "ck_schedule_batch_order",
        ):
            if name in checks:
                batch_op.drop_constraint(name, type_="check")
        for item in _foreign_keys("schedules"):
            if (
                item.get("referred_table") == "schedule_batches"
                and list(item.get("constrained_columns") or ()) == ["batch_id"]
                and item.get("name")
            ):
                batch_op.drop_constraint(str(item["name"]), type_="foreignkey")
        for column in (
            "workflow_config_version",
            "workflow_config",
            "batch_order",
            "batch_id",
        ):
            if column in columns:
                batch_op.drop_column(column)


def _downgrade_stores() -> None:
    if "stores" not in _tables():
        return
    columns = _columns("stores")
    if not {"enabled", "identity_confirmed"}.issubset(columns):
        return
    if "ck_store_enabled_requires_identity" in _checks("stores"):
        return

    # Old 0005 binaries assume this database invariant.  Rows that are valid
    # for a no-identity workflow in 0006 must therefore be disabled before the
    # old constraint is restored; no identity assertion is invented.
    op.get_bind().execute(
        sa.text(
            "UPDATE stores SET enabled = 0 "
            "WHERE enabled = 1 AND identity_confirmed = 0"
        )
    )
    with op.batch_alter_table("stores", recreate="always") as batch_op:
        batch_op.create_check_constraint(
            "ck_store_enabled_requires_identity",
            "enabled = 0 OR identity_confirmed = 1",
        )


def downgrade() -> None:
    with _sqlite_rebuild_transaction():
        _downgrade_queue()
        _downgrade_runs()
        _downgrade_schedules()
        if "schedule_batches" in _tables():
            op.drop_table("schedule_batches")
        _downgrade_stores()
