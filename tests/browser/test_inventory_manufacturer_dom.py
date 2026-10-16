"""Offline manufacturer association, separate from responsible-person records."""
from dataclasses import replace
import pytest
from test_inventory_compliance_dom import page, renderer, BROWSER, RUN, DE, install
from ziniao_automation.workflows.amazon_inventory_gpsr.config import MANUFACTURER, RESPONSIBLE_PERSON
from ziniao_automation.workflows.amazon_inventory_gpsr.page import InventoryContractError, ManufacturerSelectionError

pytestmark = [pytest.mark.asyncio(loop_scope="module"),
              pytest.mark.skipif(not BROWSER, reason="Requires offline fixture renderer")]

@pytest.mark.parametrize("traditional", [False, True])
@pytest.mark.parametrize("count,selected,invalid,disabled_first,invalid_first,status,chosen", [
 (1,False,False,False,False,"eligible",0),
 (3,False,False,False,False,"eligible",0),
 (3,False,False,True,False,"eligible",1),
 (3,False,False,False,True,"eligible",1),
 (1,True,False,False,False,"already_linked",None),
 (0,False,False,False,False,"no_records",None),
 (2,False,True,False,False,"invalid_record",None),
])
async def test_manufacturer_selects_first_usable_only_after_inspection(
    page,traditional,count,selected,invalid,disabled_first,invalid_first,status,chosen):
    adapter,items=await install(page,manufacturerOnly=True,traditional=traditional,katRadio=True,
        count=count,manufacturerSelected=selected,invalid=invalid,disabledFirst=disabled_first,
        invalidFirst=invalid_first,personReceiptStyle="responsible")
    item=replace(items[0],requirement_type=MANUFACTURER)
    result=await adapter.inspect_item(page,RUN,DE,item)
    assert result.status==status
    assert not any(x.startswith(("select-","send-","save-")) for x in await page.evaluate("window.complianceClicks"))
    if status!="eligible": return
    await adapter.prepare_submission(page,RUN,DE,item)
    assert await page.evaluate("Array.from(document.querySelectorAll('input')).findIndex(e=>e.checked)")==chosen
    assert not await adapter.confirm_submission(page,RUN,DE,item) # choice is not a receipt
    await adapter.submit_once(page,RUN,DE,item)
    assert await adapter.confirm_submission(page,RUN,DE,item)
    assert not await adapter.confirm_submission(page,RUN,DE,replace(item,requirement_type=RESPONSIBLE_PERSON))
    with pytest.raises(InventoryContractError): await adapter.submit_once(page,RUN,DE,item)
    assert (await page.evaluate("window.complianceClicks")).count("send-manufacturer")==1

async def test_person_preparation_never_authorizes_manufacturer_submission(page):
    adapter,items=await install(page,manufacturerOnly=True,katRadio=True)
    person=replace(items[0],requirement_type=RESPONSIBLE_PERSON)
    assert (await adapter.inspect_item(page,RUN,DE,person)).status=="eligible"
    await adapter.prepare_submission(page,RUN,DE,person)
    with pytest.raises(InventoryContractError):
        await adapter.submit_once(page,RUN,DE,replace(person,requirement_type=MANUFACTURER))
    assert not any(x.startswith("send-") for x in await page.evaluate("window.complianceClicks"))

async def test_all_three_requirements_confirm_independently_and_continue(page):
    from test_inventory_gpsr_dom import render_observed_attestation_receipt
    adapter,items=await install(page,includeManufacturer=True,katRadio=True,personReceiptStyle="responsible")
    for kind in ("safety_attestation",RESPONSIBLE_PERSON,MANUFACTURER):
        item=replace(items[0],requirement_type=kind)
        assert (await adapter.inspect_item(page,RUN,DE,item)).status=="eligible"
        await adapter.prepare_submission(page,RUN,DE,item)
        await adapter.submit_once(page,RUN,DE,item)
        if kind=="safety_attestation": await render_observed_attestation_receipt(page)
        assert await adapter.confirm_submission(page,RUN,DE,item)
    actions=await page.evaluate("window.complianceClicks")
    for action in ("save-safety","send-person","send-manufacturer"): assert actions.count(action)==1
    assert (await adapter.inspect_item(page,RUN,DE,replace(items[1],requirement_type=MANUFACTURER))).status=="eligible"

