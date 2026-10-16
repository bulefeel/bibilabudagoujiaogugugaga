"""Three independent requirements, immutable old scope, no financial state."""
from dataclasses import replace
import pytest
from ziniao_automation.models import Run
from ziniao_automation.workflows.types import RunMode
from ziniao_automation.workflows.amazon_inventory_gpsr.config import REQUIREMENT_TYPES, MANUFACTURER
from ziniao_automation.workflows.amazon_inventory_gpsr.definition import AmazonInventoryGpsrConfig
from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import InventoryResultUncertain
from ziniao_automation.notifications.database import read_inventory_gpsr_counts
from .test_inventory_gpsr_workflow import harness, execute
from .test_inventory_compliance import dual_run, DualAdapter, add_site

def triple(h,mode=RunMode.AUTO,run_id="triple"):
    run=dual_run(h,mode,run_id)
    config={**run.workflow_config,"processing_types":list(REQUIREMENT_TYPES)}
    with h.factory() as s:
        s.get(Run,run.id).workflow_config=config;s.commit()
    return replace(run,workflow_config=config)

@pytest.mark.parametrize("mode", [RunMode.AUTO,RunMode.DRY_RUN])
async def test_three_requirements_across_all_four_sites(harness,mode):
    run=triple(harness,mode)
    for code in ("IT","FR","ES","PL"):run=add_site(harness,run,code)
    adapter=DualAdapter()
    await execute(harness,adapter,run)
    counts=await harness.ledger.summary(run.id)
    assert counts["scanned"]==5 and counts["requirements"]==15
    assert len(adapter.actions)==(15 if mode is RunMode.AUTO else 0)
    assert counts["manufacturer_confirmed"]==(5 if mode is RunMode.AUTO else 0)
    with harness.factory() as s:
        safe=read_inventory_gpsr_counts(s,run.id)
        assert safe.manufacturer_confirmed==counts["manufacturer_confirmed"]

async def test_manufacturer_crash_only_reconciles_no_second_send(harness):
    run=triple(harness);adapter=DualAdapter();flow=harness.workflow(adapter)
    await flow.plan(run,object())
    record=next(r for r in await harness.ledger.list_items(run.id) if r.item.requirement_type==MANUFACTURER)
    await harness.ledger.mark(record.id,"ELIGIBLE",eligible=True)
    await harness.ledger.arm(record.id)
    with pytest.raises(InventoryResultUncertain):await flow.plan(run,object())
    assert adapter.actions==[] and adapter.reconciled==[MANUFACTURER]
    assert (await harness.ledger.get(record.id)).state=="UNCERTAIN"

async def test_new_type_never_expands_old_snapshot(harness):
    run=dual_run(harness)
    await execute(harness,DualAdapter(),run)
    assert {r.item.requirement_type for r in await harness.ledger.list_items(run.id)}=={"safety_attestation","responsible_person"}
    assert AmazonInventoryGpsrConfig.model_validate({"marketplace_codes":["DE"]}).processing_types==("safety_attestation",)

async def test_manufacturer_selection_failure_is_pre_intent(harness):
    from ziniao_automation.workflows.amazon_inventory_gpsr.page import ManufacturerSelectionError
    class Broken(DualAdapter):
        async def prepare_submission(self,page,run,market,item):
            if item.requirement_type==MANUFACTURER:raise ManufacturerSelectionError("fixture")
    run=triple(harness);adapter=Broken()
    await execute(harness,adapter,run)
    record=next(r for r in await harness.ledger.list_items(run.id) if r.item.requirement_type==MANUFACTURER)
    assert record.state=="FAILED" and record.intent_at is None
    assert record.reason=="manufacturer_selection_failed"
    assert len(adapter.actions)==2
