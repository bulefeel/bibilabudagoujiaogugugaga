"""Migration 0015 roundtrip and non-lossy downgrade boundaries."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests.unit.test_inventory_compliance_migration import migrate
from tests.unit.test_setup_persistence import records, begin, passed


def rows(engine):
    with engine.connect() as db:
        return db.exec_driver_sql('SELECT * FROM store_setup_site_results ORDER BY id').all()


def test_roundtrip_preserves_results_constraints_and_triggers(records):
    repo, sessions, sid = records
    row = begin(repo, sid, ['CA'])
    passed(repo, row['probe_id'], 'CA')
    repo.finish(row['probe_id'], outcome='SUCCEEDED', cleanup='CONFIRMED')
    engine = sessions.kw['bind']
    # Compare the columns at this migration's source revision, not at head.
    migrate(engine, '0015', downgrade=True)
    before = rows(engine)
    with engine.connect() as db:
        triggers = db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all()
    migrate(engine, '0014', downgrade=True)
    assert rows(engine) == before
    migrate(engine, '0015')
    assert rows(engine) == before
    with engine.connect() as db:
        assert db.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        assert not db.exec_driver_sql('PRAGMA foreign_key_check').all()
        assert db.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all() == triggers
        ddl = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='store_setup_site_results'").scalar()
        assert 'outcome IN' in ddl and 'uq_setup_site' in ddl and "'PL'" in ddl


def test_used_eu_results_prevent_destructive_downgrade(records):
    repo, sessions, sid = records
    row = begin(repo, sid, ['IT'])
    repo.finish(row['probe_id'], outcome='FAILED', cleanup='CONFIRMED')
    engine = sessions.kw['bind']
    migrate(engine, '0015', downgrade=True)
    before = rows(engine)
    with pytest.raises(RuntimeError, match='欧洲站建档记录'):
        migrate(engine, '0014', downgrade=True)
    assert rows(engine) == before
    with engine.connect() as db:
        assert db.execute(text('SELECT version_num FROM alembic_version')).scalar() == '0015'


def test_mid_rebuild_error_restores_original_table(records, monkeypatch):
    repo, sessions, sid = records
    row = begin(repo, sid, ['CA'])
    repo.finish(row['probe_id'], outcome='FAILED', cleanup='CONFIRMED')
    engine = sessions.kw['bind']
    migrate(engine, '0014', downgrade=True)
    path = Path(__file__).resolve().parents[2]/'migrations/versions/0015_setup_eu_marketplaces.py'
    spec = importlib.util.spec_from_file_location('migration0015', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.rewrite
    def fail(condition):
        original(condition)
        raise RuntimeError('fixture interrupted')
    monkeypatch.setattr(module, 'rewrite', fail)
    before = rows(engine)
    with engine.connect() as db:
        ddl = db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='store_setup_site_results'").scalar()
        with Operations.context(MigrationContext.configure(db)):
            with pytest.raises(RuntimeError, match='fixture interrupted'):
                module.upgrade()
        assert db.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='store_setup_site_results'").scalar() == ddl
        assert db.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
    assert rows(engine) == before
