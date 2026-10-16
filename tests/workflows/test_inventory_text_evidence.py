"""Screenshot failure still leaves bounded local evidence without another scrape."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ziniao_automation.models import Evidence, RunEvent
from ziniao_automation.workflows.amazon_inventory_gpsr.config import InventoryGpsrPolicy, SUBMISSION_RECEIPT
from ziniao_automation.workflows.amazon_inventory_gpsr.store import CONFIRMED

from .test_inventory_gpsr_workflow import Adapter, harness, items


class NoScreenshotPage:
    def __init__(self, failure):
        self.failure = failure

    async def screenshot(self, **kwargs):
        if self.failure == "hung":
            await asyncio.Event().wait()
        raise RuntimeError("PRIVATE_SCREENSHOT_ERROR Token=SECRET")

    def __getattr__(self, name):
        raise AssertionError(f"text evidence must not re-read page: {name}")


@pytest.mark.parametrize("failure", ["closed", "hung"])
async def test_screenshot_failure_keeps_form_and_confirmed_receipt_json(harness, tmp_path, failure):
    adapter = Adapter([replace(items(1)[0], title="PRIVATE_TITLE Cookie=SECRET")])
    adapter.timing = InventoryGpsrPolicy(evidence_timeout_seconds=0.01)
    run = replace(harness.new_run(), artifact_dir=tmp_path / "evidence")
    workflow = harness.workflow(adapter)
    page = NoScreenshotPage(failure)
    plan = await workflow.plan(run, page)
    await workflow.execute(run, page, plan)

    assert adapter.saved == ["sku-0"]
    assert (await harness.ledger.list_items(run.id))[0].state == CONFIRMED
    with harness.factory() as session:
        evidence = session.scalars(select(Evidence).where(Evidence.run_id == run.id)).all()
        assert len(evidence) == 2
        snapshots = {}
        for entry in evidence:
            assert entry.kind == "json" and entry.site_run_id
            content = Path(entry.file_path).read_bytes()
            assert entry.sha256 == hashlib.sha256(content).hexdigest()
            text = content.decode("utf8")
            assert all(word not in text for word in ("PRIVATE", "SECRET", "Cookie", "Token", "seller-id", "http"))
            payload = json.loads(text)
            assert set(payload) == {"schema_version", "evidence_source", "marketplace", "sku", "asin", "phase", "status", "receipt"}
            assert payload["sku"] == "sku-0" and payload["asin"] == "B000000000"
            snapshots[payload["phase"]] = payload
        assert snapshots["form"]["status"] == "ELIGIBLE"
        assert snapshots["form"]["receipt"] is None
        assert snapshots["receipt"]["status"] == CONFIRMED
        assert snapshots["receipt"]["receipt"] == SUBMISSION_RECEIPT


async def test_text_evidence_write_failure_never_reverses_confirmation(harness, tmp_path):
    adapter = Adapter(items(1))
    adapter.timing = InventoryGpsrPolicy(evidence_timeout_seconds=0.01)
    run = replace(harness.new_run(), artifact_dir=tmp_path / "evidence")
    workflow = harness.workflow(adapter)

    async def unavailable(*args, **kwargs):
        raise OSError("PRIVATE_DATABASE_OR_DISK_ERROR")

    harness.repo.save_evidence = unavailable
    page = NoScreenshotPage("closed")
    plan = await workflow.plan(run, page)
    await workflow.execute(run, page, plan)
    assert adapter.saved == ["sku-0"]
    assert (await harness.ledger.list_items(run.id))[0].state == CONFIRMED
    with harness.factory() as session:
        events = session.scalars(select(RunEvent).where(RunEvent.event_type == "inventory_gpsr_evidence_unavailable")).all()
        assert len(events) == 2
        assert all(event.details["text_evidence_saved"] is False for event in events)
        assert all("PRIVATE" not in event.message for event in events)


async def test_person_text_evidence_uses_person_receipt_not_seven_day_safety_claim(harness, tmp_path):
    from .test_inventory_compliance import DualAdapter, dual_run
    from ziniao_automation.workflows.amazon_inventory_gpsr.config import RESPONSIBLE_PERSON_RECEIPT
    adapter = DualAdapter()
    adapter.timing = InventoryGpsrPolicy(evidence_timeout_seconds=0.01)
    run = replace(dual_run(harness), artifact_dir=tmp_path / "evidence")
    workflow = harness.workflow(adapter)
    page = NoScreenshotPage("closed")
    plan = await workflow.plan(run, page)
    await workflow.execute(run, page, plan)
    rows = await harness.ledger.list_items(run.id)
    person = next(row for row in rows if row.item.requirement_type == "responsible_person")
    assert person.state == CONFIRMED
    path = run.artifact_dir / run.id / f"DE-inventory-{person.id}-receipt.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["receipt"] == RESPONSIBLE_PERSON_RECEIPT
    assert "七天" not in data["receipt"]