async def test_manufacturer_preparation_failure_has_no_send(page):
    adapter,items=await install(page,manufacturerOnly=True,katRadio=True,blockedSelection=True)
    item=replace(items[0],requirement_type=MANUFACTURER)
    assert (await adapter.inspect_item(page,RUN,DE,item)).status=="eligible"
    with pytest.raises(ManufacturerSelectionError): await adapter.prepare_submission(page,RUN,DE,item)
    assert not any(x.startswith("send-") for x in await page.evaluate("window.complianceClicks"))


async def test_manufacturer_uncertain_checked_form_only_rereads(page):
    from test_inventory_gpsr_dom import POLICY
    from ziniao_automation.workflows.amazon_inventory_gpsr.page import AmazonInventoryGpsrPage
    adapter,items=await install(page,manufacturerOnly=True,katRadio=True)
    item=replace(items[0],requirement_type=MANUFACTURER)
    assert (await adapter.inspect_item(page,RUN,DE,item)).status=="eligible"
    await adapter.prepare_submission(page,RUN,DE,item)
    await page.evaluate("window.noPersonReceipt=true")
    await adapter.submit_once(page,RUN,DE,item)
    restarted=AmazonInventoryGpsrPage(policy=POLICY)
    await restarted.open_inventory(page,RUN,DE)
    assert not await restarted.reconcile_item(page,RUN,DE,item)
    assert (await page.evaluate("window.complianceClicks")).count("send-manufacturer")==1

async def test_manufacturer_final_dispatch_rechecks_requirement_heading(page):
    from ziniao_automation.workflows.amazon_inventory_gpsr.page import _PERSON_SAVE
    adapter,items=await install(page,manufacturerOnly=True,katRadio=True)
    item=replace(items[0],requirement_type=MANUFACTURER)
    assert (await adapter.inspect_item(page,RUN,DE,item)).status=="eligible"
    await adapter.prepare_submission(page,RUN,DE,item)
    original=page.evaluate
    async def swap(script,arg=None):
        if script==_PERSON_SAVE:
            await original("document.querySelector('kat-panel h3').textContent='负责人信息'")
        return await original(script,arg)
    page.evaluate=swap
    with pytest.raises(InventoryContractError):await adapter.submit_once(page,RUN,DE,item)
    assert not any(x.startswith("send-") for x in await original("window.complianceClicks"))


@pytest.mark.parametrize("traditional", [False, True])
async def test_only_remaining_manufacturer_proves_person_requirement_absent(page, traditional):
    adapter, items = await install(page, manufacturerOnly=True, traditional=traditional)
    await page.evaluate("""() => {
        document.querySelectorAll('[data-sku] kat-link').forEach(entry => {
            const open = entry.onclick;
            entry.onclick = () => { open(); document.querySelector('kat-panel .violation').remove(); };
        });
    }""")
    person = replace(items[0], requirement_type=RESPONSIBLE_PERSON)
    result = await adapter.inspect_item(page, RUN, DE, person)
    assert result.status == "not_applicable" and result.requirements_complete
    actions = await page.evaluate("window.complianceClicks")
    assert actions.count("open:SKU-GPSR") == 1
    assert not any(a.startswith(("select-", "send-", "save-")) for a in actions)
    assert not await adapter.reconcile_item(page, RUN, DE, person)  # absence is not a receipt


async def test_other_form_alone_is_not_a_complete_requirements_check(page):
    adapter, items = await install(page, manufacturerOnly=True)
    manufacturer = replace(items[0], requirement_type=MANUFACTURER)
    assert (await adapter.inspect_item(page, RUN, DE, manufacturer)).status == "eligible"
    state = await adapter._panel_snapshot(page, RUN, DE)
    result = await adapter._inspect_loaded_panel(page, RUN, DE,
        replace(items[0], requirement_type=RESPONSIBLE_PERSON), state)
    assert result.status == "not_applicable" and not result.requirements_complete
