"""Independent per-requirement barriers and legacy configuration semantics."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from .test_inventory_gpsr_workflow import harness, Adapter, items, execute
from ziniao_automation.models import Run, InventoryGpsrRequirement, StoreMarketplace
from ziniao_automation.workflows.amazon_inventory_gpsr.definition import AmazonInventoryGpsrConfig
from ziniao_automation.workflows.amazon_inventory_gpsr.config import REQUIREMENT_TYPES, RESPONSIBLE_PERSON, INVENTORY_MARKETPLACES
from ziniao_automation.workflows.amazon_inventory_gpsr.store import InventorySubmissionBlocked
from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import InventoryResultUncertain
from ziniao_automation.workflows.types import RunMode, MarketplaceRef
from ziniao_automation.notifications.database import read_inventory_gpsr_counts


def dual_run(h, mode=RunMode.AUTO, run_id='dual'):
    run=h.new_run(run_id,mode)
    config={'marketplace_codes':['DE'],'processing_types':['safety_attestation','responsible_person']}
    with h.factory() as s:
        s.get(Run,run.id).workflow_config=config;s.commit()
    return replace(run,workflow_config=config)


class DualAdapter(Adapter):
    def __init__(self):
        super().__init__(items(1));self.actions=[];self.person_result='eligible'
    async def inspect_item(self,page,run,marketplace,item):
        return SimpleNamespace(status=self.person_result if item.requirement_type==RESPONSIBLE_PERSON else 'eligible',
                               reason='',page=page,initial_selected=self.person_result=='already_linked' if item.requirement_type==RESPONSIBLE_PERSON else None)
    async def submit_once(self,page,run,marketplace,item):
        self.actions.append((marketplace.code,item.sku,item.requirement_type))
    async def reconcile_item(self,page,run,marketplace,item):
        self.reconciled.append(item.requirement_type);return False


async def test_dual_requirements_two_barriers_one_candidate(harness):
    run=dual_run(harness);adapter=DualAdapter()
    await execute(harness,adapter,run)
    counts=await harness.ledger.summary(run.id)
    assert counts['scanned']==1 and counts['requirements']==2
    assert counts['safety_confirmed']==counts['responsible_confirmed']==1
    assert len(adapter.actions)==2 and len(set(adapter.actions))==2
    with harness.factory() as s:
        safe=read_inventory_gpsr_counts(s,run.id)
        assert safe.scanned==1 and safe.requirements==2 and safe.responsible_confirmed==1


@pytest.mark.parametrize('state,key',[('already_linked','already_linked'),('multiple_records','multiple_records'),
                                    ('no_records','no_records'),('invalid_record','invalid_record')])
async def test_person_skips_never_submit_and_dont_hide_safety(harness,state,key):
    run=dual_run(harness);adapter=DualAdapter();adapter.person_result=state
    await execute(harness,adapter,run)
    assert len(adapter.actions)==1 and adapter.actions[0][2]=='safety_attestation'
    assert (await harness.ledger.summary(run.id))[key]==1


async def test_dry_run_both_zero_writes(harness):
    run=dual_run(harness,RunMode.DRY_RUN);adapter=DualAdapter()
    await execute(harness,adapter,run)
    assert not adapter.actions
    assert all(r.intent_at is None for r in await harness.ledger.list_items(run.id))


async def test_person_armed_crash_restarts_receipt_only(harness):
    run=dual_run(harness);adapter=DualAdapter();flow=harness.workflow(adapter)
    await flow.plan(run,object())
    records=await harness.ledger.list_items(run.id)
    person=next(r for r in records if r.item.requirement_type==RESPONSIBLE_PERSON)
    await harness.ledger.mark(person.id,'ELIGIBLE',eligible=True)
    await harness.ledger.arm(person.id)
    with pytest.raises(InventoryResultUncertain):await flow.plan(run,object())
    assert adapter.actions==[] and adapter.reconciled==[RESPONSIBLE_PERSON]
    assert (await harness.ledger.get(person.id)).state=='UNCERTAIN'


async def test_history_is_scoped_by_type_and_marketplace(harness):
    run=dual_run(harness);adapter=DualAdapter();flow=harness.workflow(adapter)
    await flow.plan(run,object())
    records=await harness.ledger.list_items(run.id)
    for r in records:
        await harness.ledger.mark(r.id,'ELIGIBLE',eligible=True)
        await harness.ledger.arm(r.id)
    assert len(records)==2  # same SKU, independent requirements can each own intent
    with pytest.raises(InventorySubmissionBlocked):await harness.ledger.arm(records[0].id)


@pytest.mark.parametrize('code',['IT','FR','ES','PL'])
async def test_registered_eu_site_runs_without_payment_state(harness,code):
    run=dual_run(harness,RunMode.DRY_RUN)
    with harness.factory() as s:
        market=StoreMarketplace(store_id=int(run.store.id),code=code,domain=INVENTORY_MARKETPLACES[code],currency='PLN' if code=='PL' else 'EUR',enabled=True)
        s.add(market);s.flush();mid=market.id
        config={'marketplace_codes':[code],'processing_types':['safety_attestation','responsible_person']}
        s.get(Run,run.id).workflow_config=config;s.commit()
    run=replace(run,marketplaces=(MarketplaceRef(str(mid),code,INVENTORY_MARKETPLACES[code],'EUR'),),workflow_config=config)
    await execute(harness,DualAdapter(),run)
    assert (await harness.ledger.summary(run.id))['requirements']==2


def test_old_config_never_expands_to_responsible_person():
    assert AmazonInventoryGpsrConfig.model_validate({'marketplace_codes':['DE']}).processing_types==('safety_attestation',)
    with pytest.raises(ValueError):AmazonInventoryGpsrConfig.model_validate({'processing_types':[]})
    with pytest.raises(ValueError):AmazonInventoryGpsrConfig.model_validate({'processing_types':['unknown_requirement']})


def add_site(h, run, code):
    with h.factory() as s:
        market = StoreMarketplace(store_id=int(run.store.id), code=code,
            domain=INVENTORY_MARKETPLACES[code], currency='EUR', enabled=True)
        s.add(market); s.flush(); mid = market.id
        config = {**run.workflow_config, 'marketplace_codes': [*run.workflow_config['marketplace_codes'], code]}
        s.get(Run, run.id).workflow_config = config
        s.commit()
    return replace(run, workflow_config=config, marketplaces=(*run.marketplaces,
        MarketplaceRef(str(mid), code, INVENTORY_MARKETPLACES[code], 'EUR')))


async def test_same_sku_on_two_sites_has_independent_submission_intents(harness):
    run = add_site(harness, dual_run(harness), 'IT')
    adapter = DualAdapter()
    await execute(harness, adapter, run)
    assert len(adapter.actions) == len(set(adapter.actions)) == 4
    counts = await harness.ledger.summary(run.id)
    assert counts['scanned'] == 2 and counts['requirements'] == counts['confirmed'] == 4


async def test_uncertain_second_requirement_stops_all_later_sites(harness):
    run = add_site(harness, dual_run(harness), 'IT')
    class UncertainPerson(DualAdapter):
        async def confirm_submission(self, page, run, market, item):
            return item.requirement_type != RESPONSIBLE_PERSON
    adapter = UncertainPerson()
    await execute(harness, adapter, run)
    assert len(adapter.actions) == 2
    assert all(action[0] == 'DE' for action in adapter.actions)
    rows = await harness.ledger.list_items(run.id, 'IT')
    assert all(r.state == 'PENDING' and r.intent_at is None for r in rows)


async def test_selection_failure_is_pre_intent_and_other_products_continue(harness):
    from ziniao_automation.workflows.amazon_inventory_gpsr.page import PersonSelectionError
    class SelectFailure(DualAdapter):
        def __init__(self):
            super().__init__();self.candidates=items(2);self.prepared=[]
        async def scan_candidates(self,*args):return self.candidates
        async def prepare_submission(self,page,run,market,item):
            self.prepared.append((item.sku,item.requirement_type))
            if item.requirement_type==RESPONSIBLE_PERSON and item.sku==self.candidates[-1].sku:
                raise PersonSelectionError('fixture selection did not complete')
    run=dual_run(harness);adapter=SelectFailure()
    await execute(harness,adapter,run)
    records=await harness.ledger.list_items(run.id)
    failed=[r for r in records if r.state=='FAILED']
    assert len(failed)==1 and failed[0].intent_at is None
    assert failed[0].reason=='responsible_person_selection_failed'
    assert len(adapter.actions)==3
    assert all(r.state!='UNCERTAIN' for r in records)


@pytest.mark.parametrize("mode", [RunMode.AUTO, RunMode.DRY_RUN])
async def test_preparation_completes_before_arm_and_dry_run_never_selects(harness, mode):
    class Preparing(DualAdapter):
        def __init__(self):super().__init__();self.prepared=[]
        async def prepare_submission(self,page,run,market,item):
            row=next(r for r in await harness.ledger.list_items(run.id) if r.item.requirement_type==item.requirement_type)
            assert row.state=='ELIGIBLE' and row.intent_at is None
            self.prepared.append(item.requirement_type)
        async def submit_once(self,page,run,market,item):
            row=next(r for r in await harness.ledger.list_items(run.id) if r.item.requirement_type==item.requirement_type)
            assert item.requirement_type in self.prepared and row.state=='SUBMITTING' and row.intent_at
            await super().submit_once(page,run,market,item)
    adapter=Preparing();await execute(harness,adapter,dual_run(harness,mode))
    if mode is RunMode.AUTO:
        assert len(adapter.prepared)==2 and len(adapter.actions)==2
    else:
        assert adapter.prepared==adapter.actions==[]
