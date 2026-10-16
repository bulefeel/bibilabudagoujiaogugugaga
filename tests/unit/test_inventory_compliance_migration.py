"""0014 preserves historical submission evidence and SQLite mirror triggers."""
import importlib.util
from pathlib import Path

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ziniao_automation.db import _migration_config
from tests.unit.test_store_history_migration import legacy


def migrate(engine, target, downgrade=False):
    config = _migration_config()
    with engine.connect() as db:
        config.attributes['connection'] = db
        (command.downgrade if downgrade else command.upgrade)(config, target)


def seed_0013(tmp_path):
    _, engine, _, ids, saved = legacy(tmp_path)
    migrate(engine, '0013')
    with engine.begin() as db:
        db.exec_driver_sql("INSERT INTO inventory_gpsr_items "
            "(id,run_id,store_id,marketplace_code,sku,asin,title,page_number,position,state,reason,eligible,intent_at,confirmed_at,created_at,updated_at) "
            "VALUES ('old-proof',?,?, 'DE','SKU-OLD','B000000001','',1,0,'CONFIRMED','submission_confirmed',1,"
            "'2026-10-01 01:00:00','2026-10-01 01:01:00','2026-10-01 00:00:00','2026-10-01 01:01:00')",
            (saved['run'], ids['two']))
        # Deleted IDs must not be reused even when all higher live IDs are gone.
        db.exec_driver_sql("UPDATE sqlite_sequence SET seq=100 WHERE name='store_marketplaces'")
    return engine, ids


def test_requirements_backfill_and_roundtrip_keep_evidence_and_history_triggers(tmp_path):
    engine, ids = seed_0013(tmp_path)
    with engine.connect() as db:
        before = db.exec_driver_sql('SELECT * FROM inventory_gpsr_items').all()
        triggers = db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all()
    migrate(engine, '0014')
    with engine.begin() as db:
        assert db.exec_driver_sql('SELECT * FROM inventory_gpsr_items').all() == before
        row = db.exec_driver_sql('SELECT item_id,requirement_type,state,intent_at,confirmed_at FROM inventory_gpsr_requirements').one()
        assert tuple(row) == ('old-proof','safety_attestation','CONFIRMED','2026-10-01 01:00:00','2026-10-01 01:01:00')
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all() == triggers
        sites = db.exec_driver_sql("SELECT id,enabled FROM store_marketplaces WHERE code IN ('IT','FR','ES','PL')").all()
        assert len(sites) == 8 and all(r.id > 100 and not r.enabled for r in sites)
        assert not db.exec_driver_sql('PRAGMA foreign_key_check').all()
        assert db.exec_driver_sql('SELECT count(*) FROM marketplace_history').scalar() == db.exec_driver_sql('SELECT count(*) FROM store_marketplaces').scalar()
    migrate(engine, '0013', downgrade=True)
    with engine.connect() as db:
        assert db.exec_driver_sql('SELECT * FROM inventory_gpsr_items').all() == before
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all() == triggers
        assert db.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        assert not db.exec_driver_sql('PRAGMA foreign_key_check').all()
    engine.dispose()


def test_mid_rebuild_failure_rolls_back_schema_triggers_and_sequence(tmp_path, monkeypatch):
    engine, _ = seed_0013(tmp_path)
    path = Path(__file__).resolve().parents[2] / 'migrations/versions/0014_inventory_compliance_requirements.py'
    spec = importlib.util.spec_from_file_location('migration0014', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.rewrite
    def fail(*args):
        original(*args)
        raise RuntimeError('fixture interrupted')
    monkeypatch.setattr(module, 'rewrite', fail)
    with engine.connect() as db:
        before = db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger','index') ORDER BY name").all()
        with Operations.context(MigrationContext.configure(db)):
            with pytest.raises(RuntimeError, match='fixture interrupted'):
                module.upgrade()
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger','index') ORDER BY name").all() == before
        assert db.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name='store_marketplaces'").scalar() == 100
        assert db.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
    engine.dispose()
