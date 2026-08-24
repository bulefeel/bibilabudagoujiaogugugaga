from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
import pytest
from sqlalchemy import create_engine, text

from ziniao_automation.db import (
    backup_sqlite_database,
    database_revision,
    init_database,
)


def test_fresh_database_does_not_create_a_meaningless_backup(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    backups = tmp_path / "backups"
    engine = create_engine(f"sqlite:///{database.as_posix()}")

    created = init_database(engine, backup_dir=backups)

    assert created is None
    assert not backups.exists()
    assert database_revision(engine) != "unversioned"


def test_pending_migration_gets_a_consistent_backup_that_survives_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "installed.db"
    backups = tmp_path / "backups"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE important_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO important_rows VALUES ('keep-me')")
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.execute("INSERT INTO alembic_version VALUES ('legacy-test')")

    engine = create_engine(f"sqlite:///{database.as_posix()}")

    def fail_upgrade(*args, **kwargs):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(command, "upgrade", fail_upgrade)
    with pytest.raises(RuntimeError, match="simulated migration failure"):
        init_database(engine, backup_dir=backups)

    snapshots = list(backups.glob("pre-migration-*.db"))
    assert len(snapshots) == 1
    with sqlite3.connect(snapshots[0]) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT value FROM important_rows").fetchone() == (
            "keep-me",
        )
    assert not list(backups.glob("*.partial"))


def test_database_at_head_is_not_backed_up_again(tmp_path: Path) -> None:
    database = tmp_path / "current.db"
    backups = tmp_path / "backups"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    init_database(engine, backup_dir=backups)

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS marker (value TEXT)"))
    created = init_database(engine, backup_dir=backups)

    assert created is None
    assert not backups.exists()


def test_backup_api_includes_rows_still_resident_in_wal(tmp_path: Path) -> None:
    database = tmp_path / "wal-source.db"
    backups = tmp_path / "backups"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE financial_guard (value TEXT NOT NULL)")
        writer.execute("INSERT INTO financial_guard VALUES ('committed-in-wal')")
        writer.commit()

        snapshot = backup_sqlite_database(
            database,
            backups,
            from_revision="0004",
            to_revision="0005",
        )
    finally:
        writer.close()

    with sqlite3.connect(snapshot) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT value FROM financial_guard").fetchone() == (
            "committed-in-wal",
        )
