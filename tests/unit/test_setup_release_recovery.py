"""Closed setup failures must have an actionable, evidence-based release exit."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from tests.unit.test_setup_persistence import records, begin
from tests.unit.test_store_setup_runtime import _Controller, _service, _terminal
from tests.unit.test_ziniao_controller import FakeClient, FakeSessions, SequencedHealth
from ziniao_automation.models import Store, StoreSetupProbe, Run
from ziniao_automation.setup_repository import SetupConflict
from ziniao_automation.ziniao.controller import ZiniaoController, ZiniaoControllerConfig
from ziniao_automation.ziniao.errors import ZiniaoApiError, ZiniaoLaunchError
from ziniao_automation.ziniao.models import ProfileSelector


@pytest.mark.parametrize('acknowledged', [True, False])
async def test_device_unbound_is_not_retried_and_reports_actual_cleanup(acknowledged):
    class Client(FakeClient):
        async def start_browser(self, selector):
            self.actions.append(('start', selector.value))
            raise ZiniaoApiError('startBrowser', '-10000', '因当前账号未绑定设备,请绑定设备 错误码：104')
        async def stop_browser(self, selector, **kwargs):
            await super().stop_browser(selector, **kwargs)
            return acknowledged
    client = Client([])
    controller = ZiniaoController(client, process_running=lambda: True)
    with pytest.raises(ZiniaoLaunchError) as caught:
        await controller.open_store(ProfileSelector('oauth', 'fixture'), '1')
    assert caught.value.reason_code == 'ZINIAO_DEVICE_UNBOUND'
    assert caught.value.cleanup_confirmed is acknowledged
    assert client.actions == [('start', 'fixture'), ('stop', 'fixture')]
    assert not any(controller.locks.snapshot()['stores'].values())


@pytest.mark.parametrize('confirmed', [True, False])
async def test_setup_acquisition_failure_preserves_controller_cleanup(tmp_path, confirmed):
    class FailedController(_Controller):
        @asynccontextmanager
        async def financial_session(self, *args, **kwargs):
            raise ZiniaoLaunchError('fixture', cleanup_confirmed=confirmed, reason_code='ZINIAO_DEVICE_UNBOUND')
            yield
    service, sessions, sid = _service(tmp_path, FailedController())
    row = await service.detect_store_setup(sid, ['US'])
    result = await _terminal(service, sid, row['probe_id'])
    assert result['status'] == 'FAILED'
    assert result['cleanup_state'] == ('CONFIRMED' if confirmed else 'UNCONFIRMED')
    assert result['reason_code'] == 'ZINIAO_DEVICE_UNBOUND'
    assert '104' in result['message']
    assert all(not site['attempted'] for site in result['marketplaces'])
    if confirmed:
        assert begin(service.setup_records, sid, ['DE'])['lifecycle'] == 'QUEUED'
    else:
        with pytest.raises(SetupConflict):
            begin(service.setup_records, sid, ['DE'])


def stuck(repo, sid):
    row = begin(repo, sid, ['US'])
    repo.finish(row['probe_id'], outcome='FAILED', cleanup='UNCONFIRMED')
    return row['probe_id']


async def test_explicit_release_without_pid_proof_is_idempotent_and_preserves_failure(records):
    repo, sessions, sid = records
    pid = stuck(repo, sid)
    before = repo.get(sid, pid)
    client = FakeClient([])
    controller = ZiniaoController(client)
    assert (await repo.check_release(sid, pid))['cleanup_state'] == 'UNCONFIRMED'
    result = await repo.release_environment(sid, pid, controller)
    assert result['cleanup_state'] == 'CONFIRMED'
    assert result['outcome'] == before['outcome'] == 'FAILED'
    for key in ['marketplaces', 'finished_at', 'created_at', 'setup_revision', 'reason_code']:
        assert result[key] == before[key]
    await repo.release_environment(sid, pid, controller)
    assert client.actions == [('stop', 'fixture-env')]
    assert begin(repo, sid, ['DE'])['lifecycle'] == 'QUEUED'


@pytest.mark.parametrize('busy', ['funds', 'store', 'launch'])
async def test_release_never_waits_behind_or_interrupts_live_locks(records, busy):
    repo, _, sid = records
    pid = stuck(repo, sid)
    client = FakeClient([])
    controller = ZiniaoController(client)
    lock = await controller.locks.store_lock(str(sid)) if busy == 'store' else getattr(controller.locks, busy)
    await lock.acquire()
    try:
        with pytest.raises(SetupConflict):
            await asyncio.wait_for(repo.release_environment(sid, pid, controller), .5)
        assert lock.locked()
        assert client.actions == []
        assert repo.get(sid, pid)['cleanup_state'] == 'UNCONFIRMED'
    finally:
        lock.release()


@pytest.mark.parametrize('acknowledged', [False, None])
async def test_release_without_acknowledgement_never_unlocks(records, acknowledged):
    repo, _, sid = records
    pid = stuck(repo, sid)
    class Client(FakeClient):
        async def stop_browser(self, *args, **kwargs): return acknowledged
    controller = ZiniaoController(Client([]))
    assert (await repo.release_environment(sid, pid, controller))['cleanup_state'] == 'UNCONFIRMED'
    assert not controller.locks.funds.locked()


async def test_release_timeout_retains_block_and_releases_locks(records):
    repo, _, sid = records
    pid = stuck(repo, sid)
    class Client(FakeClient):
        async def stop_browser(self, *args, **kwargs): await asyncio.Event().wait()
    controller = ZiniaoController(Client([]), config=ZiniaoControllerConfig(setup_release_timeout_seconds=.01))
    with pytest.raises(TimeoutError):
        await repo.release_environment(sid, pid, controller)
    assert repo.get(sid, pid)['cleanup_state'] == 'UNCONFIRMED'
    assert not controller.locks.funds.locked()
    assert not controller.locks.launch.locked()
    assert not (await controller.locks.store_lock(str(sid))).locked()


async def test_release_rejects_wrong_store_changed_environment_and_running_probe(records):
    repo, sessions, sid = records
    row = begin(repo, sid, ['US'])
    controller = ZiniaoController(FakeClient([]))
    with pytest.raises(SetupConflict):
        await repo.release_environment(sid, row['probe_id'], controller)
    repo.finish(row['probe_id'], outcome='FAILED', cleanup='UNCONFIRMED')
    with pytest.raises(LookupError):
        await repo.release_environment(sid+1, row['probe_id'], controller)
    with sessions.begin() as db:
        db.get(Store, sid).selector_value = 'new-environment'
    with pytest.raises(SetupConflict):
        await repo.release_environment(sid, row['probe_id'], controller)
    assert controller.client.actions == []


@pytest.mark.parametrize('status', ['QUEUED', 'RUNNING', 'WAITING_AUTH', 'RECONCILING'])
async def test_release_blocks_pending_or_active_business(records, status):
    repo, sessions, sid = records
    pid = stuck(repo, sid)
    with sessions.begin() as db:
        db.add(Run(store_id=sid, workflow='amazon_disbursement', mode='dry_run', status=status))
    controller = ZiniaoController(FakeClient([]))
    with pytest.raises(SetupConflict):
        await repo.release_environment(sid, pid, controller)
    assert controller.client.actions == []


async def test_unknown_acquisition_error_does_not_invent_release_evidence(tmp_path):
    class FailedController(_Controller):
        @asynccontextmanager
        async def financial_session(self, *args, **kwargs):
            raise RuntimeError('unclassified launch interruption')
            yield
    service, _, sid = _service(tmp_path, FailedController())
    row = await service.detect_store_setup(sid, ['US'])
    result = await _terminal(service, sid, row['probe_id'])
    assert result['cleanup_state'] == 'UNCONFIRMED'


def test_release_endpoint_uses_current_service_and_csrf(tmp_path):
    from fastapi.testclient import TestClient
    from ziniao_automation.config import Settings
    from ziniao_automation.web import create_app
    from ziniao_automation.setup_repository import SetupRepository
    from types import SimpleNamespace
    app = create_app(Settings(project_root=tmp_path, data_dir=tmp_path/'data', testing=True))
    with TestClient(app) as client:
        client.get('/')
        with app.state.sessions.begin() as db:
            store = Store(name='Fixture', selector_type='oauth', selector_value='fixture-env', raw_profile={'platform_id':'1'})
            db.add(store);db.flush();sid=store.id
        repo = SetupRepository(app.state.sessions)
        pid = stuck(repo, sid)
        controller = ZiniaoController(FakeClient([]))
        async def idle(store_id): return False
        service = app.state.automation_service
        app.state.automation_service = SimpleNamespace(has_active_store_setup=idle, ziniao_controller=controller)
        try:
            url = f'/api/stores/{sid}/store-setup-probes/{pid}/release-environment'
            assert client.post(url, json={}).status_code == 403
            result = client.post(url, json={}, headers={'X-CSRF-Token':client.cookies.get('ziniao_csrf')})
            assert result.status_code == 200, result.text
            assert result.json()['cleanup_state'] == 'CONFIRMED'
            assert result.json()['outcome'] == 'FAILED'
        finally:
            app.state.automation_service = service
