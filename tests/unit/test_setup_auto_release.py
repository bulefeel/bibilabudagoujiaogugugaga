"""Recover stale setup leases before continuing a batch, without replaying work."""
import pytest
from sqlalchemy import select, func

from tests.unit.test_setup_persistence import records, begin
from tests.unit.test_setup_release_recovery import stuck
from tests.unit.test_store_setup_runtime import _service, _Controller, _terminal
from tests.unit.test_ziniao_controller import FakeClient, SequencedHealth
from ziniao_automation.models import Store, StoreSetupProbe, Run
from ziniao_automation.setup_errors import SetupConflict
from ziniao_automation.workflows.runtime import AutomationService
from ziniao_automation.ziniao.controller import ZiniaoController, ZiniaoControllerConfig
from ziniao_automation.ziniao.errors import ZiniaoCredentialError
from ziniao_automation.ziniao.models import ProfileSelector


def service_for(sessions, controller):
    return AutomationService(engine=object(), run_loader=lambda _: None,
                             ziniao_controller=controller, session_factory=sessions)


def test_restart_before_browser_acquisition_never_creates_a_release_barrier(records):
    repo, _, sid = records
    pid = begin(repo, sid)['probe_id']
    assert repo.recover() == 1
    result = repo.get(sid, pid)
    assert result['outcome'] == 'INTERRUPTED'
    assert result['cleanup_state'] == 'CONFIRMED'
    assert repo.pending_releases() == []
    assert begin(repo, sid)['lifecycle'] == 'QUEUED'


@pytest.mark.parametrize('previous_start', [False, True])
async def test_credential_failure_preserves_actual_launch_history(previous_start):
    class Client(FakeClient):
        def __init__(self):
            super().__init__([9001]); self.attempts = 0
        async def start_browser(self, selector):
            self.attempts += 1
            if previous_start and self.attempts == 1:
                return await super().start_browser(selector)
            raise ZiniaoCredentialError('fixture credential read failed before HTTP')
        async def stop_browser(self, selector, **kwargs):
            await super().stop_browser(selector, **kwargs)
            return False
    client = Client()
    controller = ZiniaoController(client, process_running=lambda:True,
        health_checker=SequencedHealth([False]),
        config=ZiniaoControllerConfig(start_retry_delay_seconds=.001))
    with pytest.raises(ZiniaoCredentialError) as caught:
        await controller.open_store(ProfileSelector('oauth','fixture-env'), '1')
    assert caught.value.cleanup_confirmed is (not previous_start)
    assert client.actions == ([('start','fixture-env'),('stop','fixture-env')] if previous_start else [])


async def test_error_before_session_does_not_claim_a_browser(tmp_path, monkeypatch):
    service, _, sid = _service(tmp_path, _Controller())
    async def invalid_plan(**kwargs):
        raise ValueError('fixture validation failed before acquiring a session')
    monkeypatch.setattr(service, '_run_store_setup_probe', invalid_plan)
    row = await service.detect_store_setup(sid, ['US'])
    result = await _terminal(service, sid, row['probe_id'])
    assert result['outcome'] == 'FAILED' and result['cleanup_state'] == 'CONFIRMED'
    assert service.ziniao_controller.opens == 0
    assert not service.setup_records.pending_releases()


async def test_new_store_request_releases_old_store_without_replaying_it(records, monkeypatch):
    repo, sessions, sid = records
    pid = stuck(repo, sid)
    original = repo.get(sid, pid)
    with sessions.begin() as db:
        other = Store(name='Next fixture', selector_type='oauth', selector_value='next-env', raw_profile={'platform_id':'1'})
        db.add(other); db.flush(); next_id = other.id
    client = FakeClient([])
    client.preflight_credentials = lambda: None
    service = service_for(sessions, ZiniaoController(client))
    processed = []
    async def observe(**kwargs):
        processed.append(kwargs['store_id'])
        return {'status':'FAILED'}
    monkeypatch.setattr(service, '_run_store_setup_probe', observe)
    row = await service.detect_store_setup(next_id, ['DE'])
    await _terminal(service, next_id, row['probe_id'])
    assert processed == [next_id]
    assert client.actions == [('stop', 'fixture-env')]
    after = repo.get(sid, pid)
    assert after['cleanup_state'] == 'CONFIRMED'
    for key in ('outcome', 'marketplaces', 'reason_code', 'created_at', 'finished_at'):
        assert after[key] == original[key]


