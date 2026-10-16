"""0016 keeps all existing rows, indexes, triggers and immutable run inputs."""
import importlib.util
from pathlib import Path
import pytest
from sqlalchemy.exc import IntegrityError
from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.unit.test_inventory_compliance_migration import seed_0013, migrate

def snapshot(db):
    names=db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('alembic_version','sqlite_sequence') ORDER BY name").scalars().all()
    return {n:db.exec_driver_sql('SELECT * FROM "'+n+'" ORDER BY rowid').all() for n in names}

def test_manufacturer_migration_preserves_every_row_and_roundtrips(tmp_path):
    engine,_=seed_0013(tmp_path);migrate(engine,"0015")
    with engine.connect() as db:
        before=snapshot(db)
        triggers=db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all()
        indexes=db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='inventory_gpsr_requirements' ORDER BY name").all()
    migrate(engine,"0016")
    with engine.connect() as db:
        assert snapshot(db)==before
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all()==triggers
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='inventory_gpsr_requirements' ORDER BY name").all()==indexes
        assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
    migrate(engine,"0015",downgrade=True)
    with engine.connect() as db:assert snapshot(db)==before
    engine.dispose()

def test_manufacturer_downgrade_blocks_data_loss_and_unique_intents_remain(tmp_path):
    engine,_=seed_0013(tmp_path);migrate(engine,"0016")
    with engine.begin() as db:
        db.exec_driver_sql("INSERT INTO inventory_gpsr_requirements (id,item_id,run_id,store_id,marketplace_code,sku,asin,requirement_type,state,reason,eligible,intent_at,created_at,updated_at) SELECT 'maker',item_id,run_id,store_id,marketplace_code,sku,asin,'manufacturer',state,reason,eligible,intent_at,created_at,updated_at FROM inventory_gpsr_requirements LIMIT 1")
    with pytest.raises(RuntimeError,match="制造商"):migrate(engine,"0015",downgrade=True)
    with engine.connect() as db:
        assert db.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()=="0016"
        assert db.exec_driver_sql("SELECT state FROM inventory_gpsr_requirements WHERE id='maker'").scalar()=="CONFIRMED"
        assert db.exec_driver_sql("PRAGMA foreign_keys").scalar()==1
    with pytest.raises(IntegrityError),engine.begin() as db:
        db.exec_driver_sql("UPDATE inventory_gpsr_requirements SET requirement_type='unknown' WHERE id='maker'")
    engine.dispose()

def test_manufacturer_rebuild_failure_rolls_back(tmp_path,monkeypatch):
    engine,_=seed_0013(tmp_path);migrate(engine,"0015")
    path=Path(__file__).resolve().parents[2]/"migrations/versions/0016_inventory_manufacturer.py"
    spec=importlib.util.spec_from_file_location("migration0016",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    original=module.rewrite
    def fail(*args):original(*args);raise RuntimeError("fixture interrupted")
    monkeypatch.setattr(module,"rewrite",fail)
    with engine.connect() as db:
        before=snapshot(db)
        schema=db.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all()
        with Operations.context(MigrationContext.configure(db)):
            with pytest.raises(RuntimeError,match="fixture interrupted"):module.upgrade()
        assert snapshot(db)==before
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master ORDER BY name").all()==schema
        assert db.exec_driver_sql("PRAGMA foreign_keys").scalar()==1
    engine.dispose()
