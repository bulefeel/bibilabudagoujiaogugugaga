"""Incremental onboarding of existing stores; SQLite only, no browser or payments."""
from copy import deepcopy

import pytest
from sqlalchemy import select, text

from tests.unit.test_setup_persistence import records, begin, passed, failed
from ziniao_automation.models import OperationGuard, StoreSetupSiteResult
from ziniao_automation.repositories import StoreRepository
from ziniao_automation.setup_repository import SetupConflict, SetupRepository
from ziniao_automation.ziniao.models import ProfileSelector


def established(repo, sid, codes=('CA', 'UK', 'AU')):
    row = begin(repo, sid, codes)
    repo.bind_identity(row['probe_id'], 'SELLER', ProfileSelector('oauth', 'fixture-env'), codes[0])
    for code in codes:
        passed(repo, row['probe_id'], code)
    repo.finish(row['probe_id'], outcome='SUCCEEDED', cleanup='CONFIRMED')
    return row['probe_id']


def test_add_sites_to_configured_store_preserves_previous_facts_and_history(records):
    repo, sessions, sid = records
    original = established(repo, sid)
    before = deepcopy(repo.summary(sid))
    history = deepcopy(repo.get(sid, original))
    target = repo.preview([sid], 'fill_missing', {sid: ['CA', 'US', 'DE']})['targets'][0]
    assert target['eligible']
    assert target['marketplace_codes'] == ['US', 'DE']
    assert target['added_marketplace_codes'] == ['US', 'DE']
    assert target['already_passed_marketplace_codes'] == ['CA']
    assert set(target['preserved_marketplace_codes']) == {'CA', 'UK', 'AU'}
    assert repo.summary(sid) == before  # preview has no writes
    row = begin(repo, sid, target['marketplace_codes'], expected_revision=target['setup_revision'])
    for code in ('CA', 'UK', 'AU'):
        assert repo.summary(sid)['marketplaces'][code] == before['marketplaces'][code]
    passed(repo, row['probe_id'], 'US')
    failed(repo, row['probe_id'], 'DE')
    repo.finish(row['probe_id'], outcome='PARTIAL', cleanup='CONFIRMED')
    summary = SetupRepository(sessions).summary(sid)
    assert summary['expected_seller_id'] == 'SELLER' and summary['identity_confirmed']
    assert set(summary['selected_marketplace_codes']) == {'CA', 'UK', 'AU', 'US', 'DE'}
    assert summary['verification_outcome'] == 'PARTIAL'
    assert not summary['stale']
    assert repo.get(sid, original) == history  # no rewriting historical probes
    retry = repo.preview([sid], 'fill_missing', {})['targets'][0]
    assert retry['eligible'] and retry['marketplace_codes'] == ['DE']
    with sessions() as db:
        assert {s.marketplace_code for s in db.scalars(select(StoreSetupSiteResult).where(
            StoreSetupSiteResult.probe_id == row['probe_id']))} == {'DE', 'US'}


def test_successive_additions_reuse_unchanged_sites_without_rechecking(records):
    repo, _, sid = records
    established(repo, sid, ['CA'])
    for code in ['US', 'DE', 'IT']:
        target = repo.preview([sid], 'fill_missing', {sid: ['CA', code]})['targets'][0]
        assert target['marketplace_codes'] == [code]
        row = begin(repo, sid, [code], expected_revision=target['setup_revision'])
        passed(repo, row['probe_id'], code)
        repo.finish(row['probe_id'], outcome='SUCCEEDED', cleanup='CONFIRMED')
        assert repo.summary(sid)['verification_outcome'] == 'SUCCEEDED'
    assert repo.preview([sid], 'fill_missing', {})['eligible_count'] == 0
    full = repo.preview([sid], 'recheck', {sid: ['CA', 'US']})['targets'][0]
    assert full['marketplace_codes'] == ['CA', 'US'] and not full['already_passed_marketplace_codes']


def test_saved_new_site_is_eligible_even_with_confirmed_identity(records):
    repo, sessions, sid = records
    established(repo, sid, ['CA'])
    with sessions.begin() as db:
        StoreRepository(db).replace_marketplaces(sid, [{'code': c, 'enabled': True} for c in ['CA', 'US']])
    target = repo.preview([sid], 'fill_missing', {})['targets'][0]
    assert target['eligible'] and target['marketplace_codes'] == ['US']
    assert not target['added_marketplace_codes']
    assert repo.summary(sid)['marketplaces']['CA']['outcome'] == 'PASSED'


