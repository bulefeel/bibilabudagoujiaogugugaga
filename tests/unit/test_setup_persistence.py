"""Persistence, recovery, API contract, revision and cleanup boundary tests."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import json

import pytest
from sqlalchemy import select, text, func
from pydantic import ValidationError
from fastapi.testclient import TestClient

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import Store, StoreMarketplace, StoreSetupProbe, StoreSetupSiteResult, Run, OperationGuard
from ziniao_automation.repositories import StoreRepository, ConflictError
from ziniao_automation.setup_repository import SetupRepository, SetupConflict
from ziniao_automation.schemas import StoreSetupInput
from ziniao_automation.ziniao.models import ProfileSelector


@pytest.fixture
def records(tmp_path):
    engine=create_sqlite_engine(Settings(project_root=tmp_path,data_dir=tmp_path/'data',testing=True))
    init_database(engine)
    sessions=make_session_factory(engine)
    with sessions.begin() as db:
        store=Store(name='Fixture',selector_type='oauth',selector_value='fixture-env',raw_profile={'platform_id':'1'})
        db.add(store);db.flush();sid=store.id
    yield SetupRepository(sessions),sessions,sid
    engine.dispose()


def begin(repo,sid,codes=('CA','UK','AU'),**kwargs):
    return repo.begin(sid,list(codes),kind='unified',seconds=1800,policy_version='onboarding-policy-v1',**kwargs)


def passed(repo,pid,code):
    repo.progress(pid,sites=[{'code':code,'status':'CHECKING'}],lifecycle='CHECKING',phase='CHECK_SITE')
    repo.progress(pid,sites=[{'code':code,'status':'SUCCEEDED','identity_check':'PASSED','access_check':'PASSED','payment_capability':'UNKNOWN'}])


def failed(repo,pid,code):
    repo.progress(pid,sites=[{'code':code,'status':'CHECKING'}],lifecycle='CHECKING',phase='CHECK_SITE')
    repo.progress(pid,sites=[{'code':code,'status':'FAILED','reason_code':'INACTIVE_ACCOUNT'}])


def test_rows_exist_before_launch_and_projection_has_no_sensitive_snapshot(records):
    repo,sessions,sid=records
    row=begin(repo,sid)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(StoreSetupSiteResult))==3
        assert db.scalar(select(func.count()).select_from(Run))==0
        assert db.scalar(select(func.count()).select_from(OperationGuard))==0
    body=json.dumps(row)
    for secret in ('fixture-env','config_snapshot','request_fingerprint','resource_snapshot'):
        assert secret not in body
    assert row['lifecycle']=='QUEUED' and row['outcome'] is None
    assert all(not s['attempted'] and s['outcome'] is None for s in row['marketplaces'])


def test_idempotency_survives_own_binding_and_completion(records):
    repo,sessions,sid=records;rid=str(uuid4())
    row=begin(repo,sid,request_id=rid,expected_revision=0);pid=row['probe_id']
    before=row['setup_revision']
    repo.bind_identity(pid,'SELLER',ProfileSelector('oauth','fixture-env'),'CA')
    assert repo.get(sid,pid)['setup_revision']>before
    assert repo.existing(sid,['CA','UK','AU'],request_id=rid,expected_revision=0)['probe_id']==pid
    for code in ('CA','UK','AU'):passed(repo,pid,code)
    repo.finish(pid,outcome='SUCCEEDED',cleanup='CONFIRMED')
    result=repo.existing(sid,['CA','UK','AU'],request_id=rid,expected_revision=0)
    assert result['outcome']=='SUCCEEDED' and result['reused']
    with pytest.raises(SetupConflict):repo.existing(sid,['CA'],request_id=rid)
    with sessions() as db: assert db.scalar(select(func.count()).select_from(StoreSetupProbe))==1


def test_same_active_action_remembers_an_alias_after_terminal(records):
    repo,_,sid=records;first=str(uuid4());second=str(uuid4())
    row=begin(repo,sid,['CA'],request_id=first)
    assert repo.existing(sid,['CA'],request_id=second)['probe_id']==row['probe_id']
    repo.finish(row['probe_id'],outcome='CANCELLED',cleanup='CONFIRMED',reason='SETUP_CANCELLED')
    assert repo.existing(sid,['CA'],request_id=second)['probe_id']==row['probe_id']


def test_concurrent_same_request_creates_one_probe(records):
    repo,sessions,sid=records;rid=str(uuid4())
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:begin(repo,sid,['CA'],request_id=rid),range(2)))
    assert results[0]['probe_id']==results[1]['probe_id']
    with sessions() as db:assert db.scalar(select(func.count()).select_from(StoreSetupProbe))==1


def test_different_active_scope_conflicts_without_changing_config(records):
    repo,sessions,sid=records;row=begin(repo,sid,['CA'])
    with pytest.raises(SetupConflict):repo.existing(sid,['UK'])
    with pytest.raises(SetupConflict):begin(repo,sid,['UK'])
    with sessions() as db:assert [m.code for m in db.scalars(select(StoreMarketplace))]==['CA']


def test_repository_blocks_edit_reset_delete_during_probe(records):
    repo,sessions,sid=records;begin(repo,sid,['CA'])
    for mutation in [lambda r:r.update(sid,expected_seller_id='OTHER'),lambda r:r.reset_setup(sid),lambda r:r.replace_marketplaces(sid,[])]:
        with sessions() as db,pytest.raises(ConflictError):mutation(StoreRepository(db))
    with sessions.begin() as db:StoreRepository(db).update(sid,name='Renamed',enabled=False)


def test_revision_changes_only_for_identity_environment_and_sites(records):
    repo,sessions,sid=records
    def rev():return repo.summary(sid)['setup_revision']
    original=rev()
    with sessions.begin() as db:StoreRepository(db).update(sid,name='Renamed',enabled=True)
    assert rev()==original
    with sessions.begin() as db:StoreRepository(db).update(sid,expected_seller_id='SELLER',identity_confirmed=True)
    assert rev()>original;original=rev()
    with sessions.begin() as db:StoreRepository(db).replace_marketplaces(sid,[{'code':'DE','enabled':True}])
    assert rev()>original;original=rev()
    with sessions.begin() as db:StoreRepository(db).replace_marketplaces(sid,[{'code':'DE','enabled':True}])
    assert rev()==original


def test_direct_sql_generation_change_prevents_late_binding(records):
    repo,sessions,sid=records;row=begin(repo,sid,['CA']);pid=row['probe_id']
    with sessions.begin() as db:db.execute(text("UPDATE stores SET selector_value='different' WHERE id=:id"),{'id':sid})
    with pytest.raises(SetupConflict):repo.bind_identity(pid,'OLD',ProfileSelector('oauth','fixture-env'),'CA')
    repo.finish(pid,outcome='FAILED',cleanup='CONFIRMED')
    assert repo.get(sid,pid)['outcome']=='INTERRUPTED'
    with sessions() as db:assert db.get(Store,sid).expected_seller_id is None
    assert repo.summary(sid)['stale']


def test_retry_only_failed_sites_and_summary_merges_previous_pass(records):
    repo,_,sid=records;first=begin(repo,sid);pid=first['probe_id']
    passed(repo,pid,'CA');passed(repo,pid,'UK');failed(repo,pid,'AU')
    repo.finish(pid,outcome='PARTIAL',cleanup='CONFIRMED')
    second=begin(repo,sid,scope='retry_unverified')
    assert [m['code'] for m in second['marketplaces']]==['AU']
    passed(repo,second['probe_id'],'AU');repo.finish(second['probe_id'],outcome='SUCCEEDED',cleanup='CONFIRMED')
    summary=repo.summary(sid)
    assert summary['verification_outcome']=='SUCCEEDED'
    assert all(summary['marketplaces'][c]['outcome']=='PASSED' for c in ('CA','UK','AU'))
    assert begin(repo,sid,scope='retry_unverified')['no_work']


def test_new_failure_does_not_hide_behind_previous_pass(records):
    repo,_,sid=records;first=begin(repo,sid,['CA']);passed(repo,first['probe_id'],'CA');repo.finish(first['probe_id'],outcome='SUCCEEDED',cleanup='CONFIRMED')
    second=begin(repo,sid,['CA']);failed(repo,second['probe_id'],'CA');repo.finish(second['probe_id'],outcome='FAILED',cleanup='CONFIRMED')
    summary=repo.summary(sid)
    assert summary['marketplaces']['CA']['outcome']=='BLOCKED' and summary['last_passed_at']['CA']


@pytest.mark.parametrize('state',['QUEUED','CHECKING','WAITING_AUTH'])
def test_restart_preserves_completed_rows_and_never_replays(records,state):
    repo,sessions,sid=records;row=begin(repo,sid);pid=row['probe_id']
    passed(repo,pid,'CA')
    repo.progress(pid,lifecycle=state,phase='CHECK_SITE')
    fresh=SetupRepository(sessions);assert fresh.recover()==1;assert fresh.recover()==0
    result=fresh.get(sid,pid)
    assert result['status']=='FAILED' and result['outcome']=='INTERRUPTED'
    assert result['cleanup_state']=='UNCONFIRMED'
    assert result['marketplaces'][0]['outcome']=='PASSED'
    assert all(m['outcome']=='NOT_RUN' and not m['attempted'] for m in result['marketplaces'][1:])
    with pytest.raises(SetupConflict):begin(fresh,sid)


def test_waiting_diagnostics_and_terminal_privacy(records):
    repo,_,sid=records;row=begin(repo,sid,['CA']);pid=row['probe_id'];sentinel='SECRET_cookie_token_123456'
    repo.progress(pid,lifecycle='WAITING_AUTH',phase='READ_IDENTITY',diagnostic={'reason_code':'AUTH_INPUT_NOT_READY','stage':'otp_signin','attempts':0,'extra':sentinel})
    waiting=repo.get(sid,pid)
    assert waiting['resume_phase']=='READ_IDENTITY'
    assert waiting['last_diagnostic']['attempts']==0
    repo.finish(pid,outcome='FAILED',cleanup='CONFIRMED',reason=sentinel)
    body=json.dumps(repo.get(sid,pid));assert sentinel not in body


def test_late_callback_cannot_overwrite_terminal(records):
    repo,_,sid=records;row=begin(repo,sid,['CA']);pid=row['probe_id'];repo.finish(pid,outcome='CANCELLED',cleanup='CONFIRMED',reason='SETUP_CANCELLED')
    before=repo.get(sid,pid)
    passed(repo,pid,'CA');repo.finish(pid,outcome='SUCCEEDED',cleanup='UNCONFIRMED')
    assert repo.get(sid,pid)==before


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_can_only_be_cleared_by_matching_process_evidence(records,monkeypatch):
    repo,_,sid=records;row=begin(repo,sid,['CA']);pid=row['probe_id']
    repo.save_resource_proof(pid,{'pid':123,'created':'123456789'})
    repo.finish(pid,outcome='CANCELLED',cleanup='UNCONFIRMED')
    async def alive(proof):return False
    monkeypatch.setattr('ziniao_automation.setup_resources.process_released',alive)
    assert (await repo.check_release(sid,pid))['cleanup_state']=='UNCONFIRMED'
    with pytest.raises(SetupConflict):begin(repo,sid,['CA'])
    async def gone(proof):return True
    monkeypatch.setattr('ziniao_automation.setup_resources.process_released',gone)
    assert (await repo.check_release(sid,pid))['cleanup_state']=='CONFIRMED'
    assert begin(repo,sid,['CA'])['lifecycle']=='QUEUED'


@pytest.mark.parametrize('platform',[{}, {'platform_id':'92','platform_name':'TikTok Shop'}])
def test_non_amazon_never_admitted(records,platform):
    repo,sessions,sid=records
    with sessions.begin() as db:db.get(Store,sid).raw_profile=platform
    with pytest.raises(ValueError):begin(repo,sid,['CA'])
    assert repo.preview([sid],'fill_missing',{})['eligible_count']==0


def test_de_only_is_not_widened_and_preview_is_readonly(records):
    repo,sessions,sid=records
    with sessions.begin() as db:StoreRepository(db).replace_marketplaces(sid,[{'code':'DE','enabled':True}])
    revision=repo.summary(sid)['setup_revision']
    for mode in ('fill_missing','retry_unverified','recheck'):
        preview=repo.preview([sid],mode,{})
        assert preview['eligible_count']==1
        assert preview['targets'][0]['marketplace_codes']==['DE']
    assert repo.summary(sid)['setup_revision']==revision
    with sessions() as db:assert db.scalar(select(func.count()).select_from(StoreSetupProbe))==0


def test_empty_configuration_defaults_are_only_in_preview_until_confirmed(records):
    repo,sessions,sid=records
    preview=repo.preview([sid],'fill_missing',{})
    assert preview['targets'][0]['marketplace_codes']==['CA','UK','AU']
    assert preview['targets'][0]['defaulted']
    with sessions() as db:assert db.scalar(select(func.count()).select_from(StoreMarketplace))==0


@pytest.mark.parametrize('payload',[
    {'marketplace_codes':[]},{'marketplace_codes':['CA','CA']},{'marketplace_codes':['JP']},
    {'marketplace_codes':['CA'],'scope':'unknown'},{'marketplace_codes':['CA'],'script':'injected'},
    {'marketplace_codes':['CA'],'expected_setup_revision':-1},
])
def test_request_validation_is_closed(payload):
    with pytest.raises(ValidationError):StoreSetupInput.model_validate(payload)


def test_identity_only_does_not_invent_sites(records):
    repo,sessions,sid=records
    row=repo.begin(sid,[],kind='identity',seconds=1800,policy_version='v1')
    repo.finish(row['probe_id'],outcome='SUCCEEDED',cleanup='CONFIRMED',identity={'status':'SUCCEEDED','seller_id':'SELLER'})
    result=repo.get(sid,row['probe_id']);assert result['outcome']=='SUCCEEDED' and result['marketplaces']==[]
    with sessions() as db:assert db.get(Store,sid).expected_seller_id is None


def test_delete_cascades_setup_records_only_after_cleanup(records):
    repo,sessions,sid=records;row=begin(repo,sid,['CA']);repo.finish(row['probe_id'],outcome='FAILED',cleanup='CONFIRMED')
    with sessions.begin() as db:db.execute(text('DELETE FROM stores WHERE id=:id'),{'id':sid})
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(StoreSetupProbe))==0
        assert db.scalar(select(func.count()).select_from(StoreSetupSiteResult))==0


def test_api_summary_preview_and_validation_without_browser(tmp_path):
    from ziniao_automation.web import create_app
    app=create_app(Settings(project_root=tmp_path,data_dir=tmp_path/'data',testing=True))
    with TestClient(app) as client:
        client.get('/')
        headers={'X-CSRF-Token':client.cookies.get('ziniao_csrf')}
        with app.state.sessions.begin() as db:
            store=Store(name='Fixture',selector_type='oauth',selector_value='env',raw_profile={'platform_id':'1'})
            db.add(store);db.flush();sid=store.id
        response=client.get(f'/api/stores/{sid}/setup-summary')
        assert response.status_code==200, response.text
        assert response.json()['verification_outcome']=='UNVERIFIED'
        result=client.post('/api/stores/setup-batch/preview',json={'store_ids':[sid],'mode':'fill_missing'},headers=headers)
        assert result.status_code==200 and result.json()['eligible_count']==1
        assert client.post(f'/api/stores/{sid}/detect-store-setup',json={'marketplace_codes':['JP']},headers=headers).status_code==422
        assert client.post('/api/stores/setup-batch/preview',json={'store_ids':[sid,sid]},headers=headers).status_code==422
        assert client.get(f'/api/stores/{sid}/store-setup-probe').status_code==204


@pytest.mark.parametrize("via_existing", [False, True])
def test_same_request_rejects_changed_original_preview_revision(records, via_existing):
    repo, sessions, sid = records
    rid = str(uuid4())
    row = begin(repo, sid, ['CA'], request_id=rid, expected_revision=0)
    repo.bind_identity(row['probe_id'], 'SELLER', ProfileSelector('oauth', 'fixture-env'), 'CA')
    # Own binding changes the live revision, never the original request contract.
    assert begin(repo, sid, ['CA'], request_id=rid, expected_revision=0)['probe_id'] == row['probe_id']
    with pytest.raises(SetupConflict):
        if via_existing:
            repo.existing(sid, ['CA'], request_id=rid, expected_revision=1)
        else:
            begin(repo, sid, ['CA'], request_id=rid, expected_revision=1)


@pytest.mark.parametrize('identity,access', [('NOT_CHECKED','PASSED'), ('PASSED','FAILED'), (None,None)])
def test_site_cannot_persist_pass_without_both_checks(records, identity, access):
    repo, _, sid = records
    row = begin(repo, sid, ['CA'])
    pid = row['probe_id']
    with pytest.raises(ValueError, match='verified identity and access'):
        repo.progress(pid, sites=[{'code':'CA','status':'SUCCEEDED', 'identity_check':identity, 'access_check':access}])
    assert repo.get(sid, pid)['marketplaces'][0]['outcome'] is None


def test_retry_identity_bootstrap_uses_only_current_selected_pass(records):
    repo,sessions,sid=records
    first=begin(repo,sid)
    passed(repo,first['probe_id'],'UK')
    failed(repo,first['probe_id'],'CA');failed(repo,first['probe_id'],'AU')
    repo.finish(first['probe_id'],outcome='PARTIAL',cleanup='CONFIRMED')
    retry=begin(repo,sid,scope='retry_unverified')
    assert repo.identity_bootstrap_codes(retry['probe_id']) == ['UK','CA','AU']
    assert [m['code'] for m in retry['marketplaces']] == ['CA','AU']
    repo.finish(retry['probe_id'],outcome='CANCELLED',cleanup='CONFIRMED')
    with sessions.begin() as db:
        db.execute(text("UPDATE stores SET expected_seller_id='DIFFERENT' WHERE id=:id"),{'id':sid})
    fresh=begin(repo,sid,['CA','AU'],scope='retry_unverified')
    assert repo.identity_bootstrap_codes(fresh['probe_id']) == ['CA','AU']


def test_current_store_facts_survive_site_failure_but_not_reset(records):
    repo,sessions,sid=records
    row=begin(repo,sid,["CA"]);pid=row["probe_id"]
    repo.bind_identity(pid,"BOUND-SELLER",ProfileSelector("oauth","fixture-env"),"CA")
    repo.progress(pid,identity={"status":"SUCCEEDED","seller_id":"BOUND-SELLER","identity_confirmed":True})
    repo.finish(pid,outcome="FAILED",cleanup="CONFIRMED",reason="IDENTITY_PAGE_NOT_READY")
    summary=repo.summary(sid)
    assert summary["expected_seller_id"]=="BOUND-SELLER"
    assert summary["identity_confirmed"] is True
    assert "卖家身份已保存" in summary["latest_probe"]["message"]
    assert "未绑定" not in summary["latest_probe"]["message"]
    with sessions.begin() as db:StoreRepository(db).reset_setup(sid)
    summary=repo.summary(sid)
    assert summary["expected_seller_id"] is None
    assert summary["identity_confirmed"] is False
    assert summary["latest_probe"]["identity"]["seller_id"]=="BOUND-SELLER"