async def test_failed_auto_release_returns_the_actual_blocker_without_new_probe(records):
    repo, sessions, sid = records
    pid = stuck(repo, sid)
    class Client(FakeClient):
        async def stop_browser(self, selector, **kwargs):
            await super().stop_browser(selector, **kwargs)
            return False
    client = Client([])
    service = service_for(sessions, ZiniaoController(client))
    with pytest.raises(SetupConflict) as caught:
        await service.detect_store_setup(sid, ['DE'])
    assert caught.value.code == 'SETUP_RESOURCE_UNCONFIRMED'
    assert caught.value.store_id == sid and caught.value.probe_id == pid
    assert client.actions == [('stop', 'fixture-env')]
    assert repo.get(sid, pid)['cleanup_state'] == 'UNCONFIRMED'
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(StoreSetupProbe)) == 1


async def test_exited_process_evidence_avoids_an_unnecessary_stop(records, monkeypatch):
    repo, sessions, sid = records
    row = begin(repo, sid)
    repo.save_resource_proof(row['probe_id'], {'pid':123, 'created':'12345'})
    repo.finish(row['probe_id'], outcome='FAILED', cleanup='UNCONFIRMED')
    async def exited(proof): return True
    monkeypatch.setattr('ziniao_automation.setup_resources.process_released', exited)
    client = FakeClient([])
    await service_for(sessions, ZiniaoController(client)).recover_setup_resources()
    assert not client.actions and not repo.pending_releases()


@pytest.mark.parametrize('status', ['QUEUED', 'RUNNING', 'WAITING_AUTH', 'RECONCILING'])
async def test_auto_release_preserves_active_business(records, status):
    repo, sessions, sid = records
    pid = stuck(repo, sid)
    with sessions.begin() as db:
        db.add(Run(store_id=sid, workflow='amazon_disbursement', mode='dry_run', status=status))
    client = FakeClient([])
    with pytest.raises(SetupConflict):
        await service_for(sessions, ZiniaoController(client)).recover_setup_resources()
    assert not client.actions
    assert repo.get(sid, pid)['cleanup_state'] == 'UNCONFIRMED'


async def test_release_recovers_all_terminal_records_of_same_environment(records):
    repo, sessions, sid = records
    older = begin(repo, sid)['probe_id']
    repo.finish(older, outcome='FAILED', cleanup='CONFIRMED')
    newer = stuck(repo, sid)
    with sessions.begin() as db:
        db.get(StoreSetupProbe, older).cleanup_state = 'UNCONFIRMED'
    client = FakeClient([])
    await service_for(sessions, ZiniaoController(client)).recover_setup_resources()
    assert client.actions == [('stop', 'fixture-env')]
    assert all(repo.get(sid, pid)['cleanup_state'] == 'CONFIRMED' for pid in (older, newer))


def test_summary_exposes_an_older_unreleased_record(records):
    repo, sessions, sid = records
    older = begin(repo, sid)['probe_id']
    repo.finish(older, outcome='FAILED', cleanup='CONFIRMED')
    newer = begin(repo, sid)['probe_id']
    repo.finish(newer, outcome='FAILED', cleanup='CONFIRMED')
    with sessions.begin() as db:
        db.get(StoreSetupProbe, older).cleanup_state = 'UNCONFIRMED'
    summary = repo.summary(sid)
    assert summary['latest_probe']['probe_id'] == newer
    assert summary['release_probe']['probe_id'] == older


def test_batch_reset_recovers_only_selected_stale_environment(tmp_path):
    from fastapi.testclient import TestClient
    from ziniao_automation.config import Settings
    from ziniao_automation.web import create_app
    from ziniao_automation.setup_repository import SetupRepository
    app = create_app(Settings(project_root=tmp_path, data_dir=tmp_path/'data', testing=True))
    with TestClient(app) as browser:
        browser.get('/')
        with app.state.sessions.begin() as db:
            store = Store(name='Fixture', selector_type='oauth', selector_value='fixture-env', raw_profile={'platform_id':'1'})
            db.add(store); db.flush(); sid = store.id
        repo = SetupRepository(app.state.sessions)
        pid = stuck(repo, sid)
        before = repo.get(sid, pid)
        controller = ZiniaoController(FakeClient([]))
        original = app.state.automation_service
        app.state.automation_service = service_for(app.state.sessions, controller)
        try:
            response = browser.post('/api/stores/setup/reset-batch', json={'stores':[
                {'store_id':sid, 'expected_setup_revision':repo.summary(sid)['setup_revision']} ]},
                headers={'X-CSRF-Token':browser.cookies.get('ziniao_csrf')})
            assert response.status_code == 200, response.text
            assert response.json()['reset_count'] == 1
            assert controller.client.actions == [('stop', 'fixture-env')]
            after = repo.get(sid, pid)
            assert after['cleanup_state'] == 'CONFIRMED'
            assert after['outcome'] == before['outcome']
        finally:
            app.state.automation_service = original
