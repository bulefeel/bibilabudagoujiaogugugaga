"""Basic passes and optional capabilities have independent, durable evidence."""
import pytest
from sqlalchemy import inspect

from tests.unit.test_setup_persistence import records, begin, passed
from tests.unit.test_disbursement_sites_migration import snapshot
from tests.unit.test_inventory_compliance_migration import migrate
from ziniao_automation.workflows.store_setup.adapter import SiteSetupResult


def test_unknown_inventory_is_saved_without_erasing_basic_pass(records):
    repo, _, sid = records
    pid = begin(repo, sid, ['IT'])['probe_id']
    repo.progress(pid, sites=[{'code':'IT', 'status':'CHECKING'}], lifecycle='CHECKING', phase='CHECK_SITE')
    repo.progress(pid, sites=[{'code':'IT', 'status':'SUCCEEDED', **SiteSetupResult(
        'PASSED', 'PASSED', 'NOT_CHECKED', 'dom:visible_account', inventory_capability='UNKNOWN').to_payload()}])
    repo.finish(pid, outcome='SUCCEEDED', cleanup='CONFIRMED')
    site = repo.get(sid, pid)['marketplaces'][0]
    assert site['outcome'] == 'PASSED'
    assert site['inventory_capability'] == 'UNKNOWN'
    assert site['payment_capability'] == 'NOT_CHECKED'
    assert repo.summary(sid)['marketplaces']['IT']['inventory_capability'] == 'UNKNOWN'


def test_unknown_capability_value_is_not_accepted():
    with pytest.raises(ValueError, match='inventory capability'):
        SiteSetupResult('PASSED', 'PASSED', 'UNKNOWN', 'fixture', inventory_capability='invented')


def test_page_receives_only_public_tracking_parameters(tmp_path):
    import json
    import re
    from html import unescape
    from fastapi.testclient import TestClient
    from ziniao_automation.config import Settings
    from ziniao_automation.web import create_app
    app = create_app(Settings(project_root=tmp_path, data_dir=tmp_path/'data', testing=True))
    with TestClient(app) as client:
        page = client.get('/stores')
        assert page.status_code == 200
        attribute = re.search(r"data-tracking-policy='([^']*)'", page.text)
        assert attribute is not None
        policy = json.loads(unescape(attribute.group(1)))
        assert set(policy) == {'batch_poll_interval_ms', 'batch_poll_request_timeout_ms',
                              'batch_poll_error_retries', 'batch_poll_finalization_grace_ms', 'total_deadline_seconds'}
        assert policy['total_deadline_seconds'] == 1800


def test_migration_roundtrip_preserves_historical_checks_timestamps_and_business_rows(records):
    repo, sessions, sid = records
    pid = begin(repo, sid, ['DE'])['probe_id']
    passed(repo, pid, 'DE')
    repo.finish(pid, outcome='SUCCEEDED', cleanup='CONFIRMED')
    engine = sessions.kw['bind']
    migrate(engine, '0018', downgrade=True)
    before = snapshot(engine)
    old_columns = [c['name'] for c in inspect(engine).get_columns('store_setup_site_results')]
    assert 'inventory_capability' not in old_columns
    for _ in range(2):
        migrate(engine, '0019')
        after = snapshot(engine)
        with engine.connect() as db:
            after['store_setup_site_results'] = db.exec_driver_sql(
                'SELECT '+','.join(old_columns)+' FROM store_setup_site_results ORDER BY 1').all()
            assert db.exec_driver_sql('SELECT DISTINCT inventory_capability FROM store_setup_site_results').scalars().all() == ['NOT_CHECKED']
            assert db.exec_driver_sql('PRAGMA integrity_check').scalar() == 'ok'
            assert not db.exec_driver_sql('PRAGMA foreign_key_check').all()
        assert after == before
        summary = repo.summary(sid)
        assert summary['marketplaces']['DE']['outcome'] == 'PASSED'
        assert not summary['stale']
        migrate(engine, '0018', downgrade=True)
        assert snapshot(engine) == before
    migrate(engine, '0019')
