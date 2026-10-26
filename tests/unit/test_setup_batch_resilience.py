"""Offline reproductions of incremental setup and transient launch failures."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from tests.unit.test_setup_persistence import records, begin, passed
from tests.unit.test_setup_add_sites import established
from tests.unit.test_store_setup_runtime import _Controller, _service, _terminal
from tests.unit.test_ziniao_controller import FakeClient, FakeSessions, SequencedHealth
from tests.workflows.test_store_setup_adapter import adapter, site, store, policy
from ziniao_automation.models import OperationGuard
from ziniao_automation.setup_repository import SetupConflict
from ziniao_automation.repositories import StoreRepository
from ziniao_automation.workflows.store_setup.adapter import AmazonStoreSetupPage, SiteSetupResult
from ziniao_automation.workflows.store_setup.identity import SetupRejected
from ziniao_automation.workflows.store_setup.inventory_adapter import InventoryStoreSetupPage
from ziniao_automation.workflows.amazon_landing import LandingOutcome
from ziniao_automation.workflows.store_setup.config import SETUP_MARKETPLACES
from ziniao_automation.workflows.types import MarketplaceRef
from ziniao_automation.ziniao.controller import ZiniaoController, ZiniaoControllerConfig
from ziniao_automation.ziniao.errors import ZiniaoApiError, ZiniaoLaunchError
from ziniao_automation.ziniao.models import ProfileSelector


def test_incremental_full_probe_uses_proven_site_only_as_live_identity_entry(records):
    repo, _, sid = records
    established(repo, sid, ['CA', 'US'])
    preview = repo.preview([sid], 'fill_missing', {sid: ['CA', 'US', 'UK', 'DE']})['targets'][0]
    row = begin(repo, sid, preview['marketplace_codes'], scope=preview['scope'])
    assert [s['code'] for s in row['marketplaces']] == ['UK', 'DE']
    assert repo.identity_bootstrap_codes(row['probe_id']) == ['CA', 'US', 'UK', 'DE']
    # A bootstrap entry does not create another site result or mark a new pass.
    saved = repo.get(sid, row['probe_id'])
    assert {s['code'] for s in saved['marketplaces']} == {'UK', 'DE'}
    assert all(s['outcome'] is None for s in saved['marketplaces'])


@pytest.mark.parametrize('field,value', [('expected_seller_id','OTHER'), ('selector_value','other-env')])
def test_changed_identity_or_environment_cannot_use_old_bootstrap(records, field, value):
    repo, sessions, sid = records
    established(repo, sid, ['CA'])
    with sessions.begin() as db:
        StoreRepository(db).update(sid, **{field: value})
    row = begin(repo, sid, ['US'])
    assert repo.identity_bootstrap_codes(row['probe_id']) == ['US']
    with sessions.begin() as db:
        db.execute(text(f'UPDATE stores SET {field}=:value WHERE id=:sid'),
                   {'value': value + '-changed', 'sid': sid})
    with pytest.raises(SetupConflict):
        repo.identity_bootstrap_codes(row['probe_id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['identity', 'site'])
async def test_navigation_timeout_becomes_site_local_failure(phase):
    subject, nav = adapter('<body></body>')
    nav._goto = AsyncMock(side_effect=PlaywrightTimeoutError('PRIVATE_URL_OR_TOKEN'))
    with pytest.raises(SetupRejected) as caught:
        if phase == 'identity':
            await subject.detect_identity(nav.page, site())
        else:
            await subject.check_site(nav.page, store(), site())
    assert caught.value.diagnostic.reason_code == 'NAVIGATION_TIMEOUT'
    assert not caught.value.stop_store
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['REGISTRATION_REQUIRED', 'MARKETPLACE_NOT_OFFERED', 'NAVIGATION_TIMEOUT'])
async def test_incremental_setup_continues_after_unavailable_first_site(tmp_path, monkeypatch, reason):
    controller = _Controller()
    service, sessions, sid = _service(tmp_path, controller, seller_id='SELLER', confirmed=True, enabled=True)
    first = begin(service.setup_records, sid, ['CA'])
    passed(service.setup_records, first['probe_id'], 'CA')
    service.setup_records.finish(first['probe_id'], outcome='SUCCEEDED', cleanup='CONFIRMED')
    identity_calls, site_calls = [], []
    async def identity(self, page, marketplace):
        identity_calls.append(marketplace.code)
        if marketplace.code != 'CA':
            raise SetupRejected('IDENTITY_CURRENT_NOT_FOUND')
        return 'SELLER', 'dom:content'
    async def check(self, page, store, marketplace):
        site_calls.append(marketplace.code)
        if marketplace.code == 'UK':
            raise SetupRejected(reason)
        return SiteSetupResult('PASSED', 'PASSED', 'UNKNOWN', 'dom:content')
    monkeypatch.setattr(AmazonStoreSetupPage, 'detect_identity', identity)
    monkeypatch.setattr(AmazonStoreSetupPage, 'check_site', check)
    row = await service.detect_store_setup(sid, ['UK', 'US', 'DE'])
    result = await _terminal(service, sid, row['probe_id'])
    assert identity_calls == ['CA'] and site_calls == ['UK', 'US', 'DE']
    assert result['status'] == 'PARTIAL' and result['identity']['status'] == 'SUCCEEDED'
    assert [s['status'] for s in result['marketplaces']] == ['FAILED', 'SUCCEEDED', 'SUCCEEDED']
    assert result['cleanup_state'] == 'CONFIRMED' and controller.opens == 1
    with sessions() as db:
        assert not list(db.scalars(select(OperationGuard)))


@pytest.mark.asyncio
async def test_unexpected_error_does_not_borrow_previous_registration_reason(tmp_path, monkeypatch, caplog):
    service, _, sid = _service(tmp_path, _Controller())
    async def identity(*args): return 'SELLER', 'dom:content'
    async def check(self, page, store, marketplace):
        if marketplace.code == 'CA': raise SetupRejected('REGISTRATION_REQUIRED')
        raise TypeError('PRIVATE_COOKIE_SECRET')
    monkeypatch.setattr(AmazonStoreSetupPage, 'detect_identity', identity)
    monkeypatch.setattr(AmazonStoreSetupPage, 'check_site', check)
    row = await service.detect_store_setup(sid, ['CA', 'UK', 'US'])
    result = await _terminal(service, sid, row['probe_id'])
    assert result['reason_code'] == 'SETUP_FAILED'
    assert result['marketplaces'][0]['reason_code'] == 'REGISTRATION_REQUIRED'
    assert result['marketplaces'][1]['reason_code'] == 'SETUP_FAILED'
    assert not result.get('last_diagnostic')
    assert 'exception_type=TypeError' in caplog.text
    assert 'phase=site_access' in caplog.text
    assert 'PRIVATE' not in caplog.text + json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('switch_reason,expected', [('not_offered','MARKETPLACE_NOT_OFFERED'),
    ('registration_required','REGISTRATION_REQUIRED'), ('account_mismatch','IDENTITY_MISMATCH'),
    ('unrecognized-private-value','MARKETPLACE_UNKNOWN')])
async def test_inventory_landing_preserves_closed_switch_reason(switch_reason, expected):
    subject = InventoryStoreSetupPage(policy(candidate_ready_timeout_ms=45000))
    class Page:
        url = 'https://sellercentral.amazon.it/account-switcher/default/merchantMarketplace'
        def is_closed(self): return False
    page = Page()
    subject.inventory.landing.land_on = AsyncMock(return_value=LandingOutcome(
        ok=False, reason='marketplace_not_selected', switch_reason=switch_reason, page=page))
    domain, currency = SETUP_MARKETPLACES['IT']
    with pytest.raises(SetupRejected) as caught:
        await subject._open_inventory(page, MarketplaceRef(id='IT', code='IT', domain=domain, currency=currency))
    assert caught.value.diagnostic.reason_code == expected
    assert caught.value.stop_store == (expected == 'IDENTITY_MISMATCH')


@pytest.mark.asyncio
@pytest.mark.parametrize('message,reason', [('网络异常，请稍后重试','ZINIAO_NETWORK_ERROR'),
    ('Property browser_type is invalid: undefined','ZINIAO_LAUNCH_FAILED')])
async def test_launch_retries_have_spacing_and_next_store_still_runs(monkeypatch, message, reason):
    class Client(FakeClient):
        async def start_browser(self, selector):
            if selector.value == 'bad':
                self.actions.append(('start', selector.value))
                raise ZiniaoApiError('startBrowser', '-10000', message)
            return await super().start_browser(selector)
    client = Client([9001])
    controller = ZiniaoController(client, sessions=FakeSessions(), health_checker=SequencedHealth([True]),
        config=ZiniaoControllerConfig(start_retry_delay_seconds=3), process_running=lambda: True)
    delays = []
    async def sleep(seconds): delays.append(seconds)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(ZiniaoLaunchError) as caught:
        await controller.open_store(ProfileSelector('oauth','bad'))
    assert caught.value.reason_code == reason and caught.value.cleanup_confirmed
    assert delays == [3, 6, 9]
    assert client.actions == [('start','bad'), ('stop','bad')] * 4
    handle = await controller.open_store(ProfileSelector('oauth','good'))
    await handle.close()
    assert client.actions[-2:] == [('start','good'), ('stop','good')]


@pytest.mark.parametrize('value', [0, -1, 31, float('nan'), float('inf')])
def test_retry_delay_rejects_unbounded_values(value):
    with pytest.raises(ValueError): ZiniaoControllerConfig(start_retry_delay_seconds=value)


@pytest.mark.asyncio
async def test_cancel_during_launch_backoff_does_not_start_again(monkeypatch):
    class Client(FakeClient):
        async def start_browser(self, selector):
            if selector.value == 'bad':
                self.actions.append(('start', selector.value))
                raise ZiniaoApiError('startBrowser', '-10000', '网络异常')
            return await super().start_browser(selector)
    waiting = asyncio.Event()
    async def sleep(_):
        waiting.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    client = Client([9001])
    controller = ZiniaoController(client, sessions=FakeSessions(), health_checker=SequencedHealth([True]),
        process_running=lambda: True)
    task = asyncio.create_task(controller.open_store(ProfileSelector('oauth','bad')))
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert [a for a in client.actions if a[0] == 'start'] == [('start','bad')]
    handle = await asyncio.wait_for(controller.open_store(ProfileSelector('oauth','good')), 1)
    await handle.close()


@pytest.mark.asyncio
async def test_settings_retry_interval_is_forwarded_to_controller(monkeypatch):
    from pathlib import Path
    from ziniao_automation.config import Settings
    from ziniao_automation.ziniao.factory import build_controller
    monkeypatch.setattr('ziniao_automation.ziniao.locate.resolve_ziniao_executable', lambda _: Path('fixture.exe'))
    monkeypatch.setenv('ZINIAO_START_RETRY_DELAY_SECONDS', '4.5')
    settings = Settings.from_env()
    controller = build_controller(start_retry_delay_seconds=settings.ziniao_start_retry_delay_seconds)
    try:
        assert controller.config.start_retry_delay_seconds == 4.5
    finally:
        await controller.client.close()
