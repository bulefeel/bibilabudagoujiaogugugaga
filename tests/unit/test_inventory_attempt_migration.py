"""0017 index-only migration preserves history and rejects lossy downgrades."""
import importlib.util
from pathlib import Path
import pytest
from sqlalchemy.exc import IntegrityError
from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.unit.test_inventory_compliance_migration import seed_0013, migrate
from tests.unit.test_inventory_manufacturer_migration import snapshot


def test_migration_roundtrip_preserves_all_rows_and_triggers(tmp_path):
    engine, _ = seed_0013(tmp_path); migrate(engine, "0016")
    with engine.connect() as db:
        before = snapshot(db)
        schema = db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger') ORDER BY name").all()
    migrate(engine, "0017")
    with engine.connect() as db:
        assert snapshot(db) == before
        for name in ("uq_inventory_gpsr_submission", "uq_inventory_requirement_submission"):
            sql = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name=?", (name,)).scalar()
            assert "run_id" in sql
        assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
    migrate(engine, "0016", downgrade=True)
    with engine.connect() as db:
        assert snapshot(db) == before
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger') ORDER BY name").all() == schema
    engine.dispose()


def test_repeated_attempts_preserve_prior_fact_and_prevent_lossy_downgrade(tmp_path):
    engine, _ = seed_0013(tmp_path); migrate(engine, "0017")
    with engine.begin() as db:
        before = db.exec_driver_sql("SELECT * FROM inventory_gpsr_requirements").all()
        # Copy a complete run so all versioned required columns remain intact.
        columns = [r[1] for r in db.exec_driver_sql("PRAGMA table_info(runs)")]
        values = ["'retry-run'" if c == "id" else c for c in columns]
        db.exec_driver_sql(f"INSERT INTO runs ({','.join(columns)}) SELECT {','.join(values)} FROM runs WHERE id=(SELECT run_id FROM inventory_gpsr_requirements LIMIT 1)")
        columns = [r[1] for r in db.exec_driver_sql("PRAGMA table_info(inventory_gpsr_items)")]
        values = ["'retry-item'" if c == "id" else "'retry-run'" if c == "run_id" else c for c in columns]
        db.exec_driver_sql(f"INSERT INTO inventory_gpsr_items ({','.join(columns)}) SELECT {','.join(values)} FROM inventory_gpsr_items LIMIT 1")
        columns = [r[1] for r in db.exec_driver_sql("PRAGMA table_info(inventory_gpsr_requirements)")]
        values = ["'retry-requirement'" if c == "id" else "'retry-run'" if c == "run_id" else "'retry-item'" if c == "item_id" else c for c in columns]
        db.exec_driver_sql(f"INSERT INTO inventory_gpsr_requirements ({','.join(columns)}) SELECT {','.join(values)} FROM inventory_gpsr_requirements LIMIT 1")
        assert db.exec_driver_sql("SELECT * FROM inventory_gpsr_requirements WHERE id!='retry-requirement'").all() == before
    with pytest.raises(RuntimeError, match="跨任务提交尝试"):
        migrate(engine, "0016", downgrade=True)
    with engine.connect() as db:
        assert db.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0017"
        assert db.exec_driver_sql("SELECT count(*) FROM inventory_gpsr_requirements").scalar() == 2
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.exec_driver_sql("UPDATE inventory_gpsr_requirements SET run_id=(SELECT run_id FROM inventory_gpsr_requirements WHERE id!='retry-requirement') WHERE id='retry-requirement'")
    engine.dispose()


def test_interrupted_index_upgrade_rolls_back(tmp_path, monkeypatch):
    engine, _ = seed_0013(tmp_path); migrate(engine, "0016")
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0017_inventory_attempt_scope.py"
    spec = importlib.util.spec_from_file_location("migration0017", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    original = module.op.create_index
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("fixture interruption")
    monkeypatch.setattr(module.op, "create_index", fail)
    with engine.connect() as db:
        before = db.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all()
        with pytest.raises(RuntimeError, match="fixture interruption"):
            with db.begin_nested(), Operations.context(MigrationContext.configure(db)):
                module.upgrade()
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all() == before
    engine.dispose()
