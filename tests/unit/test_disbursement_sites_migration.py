"""0018 expands constraints without changing existing task or business rows."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.unit.test_inventory_compliance_migration import migrate
from tests.unit.test_setup_persistence import records, begin, passed


def snapshot(engine):
    with engine.connect() as db:
        return {name: db.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY 1').all()
                for (name,) in db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' "
                                                "AND name NOT IN ('alembic_version','sqlite_sequence')")}


def seed(records):
    repo, sessions, sid = records
    row = begin(repo, sid, ["DE"])
    passed(repo, row["probe_id"], "DE")
    repo.finish(row["probe_id"], outcome="SUCCEEDED", cleanup="CONFIRMED")
    engine = sessions.kw["bind"]
    migrate(engine, "0017", downgrade=True)
    with engine.begin() as db:
        db.exec_driver_sql("UPDATE sqlite_sequence SET seq=1000 WHERE name='store_marketplaces'")
    return engine


def test_upgrade_downgrade_preserve_rows_triggers_and_history_sequence(records):
    engine = seed(records)
    before = snapshot(engine)
    with engine.connect() as db:
        triggers = db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all()
    for version, down in (("0018", False), ("0017", True), ("0018", False)):
        migrate(engine, version, downgrade=down)
        assert snapshot(engine) == before
        with engine.connect() as db:
            assert db.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
            assert db.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name='store_marketplaces'").scalar() == 1000
            assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all() == triggers


def test_us_history_blocks_lossy_downgrade(records):
    repo, sessions, sid = records
    row = begin(repo, sid, ["US"])
    repo.finish(row["probe_id"], outcome="FAILED", cleanup="CONFIRMED")
    # This test covers 0018's lossy-downgrade guard, not newer schema columns.
    migrate(sessions.kw["bind"], "0018", downgrade=True)
    before = snapshot(sessions.kw["bind"])
    with pytest.raises(RuntimeError, match="美国站"):
        migrate(sessions.kw["bind"], "0017", downgrade=True)
    assert snapshot(sessions.kw["bind"]) == before


def test_failed_second_table_rebuild_rolls_back_both_tables(records, monkeypatch):
    engine = seed(records)
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0018_disbursement_us_de.py"
    spec = importlib.util.spec_from_file_location("migration0018", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.rewrite
    def fail(table, column, codes):
        original(table, column, codes)
        if table == "store_setup_site_results": raise RuntimeError("fixture interrupted")
    monkeypatch.setattr(module, "rewrite", fail)
    before = snapshot(engine)
    with engine.connect() as db:
        ddl = db.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all()
        with Operations.context(MigrationContext.configure(db)):
            with pytest.raises(RuntimeError, match="fixture interrupted"):
                module.upgrade()
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all() == ddl
        assert db.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    assert snapshot(engine) == before
