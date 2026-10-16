"""EU setup request, persistence and dispatch; no real browsers or tasks."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.unit.test_setup_persistence import records, begin, passed, failed
from tests.unit.test_store_setup_runtime import _Controller, _service, _terminal
from ziniao_automation.models import StoreMarketplace, OperationGuard
from ziniao_automation.repositories import StoreRepository
from ziniao_automation.schemas import StoreSetupInput, StoreSetupBatchPreview, MarketplaceInput
from ziniao_automation.workflows.amazon_disbursement.config import ALLOWED_MARKETPLACES
from ziniao_automation.workflows.store_setup.adapter import SiteSetupResult
from ziniao_automation.ziniao.models import ProfileSelector

EU = ['IT', 'FR', 'ES', 'PL']


def test_eu_save_setup_and_batch_contracts_are_consistent():
    from ziniao_automation.workflows.amazon_account_switcher import MARKETPLACE_NAMES
    from ziniao_automation.workflows.store_setup.config import SETUP_MARKETPLACES
    for code in EU + ['DE']:
        assert MarketplaceInput(code=code, enabled=True).code == code
        assert StoreSetupInput(marketplace_codes=[code]).marketplace_codes == [code]
        assert SETUP_MARKETPLACES[code][0] in MARKETPLACE_NAMES
    request = StoreSetupBatchPreview(store_ids=[1], selections={1: EU})
    assert request.selections[1] == EU
    assert set(ALLOWED_MARKETPLACES) == {'CA', 'UK', 'AU'}


def test_batch_explicit_sites_add_eu_without_defaulting_or_removing_ca(records):
    repo, sessions, sid = records
    with sessions.begin() as db:
        StoreRepository(db).replace_marketplaces(sid, [{'code': 'CA', 'enabled': True}])
    revision = repo.summary(sid)['setup_revision']
    preview = repo.preview([sid], 'fill_missing', {sid: EU})['targets'][0]
    assert preview['eligible'] and not preview['defaulted']
    assert preview['marketplace_codes'] == EU
    assert repo.summary(sid)['setup_revision'] == revision
    rid = str(uuid4())
    result = begin(repo, sid, EU, request_id=rid, expected_revision=revision)
    assert begin(repo, sid, EU, request_id=rid, expected_revision=revision)['probe_id'] == result['probe_id']
    assert {s['code'] for s in result['marketplaces']} == set(EU)
    with sessions() as db:
        enabled = db.scalars(select(StoreMarketplace).where(StoreMarketplace.enabled.is_(True))).all()
        assert {m.code for m in enabled} == {'CA', *EU}
        assert list(db.scalars(select(OperationGuard))) == []


def test_explicit_batch_sites_on_empty_store_never_become_payout_sites(records):
    repo, _, sid = records
    target = repo.preview([sid], 'fill_missing', {sid: EU})['targets'][0]
    assert target['eligible'] and target['marketplace_codes'] == EU and not target['defaulted']
    assert not repo.preview([sid], 'retry_unverified', {sid: EU})['targets'][0]['eligible']


def test_eu_partial_retry_keeps_binding_and_passed_sites(records):
    repo, _, sid = records
    row = begin(repo, sid, EU)
    pid = row['probe_id']
    repo.bind_identity(pid, 'SELLER', ProfileSelector('oauth','fixture-env'), 'IT')
    for code in EU[:-1]:
        passed(repo, pid, code)
    failed(repo, pid, 'PL')
    repo.finish(pid, outcome='PARTIAL', cleanup='CONFIRMED')
    summary = repo.summary(sid)
    assert summary['identity_confirmed'] and summary['verification_outcome'] == 'PARTIAL'
    target = repo.preview([sid], 'retry_unverified', {})['targets'][0]
    assert target['marketplace_codes'] == ['PL']
    retried = begin(repo, sid, ['PL'], scope='retry_unverified')
    assert repo.identity_bootstrap_codes(retried['probe_id'])[-1] == 'PL'
    passed(repo, retried['probe_id'], 'PL')
    repo.finish(retried['probe_id'], outcome='SUCCEEDED', cleanup='CONFIRMED')
    assert repo.summary(sid)['verification_outcome'] == 'SUCCEEDED'


async def test_unified_dispatches_mixed_sites_in_one_session_without_financial_operations(tmp_path, monkeypatch):
    from ziniao_automation.workflows.store_setup.adapter import AmazonStoreSetupPage
    controller = _Controller()
    service, sessions, sid = _service(tmp_path, controller)
    checked = []
    async def identity(*args): return 'SELLER', 'dom:visible_account'
    async def check(self, page, store, marketplace):
        checked.append(marketplace.code)
        return SiteSetupResult('PASSED', 'PASSED', 'NOT_CHECKED' if marketplace.code in EU else 'SUPPORTED', 'dom:visible_account')
    monkeypatch.setattr(AmazonStoreSetupPage, 'detect_identity', identity)
    monkeypatch.setattr(AmazonStoreSetupPage, 'check_site', check)
    row = await service.detect_store_setup(sid, ['CA', *EU])
    result = await _terminal(service, sid, row['probe_id'])
    assert result['status'] == 'SUCCEEDED' and checked == ['CA', *EU]
    assert controller.opens == 1
    with sessions() as db:
        assert list(db.scalars(select(OperationGuard))) == []


def test_store_patch_eu_then_batch_preview_uses_saved_sites(tmp_path):
    from fastapi.testclient import TestClient
    from ziniao_automation.config import Settings
    from ziniao_automation.models import Store
    from ziniao_automation.web import create_app
    app = create_app(Settings(project_root=tmp_path, data_dir=tmp_path/'data', testing=True))
    with TestClient(app) as client:
        client.get('/')
        headers = {'X-CSRF-Token': client.cookies.get('ziniao_csrf')}
        with app.state.sessions.begin() as db:
            store = Store(name='Fixture', selector_type='oauth', selector_value='env', raw_profile={'platform_id':'1'})
            db.add(store); db.flush(); sid = store.id
        response = client.patch(f'/api/stores/{sid}', json={
            'marketplaces': [{'code': c, 'enabled': True} for c in EU]}, headers=headers)
        assert response.status_code == 200, response.text
        response = client.post('/api/stores/setup-batch/preview', json={
            'store_ids': [sid], 'mode':'fill_missing'}, headers=headers)
        assert response.status_code == 200
        target = response.json()['targets'][0]
        assert target['eligible'] and set(target['marketplace_codes']) == set(EU)
        assert not target['defaulted']
