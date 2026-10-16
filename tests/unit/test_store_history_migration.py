"""Full 0010 schema upgrade, physical deletion, and lossless rollback coverage."""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import importlib.util

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, _migration_config
from ziniao_automation.models import Store, Run, OperationGuard
from ziniao_automation.repositories import StoreRepository
from tests.test_store_sync_deletion import seed_history

ROOT = Path(__file__).resolve().parents[2]


def legacy(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript((ROOT / "tests/fixtures/schema_0010.sql").read_text(encoding="utf-8"))
    engine = create_sqlite_engine(Settings(project_root=tmp_path, data_dir=tmp_path / "data", database_url=f"sqlite:///{path.as_posix()}", testing=True))
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as db:
        # Seed the frozen 0010 schema with SQL, not today's ORM insert defaults.
        for name in ("one", "two"):
            db.exec_driver_sql("INSERT INTO stores (name,selector_type,selector_value,identity_confirmed,enabled,raw_profile,created_at,updated_at) VALUES (?,'oauth',?,0,0,'{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)", ("Fixture " + name, name))
        ids = dict(db.exec_driver_sql("SELECT selector_value,id FROM stores").all())
    app = SimpleNamespace(state=SimpleNamespace(sessions=sessions))
    saved = seed_history((app,None,None,None,ids,tmp_path), legacy=True)
    return path, engine, sessions, ids, saved


def existing_rows(engine):
    with engine.connect() as db:
        # Compare the frozen 0010 graph. Later feature tables have their own
        # migration/data-preservation tests and do not exist in that fixture.
        tables = [row[0] for row in db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('alembic_version','store_history','marketplace_history','store_setup_probes','store_setup_site_results','inventory_gpsr_items','inventory_gpsr_requirements')")]
        result = {}
        for table in tables:
            columns = [r[1] for r in db.exec_driver_sql(f'PRAGMA table_info("{table}")') if r[1] != "setup_revision"]
            names = ",".join('"' + name + '"' for name in columns)
            # 0014 adds disabled site enrollment options, not legacy business data.
            where = " WHERE code NOT IN ('IT','FR','ES','PL')" if table == "store_marketplaces" else ""
            result[table] = db.exec_driver_sql(f'SELECT {names} FROM "{table}"{where} ORDER BY rowid').all()
        return result


def downgrade(engine):
    config=_migration_config()
    with engine.connect() as db:
        config.attributes["connection"]=db
        command.downgrade(config,"0010")


def test_complete_legacy_graph_upgrades_and_round_trips_without_changing_any_row(tmp_path):
    _, engine, sessions, ids, saved = legacy(tmp_path)
    before=existing_rows(engine)
    assert init_database(engine,backup_dir=tmp_path / "backup") is not None
    assert existing_rows(engine)==before
    with sessions() as db:
        assert db.get(Run,saved["run"]).store.name=="Fixture two"
        assert not db.execute(text("PRAGMA foreign_key_check")).all()
        assert db.scalar(text("PRAGMA integrity_check"))=="ok"
    downgrade(engine)
    assert existing_rows(engine)==before
    init_database(engine,backup_dir=tmp_path / "backup")
    assert existing_rows(engine)==before
    engine.dispose()


def test_deleted_history_prevents_lossy_downgrade_and_survives_failed_rollback(tmp_path):
    _, engine, sessions, ids, saved = legacy(tmp_path)
    init_database(engine,backup_dir=tmp_path / "backup")
    with engine.begin() as db:
        db.exec_driver_sql("DELETE FROM stores WHERE id=?",(ids["two"],))
    before=existing_rows(engine)
    with pytest.raises(RuntimeError,match="Archived history exists"):
        downgrade(engine)
    assert existing_rows(engine)==before
    with sessions() as db:
        assert db.scalar(text("SELECT id FROM stores WHERE id=:id"), {"id":ids["two"]}) is None
        assert db.get(Run,saved["run"]).store.name=="Fixture two"
        assert db.get(OperationGuard,saved["guard"]).guard_key=="fixture-per-day-guard"
        assert db.scalar(text("SELECT version_num FROM alembic_version"))=="0011"
        assert db.scalar(text("PRAGMA foreign_keys"))==1
        assert not db.execute(text("PRAGMA foreign_key_check")).all()
    engine.dispose()


def test_new_environment_ids_never_reuse_deleted_history_ids(tmp_path):
    _, engine, sessions, ids, saved = legacy(tmp_path)
    init_database(engine,backup_dir=tmp_path / "backup")
    with engine.begin() as db:
        db.exec_driver_sql("DELETE FROM stores WHERE id=?",(ids["two"],))
    with sessions.begin() as db:
        store=StoreRepository(db).create(name="New environment",selector_type="oauth",selector_value="different")
        assert store.id>ids["two"]
        regrant=StoreRepository(db).create(name="Regranted environment",selector_type="oauth",selector_value="two")
        assert regrant.id==ids["two"] and not regrant.enabled and not regrant.identity_confirmed
    engine.dispose()


def test_interrupted_rebuild_rolls_back_schema_and_reenables_foreign_keys(tmp_path,monkeypatch):
    _, engine, _, _, _=legacy(tmp_path)
    before=existing_rows(engine)
    spec=importlib.util.spec_from_file_location("fixture_migration",ROOT / "migrations/versions/0011_store_history_deletion.py")
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    original=module._retarget
    def fail_after_rebuild(*args):
        original(*args)
        raise RuntimeError("fixture interrupted migration")
    monkeypatch.setattr(module,"_retarget",fail_after_rebuild)
    with engine.connect() as db:
        with Operations.context(MigrationContext.configure(db)):
            with pytest.raises(RuntimeError,match="fixture interrupted"):
                module.upgrade()
        assert db.exec_driver_sql("PRAGMA foreign_keys").scalar()==1
        assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
        assert db.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name='store_history'").scalar()==0
    assert existing_rows(engine)==before
    engine.dispose()