@pytest.mark.parametrize('change', ['identity', 'environment', 'disable', 'domain', 'reset'])
def test_changes_then_restore_never_resurrect_old_verification(records, change):
    repo, sessions, sid = records
    established(repo, sid, ['CA'])
    with sessions.begin() as db:
        if change == 'reset':
            StoreRepository(db).reset_setup(sid)
            StoreRepository(db).update(sid, expected_seller_id='SELLER', identity_confirmed=True)
        elif change in {'identity', 'environment'}:
            field = 'expected_seller_id' if change == 'identity' else 'selector_value'
            original = 'SELLER' if change == 'identity' else 'fixture-env'
            for value in ['OTHER', original]:
                db.execute(text(f'UPDATE stores SET {field}=:value WHERE id=:sid'), {'value': value, 'sid': sid})
        else:
            field = 'enabled' if change == 'disable' else 'domain'
            values = [0, 1] if change == 'disable' else ['wrong.invalid', 'sellercentral.amazon.ca']
            for value in values:
                db.execute(text(f'UPDATE store_marketplaces SET {field}=:value WHERE store_id=:sid'), {'value': value, 'sid': sid})
        StoreRepository(db).replace_marketplaces(sid, [{'code': c, 'enabled': True} for c in ['CA', 'US']])
    assert 'CA' not in repo.summary(sid)['marketplaces']
    row = begin(repo, sid, ['US'])
    passed(repo, row['probe_id'], 'US')
    repo.finish(row['probe_id'], outcome='SUCCEEDED', cleanup='CONFIRMED')
    assert 'CA' not in repo.summary(sid)['marketplaces']
    assert repo.preview([sid], 'fill_missing', {})['targets'][0]['marketplace_codes'] == ['CA']


def test_new_failure_overrides_old_success_after_addition(records):
    repo, _, sid = records
    established(repo, sid, ['CA'])
    row = begin(repo, sid, ['CA', 'US'])
    failed(repo, row['probe_id'], 'CA')
    passed(repo, row['probe_id'], 'US')
    repo.finish(row['probe_id'], outcome='PARTIAL', cleanup='CONFIRMED')
    assert repo.summary(sid)['marketplaces']['CA']['outcome'] == 'BLOCKED'


def test_new_sites_still_require_fresh_preview_and_do_not_expand_retry(records):
    repo, sessions, sid = records
    established(repo, sid, ['CA'])
    target = repo.preview([sid], 'fill_missing', {sid: ['US']})['targets'][0]
    assert not repo.preview([sid], 'retry_unverified', {sid: ['US']})['targets'][0]['eligible']
    with sessions.begin() as db:
        StoreRepository(db).update(sid, expected_seller_id='OTHER')
    with pytest.raises(SetupConflict):
        begin(repo, sid, ['US'], expected_revision=target['setup_revision'])
    assert repo.summary(sid)['selected_marketplace_codes'] == ['CA']


async def test_incremental_batch_executes_only_new_sites_in_one_session(tmp_path, monkeypatch):
    from tests.unit.test_store_setup_runtime import _Controller, _service, _terminal
    from ziniao_automation.workflows.store_setup.adapter import AmazonStoreSetupPage, SiteSetupResult

    controller = _Controller()
    service, sessions, sid = _service(tmp_path, controller, seller_id='SELLER', confirmed=True, enabled=True)
    repo = service.setup_records
    first = begin(repo, sid, ['CA'])
    passed(repo, first['probe_id'], 'CA')
    repo.finish(first['probe_id'], outcome='SUCCEEDED', cleanup='CONFIRMED')
    checked = []
    async def identity(*args):
        return 'SELLER', 'dom:visible_account'
    async def check(self, page, store, marketplace):
        checked.append(marketplace.code)
        return SiteSetupResult('PASSED', 'PASSED', 'NOT_CHECKED', 'dom:visible_account')
    monkeypatch.setattr(AmazonStoreSetupPage, 'detect_identity', identity)
    monkeypatch.setattr(AmazonStoreSetupPage, 'check_site', check)
    preview = repo.preview([sid], 'fill_missing', {sid: ['CA', 'US', 'DE']})['targets'][0]
    row = await service.detect_store_setup(sid, preview['marketplace_codes'],
        scope=preview['scope'], expected_setup_revision=preview['setup_revision'])
    result = await _terminal(service, sid, row['probe_id'])
    assert result['status'] == 'SUCCEEDED'
    assert checked == ['US', 'DE'] and controller.opens == 1
    assert repo.summary(sid)['verification_outcome'] == 'SUCCEEDED'
    with sessions() as db:
        assert not list(db.scalars(select(OperationGuard)))
