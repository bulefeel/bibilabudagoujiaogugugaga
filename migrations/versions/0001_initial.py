"""Initial V1 database schema.

Revision ID: 0001
Revises:
"""
from __future__ import annotations

from alembic import op

from ziniao_automation.db import Base
from ziniao_automation import models  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tables include database-level checks and uniqueness barriers for money
    # operations.  This first migration intentionally shares model metadata so
    # a clean install and test schema cannot drift apart.
    bind = op.get_bind()
    # Alembic owns its version table.  Excluding it prevents metadata-based
    # create_all from interfering with Alembic's revision stamp transaction.
    tables = [table for table in Base.metadata.sorted_tables if table.name != "alembic_version"]
    Base.metadata.create_all(bind=bind, tables=tables)


def downgrade() -> None:
    bind = op.get_bind()
    tables = [table for table in reversed(Base.metadata.sorted_tables) if table.name != "alembic_version"]
    Base.metadata.drop_all(bind=bind, tables=tables)
