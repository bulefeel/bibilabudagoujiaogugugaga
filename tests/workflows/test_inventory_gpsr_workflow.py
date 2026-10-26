"""Real SQLite barrier and deterministic adapter tests; no external browser."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from ziniao_automation.config import Settings
from ziniao_automation.db import Base, create_sqlite_engine, make_session_factory
from ziniao_automation.models import InventoryGpsrItem, OperationGuard, Run, RunEvent, Store, StoreMarketplace
from ziniao_automation.workflows.amazon_inventory_gpsr.config import InventoryGpsrPolicy, InventoryItem
from ziniao_automation.workflows.amazon_inventory_gpsr.definition import build_amazon_inventory_gpsr_definition
from ziniao_automation.workflows.amazon_inventory_gpsr.page import InventoryContractError, ItemLoadError
from ziniao_automation.workflows.amazon_inventory_gpsr.store import (
    ALREADY_SUBMITTED, BLOCKED, CONFIRMED, ELIGIBLE, FAILED, MISSING, SUBMITTING, UNCERTAIN,
    InventoryGpsrStore, InventoryRunCancelled, InventorySubmissionBlocked,
)
from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import AmazonInventoryGpsrWorkflow, InventoryResultUncertain
from ziniao_automation.workflows.errors import DomContractError, HumanAuthRequired
from ziniao_automation.workflows.repository_sqlalchemy import SqlAlchemyWorkflowRepository
from ziniao_automation.workflows.types import MarketplaceRef, RunMode, RunStatus, SiteStatus, StoreRef, WorkflowRun


class Adapter:
    def __init__(self, items=()):
        self.items = tuple(items)
        self.scans = 0
        self.inspected = []
        self.saved = []
        self.reconciled = []
        self.located = []
        self.results = {}
        self.known_saved = set()
        self.disappear = set()
        self.moved = set()
        self.confirm = True
        self.on_save = None
        self.on_confirm = None
        self.on_inspect = None
        self.cancel_check = None

    async def open_inventory(self, page, run, marketplace):
        return page

    async def scan_candidates(self, page, run, marketplace):
        self.scans += 1
        return self.items

    async def locate_item(self, page, item, *, search=False):
        self.located.append((item.sku, search))
        return item.sku not in self.disappear and (search or item.sku not in self.moved)

    async def inspect_item(self, page, run, marketplace, item):
        self.inspected.append(item.sku)
        if self.on_inspect:
            await self.on_inspect(item)
        outcome = self.results.get(item.sku, "eligible")
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(status=outcome, reason="", page=page)

    async def submit_once(self, page, run, marketplace, item):
        if self.on_save:
            await self.on_save(item)
        if self.cancel_check:
            await self.cancel_check(run)
        self.saved.append(item.sku)
        self.known_saved.add(item.sku)

    async def confirm_submission(self, page, run, marketplace, item):
        if self.on_confirm:
            await self.on_confirm(item)
        return self.confirm

    async def reconcile_item(self, page, run, marketplace, item):
        self.reconciled.append(item.sku)
        return item.sku in self.known_saved


def items(count=3):
    return tuple(InventoryItem(sku=f"sku-{i}", asin=f"B{i:09}", page_number=i // 100 + 1, position=i % 100) for i in range(count))


@pytest.fixture()
def harness(tmp_path):
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", evidence_dir=tmp_path / "evidence",
                        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}", testing=True)
    engine = create_sqlite_engine(settings)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        store = Store(name="测试店铺", selector_type="oauth", selector_value="fixture", enabled=True,
                      expected_seller_id="seller-id", identity_confirmed=True)
        session.add(store)
        session.flush()
        marketplace = StoreMarketplace(store_id=store.id, code="DE", domain="sellercentral.amazon.de", currency="EUR", enabled=True)
        session.add(marketplace)
        session.commit()
        store_id, market_id = store.id, marketplace.id
    repo = SqlAlchemyWorkflowRepository(factory)
    ledger = InventoryGpsrStore(factory)

    def new_run(run_id="run-1", mode=RunMode.AUTO):
        with factory() as session:
            session.add(Run(id=run_id, store_id=store_id, workflow="amazon_inventory_gpsr_attestation", mode=mode.value, status="RUNNING",
                            workflow_config={"marketplace_codes": ["DE"]}))
            session.commit()
        return WorkflowRun(id=run_id, workflow="amazon_inventory_gpsr_attestation", mode=mode,
                           store=StoreRef(str(store_id), "测试店铺", "oauth", "fixture", "seller-id", identity_confirmed=True),
                           marketplaces=(MarketplaceRef(str(market_id), "DE", "sellercentral.amazon.de", "EUR"),),
                           workflow_config={"marketplace_codes": ["DE"]})

    def workflow(adapter):
        return AmazonInventoryGpsrWorkflow(repository=repo, item_store=ledger, page_adapter=adapter)

    yield SimpleNamespace(factory=factory, repo=repo, ledger=ledger, new_run=new_run, workflow=workflow, engine=engine, settings=settings)
    engine.dispose()


async def execute(harness, adapter, run):
    flow = harness.workflow(adapter)
    await flow.preflight(run, object())
    plan = await flow.plan(run, object())
    if plan.lines:
        await flow.execute(run, object(), plan)
    return flow, plan


async def test_dry_run_inspects_all_candidates_to_form_and_never_saves(harness):
    adapter = Adapter(items(4))
    adapter.results = {"sku-0": "no_violation_entry", "sku-1": "no_attestation", "sku-2": "already_submitted"}
    run = harness.new_run(mode=RunMode.DRY_RUN)
    flow, plan = await execute(harness, adapter, run)
    assert not plan.lines
    assert adapter.inspected == ["sku-3", "sku-2", "sku-1", "sku-0"]
    assert not adapter.saved
    assert await harness.repo.get_site_status(run.id, "DE") == SiteStatus.DRY_RUN_COMPLETE
    counts = await harness.ledger.summary(run.id)
    assert counts == dict(scanned=4, eligible=1, confirmed=0, already_submitted=1,
                         no_violation_entry=1, no_attestation=1, missing=0, failed=0,
                         uncertain=0, pending=0, blocked=0)
    with harness.factory() as session:
        assert session.scalar(select(OperationGuard)) is None
        event = session.scalars(select(RunEvent).where(RunEvent.event_type == "inventory_gpsr_summary").order_by(RunEvent.id.desc())).first()
        assert event.details == counts and event.site_run_id


@pytest.mark.parametrize("status", list(RunStatus))
async def test_report_releases_response_observers_only_when_run_has_ended(harness, status):
    adapter = Adapter()
    detached = []
    adapter.detach_response_observers = lambda: detached.append(True)
    run = harness.new_run(mode=RunMode.DRY_RUN)
    report = await harness.workflow(adapter).report(run, status)
    assert report.status is status
    active = {
        RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_AUTH, RunStatus.RECONCILING,
    }
    assert detached == ([] if status in active else [True])


async def test_terminal_report_failure_still_releases_response_observers(harness):
    adapter = Adapter()
    detached = []
    adapter.detach_response_observers = lambda: detached.append(True)
    run = harness.new_run(mode=RunMode.DRY_RUN)
    flow = harness.workflow(adapter)

    async def fail_counts(_run):
        raise RuntimeError("fixture report unavailable")

    flow._counts = fail_counts
    with pytest.raises(RuntimeError, match="fixture report unavailable"):
        await flow.report(run, RunStatus.FAILED)
    assert detached == [True]


async def test_201_candidates_process_tail_first_and_keep_distinct_skus_for_same_asin(harness):
    candidates = list(items(201))
    candidates[100] = replace(candidates[100], asin=candidates[99].asin)
    adapter = Adapter(candidates)
    run = harness.new_run()

    async def check_intent(item):
        row = next(row for row in await harness.ledger.list_items(run.id) if row.item.sku == item.sku)
        assert row.state == SUBMITTING and row.intent_at is not None

    adapter.on_save = check_intent
    await execute(harness, adapter, run)
    assert adapter.saved == [f"sku-{i}" for i in reversed(range(201))]
    assert len(set(adapter.saved)) == 201
    assert (await harness.ledger.summary(run.id))["confirmed"] == 201


async def test_resume_uses_snapshot_never_rescans_or_repeats_confirmed_save(harness):
    adapter = Adapter(items())
    run = harness.new_run()
    first = True

    async def reauth(item):
        nonlocal first
        if item.sku == "sku-1" and first:
            first = False
            raise HumanAuthRequired("fixture signin")

    adapter.on_inspect = reauth
    with pytest.raises(HumanAuthRequired):
        await execute(harness, adapter, run)
    assert adapter.saved == ["sku-2"]
    adapter.items = items(5)
    await execute(harness, adapter, run)
    assert adapter.scans == 1
    assert adapter.saved == ["sku-2", "sku-1", "sku-0"]


@pytest.mark.parametrize("after_click", [False, True])
async def test_process_death_after_intent_never_reclicks(harness, after_click):
    class ProcessDied(BaseException):
        pass

    adapter = Adapter(items(2))
    run = harness.new_run()

    async def die(item):
        raise ProcessDied()

    if after_click:
        adapter.on_confirm = die
    else:
        adapter.on_save = die
    with pytest.raises(ProcessDied):
        await execute(harness, adapter, run)
    assert (await harness.ledger.list_items(run.id))[0].state == SUBMITTING
    adapter.on_save = adapter.on_confirm = None
    if after_click:
        await execute(harness, adapter, run)
        assert adapter.saved == ["sku-1", "sku-0"]
    else:
        with pytest.raises(InventoryResultUncertain):
            await execute(harness, adapter, run)
        assert not adapter.saved
        assert (await harness.ledger.list_items(run.id))[0].state == UNCERTAIN
    assert adapter.reconciled == ["sku-1"]
    assert adapter.scans == 1


async def test_unknown_submit_result_stops_later_products(harness):
    adapter = Adapter(items())
    adapter.confirm = False
    run = harness.new_run()
    with pytest.raises(InventoryResultUncertain):
        await execute(harness, adapter, run)
    assert adapter.saved == ["sku-2"]
    assert (await harness.ledger.summary(run.id))["uncertain"] == 1
    assert await harness.repo.get_site_status(run.id, "DE") == SiteStatus.FAILED


async def test_later_run_inspects_current_state_instead_of_searching_history(harness):
    adapter = Adapter(items(1))
    run = harness.new_run()
    await execute(harness, adapter, run)
    adapter.results["sku-0"] = "already_submitted"
    second = harness.new_run("run-2")
    await execute(harness, adapter, second)
    assert (await harness.ledger.list_items(second.id))[0].state == ALREADY_SUBMITTED
    adapter.results["sku-0"] = "eligible"
    third = harness.new_run("run-3")
    await execute(harness, adapter, third)
    assert (await harness.ledger.list_items(third.id))[0].state == BLOCKED
    assert adapter.saved == ["sku-0"] and not adapter.reconciled


async def test_same_attempt_failed_readback_preserves_unchecked_and_redacts_details(harness, caplog):
    adapter = Adapter(items(13))
    run = harness.new_run("current")
    flow = harness.workflow(adapter)
    await flow.plan(run, object())
    row = (await harness.ledger.list_items(run.id))[0]
    await harness.ledger.mark(row.id, ELIGIBLE, eligible=True)
    await harness.ledger.arm(row.id)
    async def broken_readback(page, run, marketplace, item):
        raise RuntimeError("SECRET_TOKEN https://example.invalid/?cookie=PRIVATE Cookie=SECRET")
    adapter.reconcile_item = broken_readback
    with pytest.raises(InventoryResultUncertain, match="回读执行异常") as error:
        await flow.plan(run, object())
    counts = await harness.ledger.summary(run.id)
    assert counts["pending"] == 12 and counts["uncertain"] == 1
    assert not adapter.saved
    with harness.factory() as db:
        events = db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)).all()
        diagnostic = next(e for e in events if e.event_type == "inventory_gpsr_reconciliation_failed")
        assert diagnostic.details["reason_code"] == "reconciliation_error"
        visible = repr([(e.message, e.details) for e in events]) + caplog.text + str(error.value)
        assert all(x not in visible for x in ("SECRET", "PRIVATE", "example.invalid", "cookie="))
        assert "broken_readback" in caplog.text


@pytest.mark.parametrize("exception,code,reason", [
    (TimeoutError("PRIVATE"), "read_timeout", "页面加载或读取超时"),
    (ItemLoadError("PRIVATE"), "read_timeout", "页面加载或读取超时"),
    (RuntimeError("Target page, context or browser has been closed PRIVATE"), "target_closed", "页面或紫鸟连接已关闭"),
    (DomContractError("PRIVATE"), "page_contract_error", "页面结构或商品身份未通过核验"),
    (InventoryContractError("德国站库存接口返回 HTTP 403，PRIVATE"), "inventory_http_error", "库存接口返回 HTTP 403"),
    (RuntimeError("HTTP 403 PRIVATE"), "reconciliation_error", "回读执行异常，需核查本地诊断"),
])
def test_history_failure_diagnostic_is_fixed_whitelist(exception, code, reason):
    from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import _safe_reconciliation_failure

    details, summary = _safe_reconciliation_failure(exception)
    assert details["reason_code"] == code
    assert summary == reason
    assert "PRIVATE" not in repr(details) + summary
    if code == "inventory_http_error":
        assert details["http_status"] == 403


def test_history_failure_reports_target_closed_cause_without_its_text():
    from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import _safe_reconciliation_failure

    outer = ItemLoadError("PRIVATE_INPUT")
    outer.__cause__ = RuntimeError("Target page, context or browser has been closed; TOKEN=SECRET")
    details, reason = _safe_reconciliation_failure(outer)
    assert details == {"reason_code": "target_closed", "exception_type": "RuntimeError"}
    assert reason == "页面或紫鸟连接已关闭"


@pytest.mark.parametrize("function_name,stage", [
    ("locate_item", "search_or_location"),
    ("_wait_panel", "sidebar_read"),
    ("_wait_list", "inventory_list"),
    ("_identity", "identity_check"),
    ("unknown_private_function", "history_reconciliation"),
])
def test_history_failure_stage_is_selected_from_fixed_frame_names(function_name, stage):
    from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import _safe_reconciliation_stage

    namespace = {}
    exec(f"def {function_name}():\n    raise RuntimeError('PRIVATE')", namespace)
    try:
        namespace[function_name]()
    except RuntimeError as exc:
        code, label = _safe_reconciliation_stage(exc)
    assert code == stage
    assert "PRIVATE" not in label and function_name not in label


async def test_unchecked_snapshot_is_not_a_completed_site(harness):
    adapter = Adapter(items(2))
    run = harness.new_run()
    flow = harness.workflow(adapter)
    await flow.plan(run, object())
    await flow._finish_site(run, run.marketplaces[0])
    assert (await harness.ledger.summary(run.id))["pending"] == 2
    assert await harness.repo.get_site_status(run.id, "DE") == SiteStatus.FAILED


async def test_partial_read_failure_does_not_disappear_in_dry_run(harness):
    adapter = Adapter(items())
    adapter.results["sku-1"] = ItemLoadError("fixture timeout")
    run = harness.new_run(mode=RunMode.DRY_RUN)
    _, plan = await execute(harness, adapter, run)
    assert not plan.lines
    assert adapter.inspected == ["sku-2", "sku-1", "sku-0"]
    assert await harness.repo.get_site_status(run.id, "DE") == SiteStatus.FAILED
    assert (await harness.ledger.summary(run.id))["failed"] == 1


async def test_identity_or_contract_failure_stops_subsequent_items(harness):
    adapter = Adapter(items())
    adapter.results["sku-2"] = DomContractError("fixture wrong identity")
    run = harness.new_run()
    with pytest.raises(DomContractError):
        await execute(harness, adapter, run)
    assert adapter.inspected == ["sku-2"]
    assert not adapter.saved
    assert (await harness.ledger.summary(run.id))["failed"] == 1


async def test_empty_snapshot_is_durable(harness):
    adapter = Adapter()
    run = harness.new_run()
    await execute(harness, adapter, run)
    adapter.items = items()
    await execute(harness, adapter, run)
    assert adapter.scans == 1
    assert not adapter.saved
    assert await harness.ledger.snapshot_exists(run.id, "DE")


async def test_duplicate_sku_scan_commits_neither_rows_nor_complete_marker(harness):
    adapter = Adapter([items(1)[0], items(1)[0]])
    run = harness.new_run()
    with pytest.raises(ValueError, match="重复 SKU"):
        await execute(harness, adapter, run)
    assert not await harness.ledger.snapshot_exists(run.id, "DE")
    assert not await harness.ledger.list_items(run.id)
    assert not adapter.saved


async def test_missing_candidate_gets_one_bounded_search_not_false_success(harness):
    adapter = Adapter(items())
    adapter.disappear = {"sku-1"}
    adapter.moved = {"sku-2"}
    run = harness.new_run()
    await execute(harness, adapter, run)
    assert adapter.saved == ["sku-0", "sku-2"]
    assert adapter.located.count(("sku-1", True)) == 1
    assert adapter.located.count(("sku-2", True)) == 1
    row = next(row for row in await harness.ledger.list_items(run.id) if row.item.sku == "sku-1")
    assert row.state == MISSING
    assert (await harness.ledger.summary(run.id))["confirmed"] == 2


async def test_cancel_during_inspection_prevents_intent_and_save(harness):
    adapter = Adapter(items())
    run = harness.new_run()

    async def cancel(item):
        await harness.repo.set_run_status(run.id, RunStatus.CANCELLED)

    adapter.on_inspect = cancel
    with pytest.raises(InventoryRunCancelled):
        await execute(harness, adapter, run)
    assert not adapter.saved
    assert not any(row.intent_at for row in await harness.ledger.list_items(run.id))


async def test_atomic_arm_rejects_cancelled_run_and_dryrun(harness):
    adapter = Adapter(items(1))
    run = harness.new_run(mode=RunMode.DRY_RUN)
    await execute(harness, adapter, run)
    row = (await harness.ledger.list_items(run.id))[0]
    with pytest.raises(InventorySubmissionBlocked):
        await harness.ledger.arm(row.id)
    await harness.repo.set_run_status(run.id, RunStatus.CANCELLED)
    with pytest.raises(InventoryRunCancelled):
        await harness.ledger.arm(row.id)


async def test_retaining_submission_barrier_after_run_report_deletion(harness):
    adapter = Adapter(items(1))
    run = harness.new_run()
    await execute(harness, adapter, run)
    with harness.factory() as session:
        session.execute(delete(Run).where(Run.id == run.id))
        session.commit()
        kept = session.scalar(select(InventoryGpsrItem))
        assert kept.run_id is None and kept.state == CONFIRMED
    second = harness.new_run("run-2")
    await execute(harness, adapter, second)
    assert adapter.saved == ["sku-0"]


def test_definition_is_independent_standard_workflow():
    definition = build_amazon_inventory_gpsr_definition()
    assert definition.key == "amazon_inventory_gpsr_attestation"
    assert definition.business_priority == 4
    assert not definition.requires_financial_lock
    assert definition.default_mode == RunMode.DRY_RUN
    assert definition.supported_modes == (RunMode.DRY_RUN, RunMode.AUTO)
    assert definition.config_model.model_validate({"marketplace_codes": ["de"]}).marketplace_codes == ("DE",)
    with pytest.raises(ValueError):
        definition.config_model.model_validate({"marketplace_codes": ["CA"]})
    with pytest.raises(ValueError):
        definition.config_model.model_validate({"script": "arbitrary"})


async def test_cancel_after_arm_uses_adapter_callback_and_retains_intent(harness):
    adapter = Adapter(items(1))
    run = harness.new_run()

    async def cancel(item):
        await harness.repo.set_run_status(run.id, RunStatus.CANCELLED)

    adapter.on_save = cancel
    with pytest.raises(InventoryRunCancelled):
        await execute(harness, adapter, run)
    assert not adapter.saved
    row = (await harness.ledger.list_items(run.id))[0]
    assert row.state == SUBMITTING and row.intent_at


async def test_dryrun_auth_resume_does_not_revisit_completed_forms(harness):
    adapter = Adapter(items())
    run = harness.new_run(mode=RunMode.DRY_RUN)
    first = True

    async def reauth(item):
        nonlocal first
        if item.sku == "sku-1" and first:
            first = False
            raise HumanAuthRequired("fixture signin")

    adapter.on_inspect = reauth
    with pytest.raises(HumanAuthRequired):
        await execute(harness, adapter, run)
    await execute(harness, adapter, run)
    assert adapter.inspected == ["sku-2", "sku-1", "sku-1", "sku-0"]
    assert not adapter.saved


async def test_screenshot_timeout_is_bounded_and_does_not_change_item_state(harness, tmp_path):
    adapter = Adapter(items(1))
    adapter.timing = InventoryGpsrPolicy(evidence_timeout_seconds=0.02)
    run = harness.new_run(mode=RunMode.DRY_RUN)
    flow, _ = await execute(harness, adapter, run)
    row = (await harness.ledger.list_items(run.id))[0]

    class SlowPage:
        async def screenshot(self, **kwargs):
            await asyncio.Event().wait()

    run = replace(run, artifact_dir=tmp_path / "evidence")
    await asyncio.wait_for(flow._capture_evidence(run, SlowPage(), run.marketplaces[0], row, "form"), timeout=1)
    assert (await harness.ledger.get(row.id)).state == ELIGIBLE
    with harness.factory() as session:
        event = session.scalar(select(RunEvent).where(RunEvent.event_type == "inventory_gpsr_evidence_unavailable"))
        assert event and event.details == {"stage": "form", "reason_code": "screenshot_unavailable", "text_evidence_saved": True}


async def test_new_run_without_explicit_unprocessed_proof_does_not_resave(harness):
    adapter = Adapter(items(1)); adapter.confirm = False
    run = harness.new_run()
    with pytest.raises(InventoryResultUncertain):
        await execute(harness, adapter, run)
    second = harness.new_run("run-2")
    await execute(harness, adapter, second)
    assert adapter.saved == ["sku-0"] and not adapter.reconciled
    assert await harness.ledger.snapshot_exists(second.id, "DE")
    report = await harness.workflow(adapter).report(second, RunStatus.FAILED)
    assert report.fields["uncertain"] == 0 and report.fields["blocked"] == 1


async def test_barrier_rejects_even_direct_second_run_arm(harness):
    adapter = Adapter(items(1))
    run = harness.new_run()
    await execute(harness, adapter, run)
    second = harness.new_run("run-2")
    await harness.repo.set_site_status(second.id, "DE", SiteStatus.PREFLIGHT)
    await harness.ledger.save_snapshot(run_id=second.id, store_id=int(second.store.id), marketplace_code="DE", items=items(1))
    row = (await harness.ledger.list_items(second.id))[0]
    await harness.ledger.mark(row.id, ELIGIBLE, eligible=True)
    with pytest.raises(InventorySubmissionBlocked):
        await harness.ledger.arm(row.id)


@pytest.mark.parametrize("mode,failure,expected", [
    (RunMode.DRY_RUN, ItemLoadError("load"), RunStatus.PARTIAL),
    (RunMode.AUTO, DomContractError("contract"), RunStatus.PARTIAL),
])
async def test_engine_reports_partial_for_reads_or_prior_confirmed_items(harness, mode, failure, expected):
    from ziniao_automation.workflows.engine import WorkflowEngine
    from ziniao_automation.workflows.registry import WorkflowRegistry

    class Sessions:
        @asynccontextmanager
        async def session(self, selector, store_key=None):
            yield SimpleNamespace(page=object())
        financial_session = session

    adapter = Adapter(items(2))
    adapter.results["sku-0"] = failure
    run = harness.new_run(mode=mode)
    await harness.repo.set_run_status(run.id, RunStatus.QUEUED)
    flow = harness.workflow(adapter)
    registry = WorkflowRegistry((build_amazon_inventory_gpsr_definition(flow),))
    engine = WorkflowEngine(registry=registry, repository=harness.repo, browser_sessions=Sessions())
    result = await engine.start(run)
    assert result.status == expected
    assert await harness.repo.get_run_status(run.id) == expected
    if mode is RunMode.AUTO:
        assert adapter.saved == ["sku-1"]
    else:
        assert not adapter.saved


def test_migration_upgrade_downgrade_preserves_existing_runs_and_creates_backup(harness, tmp_path):
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect
    from ziniao_automation.db import _migration_config, database_revision, init_database

    run = harness.new_run()
    config = _migration_config()
    with harness.engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, "0013")
        command.downgrade(config, "0012")
    assert "inventory_gpsr_items" not in inspect(harness.engine).get_table_names()
    backup = init_database(harness.engine, backup_dir=tmp_path / "backups")
    assert backup and backup.exists()
    assert database_revision(harness.engine) == ScriptDirectory.from_config(config).get_current_head()
    assert "inventory_gpsr_items" in inspect(harness.engine).get_table_names()
    with harness.factory() as session:
        assert session.get(Run, run.id).status == "RUNNING"
    with harness.engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0012")
    assert "inventory_gpsr_items" not in inspect(harness.engine).get_table_names()
    with harness.factory() as session:
        assert session.get(Run, run.id).status == "RUNNING"


def test_interrupted_migration_retry_recreates_submission_unique_index(harness):
    from alembic import command
    from sqlalchemy import inspect
    from ziniao_automation.db import _migration_config

    config = _migration_config()
    with harness.engine.connect() as connection:
        connection.exec_driver_sql("DROP INDEX uq_inventory_gpsr_submission")
        connection.commit()
        config.attributes["connection"] = connection
        command.stamp(config, "0012")
        command.upgrade(config, "0013")
    indexes = {index["name"]: index for index in inspect(harness.engine).get_indexes("inventory_gpsr_items")}
    assert indexes["uq_inventory_gpsr_submission"]["unique"]


@pytest.mark.parametrize("state", [SUBMITTING, UNCERTAIN, CONFIRMED])
async def test_downgrade_rejects_loss_of_submission_history(harness, state):
    from alembic import command
    from sqlalchemy import inspect
    from ziniao_automation.db import _migration_config, database_revision

    run = harness.new_run()
    await harness.repo.set_site_status(run.id, "DE", SiteStatus.PREFLIGHT)
    await harness.ledger.save_snapshot(run_id=run.id, store_id=int(run.store.id), marketplace_code="DE", items=items(1))
    row = (await harness.ledger.list_items(run.id))[0]
    await harness.ledger.mark(row.id, ELIGIBLE, eligible=True)
    await harness.ledger.arm(row.id)
    if state != SUBMITTING:
        await harness.ledger.mark(row.id, state)
    config = _migration_config()
    with harness.engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, "0013")
        with pytest.raises(RuntimeError, match="防重复"):
            command.downgrade(config, "0012")
    assert database_revision(harness.engine) == "0013"
    assert "inventory_gpsr_items" in inspect(harness.engine).get_table_names()
    assert (await harness.ledger.get(row.id)).state == state


@pytest.mark.parametrize("old_still_listed", [False, True])
async def test_store_wide_unresolved_history_never_blocks_other_candidates(harness, old_still_listed):
    adapter = Adapter(items(1)); adapter.confirm = False
    old = harness.new_run("old")
    with pytest.raises(InventoryResultUncertain):
        await execute(harness, adapter, old)
    adapter.confirm = True
    adapter.items = items(2) if old_still_listed else (items(2)[1],)
    second = harness.new_run("second")
    await execute(harness, adapter, second)
    assert adapter.saved == ["sku-0", "sku-1"]
    assert adapter.scans == 2 and not adapter.reconciled
    assert (await harness.ledger.summary(second.id))["blocked"] == int(old_still_listed)
    assert (await harness.ledger.list_items(old.id))[0].state == UNCERTAIN


async def test_pruned_run_history_preserved_without_global_recovery(harness):
    adapter = Adapter(items(1)); adapter.confirm = False
    old = harness.new_run("old")
    with pytest.raises(InventoryResultUncertain):
        await execute(harness, adapter, old)
    record = (await harness.ledger.list_items(old.id))[0]
    with harness.factory() as session:
        session.execute(delete(Run).where(Run.id == old.id)); session.commit()
    adapter.confirm = True
    adapter.items = (items(2)[1],)
    await execute(harness, adapter, harness.new_run("new"))
    assert adapter.saved == ["sku-0", "sku-1"] and not adapter.reconciled
    assert (await harness.ledger.get(record.id)).state == UNCERTAIN


async def test_possible_old_receipt_is_not_sought_or_used_to_rewrite_history(harness):
    adapter = Adapter(items(1)); adapter.confirm = False
    old = harness.new_run("old")
    with pytest.raises(InventoryResultUncertain):
        await execute(harness, adapter, old)
    adapter.confirm = True
    adapter.items = (items(2)[1],)
    await execute(harness, adapter, harness.new_run("new"))
    assert not adapter.reconciled
    assert adapter.saved == ["sku-0", "sku-1"]
    assert (await harness.ledger.list_items(old.id))[0].state == UNCERTAIN


def _real_runtime(harness, adapter):
    from ziniao_automation.workflows.engine import WorkflowEngine
    from ziniao_automation.workflows.registry import WorkflowRegistry
    from ziniao_automation.workflows.runtime import AutomationService, DatabaseRunLoader

    class Sessions:
        @asynccontextmanager
        async def session(self, selector, store_key=None):
            yield SimpleNamespace(page=object())
        financial_session = session

        async def cancel_auth(self, run_id):
            return False

    registry = WorkflowRegistry((build_amazon_inventory_gpsr_definition(harness.workflow(adapter)),))
    loader = DatabaseRunLoader(harness.factory, workflow_registry=registry)
    controller = Sessions()
    engine = WorkflowEngine(registry=registry, repository=harness.repo, browser_sessions=controller)
    service = AutomationService(engine=engine, run_loader=loader, ziniao_controller=controller, session_factory=harness.factory)
    return service, engine, registry


@pytest.mark.parametrize("after_intent", [False, True])
async def test_public_cancel_api_stops_running_inventory_and_preserves_intent(harness, after_intent):
    from fastapi.testclient import TestClient
    from ziniao_automation.web import create_app
    from tests.test_db_api import bootstrap

    adapter = Adapter(items(2))
    run = harness.new_run()
    service, engine, registry = _real_runtime(harness, adapter)
    app = create_app(harness.settings, automation_service=service, workflow_registry=registry)
    paused, release = asyncio.Event(), asyncio.Event()

    async def wait_for_cancel(item):
        paused.set()
        await release.wait()

    if after_intent:
        adapter.on_save = wait_for_cancel
    else:
        adapter.on_inspect = wait_for_cancel
    await harness.repo.set_run_status(run.id, RunStatus.QUEUED)
    with TestClient(app) as client:
        csrf = bootstrap(client)
        task = asyncio.create_task(engine.start(run))
        await asyncio.wait_for(paused.wait(), timeout=3)
        response = client.post(f"/api/runs/{run.id}/cancel", json={}, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 202, response.text
        release.set()
        with pytest.raises(InventoryRunCancelled):
            await asyncio.wait_for(task, timeout=3)
    assert await harness.repo.get_run_status(run.id) == RunStatus.CANCELLED
    assert not adapter.saved
    row = (await harness.ledger.list_items(run.id))[0]
    assert bool(row.intent_at) is after_intent
    if after_intent:
        assert row.state == SUBMITTING


@pytest.mark.parametrize("confirmed_on_recovery", [False, True])
async def test_real_queue_restart_recovers_intent_before_other_snapshot_items(harness, confirmed_on_recovery):
    from ziniao_automation.models import RunQueueEntry

    adapter = Adapter(items(2))
    run = harness.new_run()
    flow = harness.workflow(adapter)
    await flow.plan(run, object())
    row = (await harness.ledger.list_items(run.id))[0]
    await harness.ledger.mark(row.id, ELIGIBLE, eligible=True)
    await harness.ledger.arm(row.id)
    if confirmed_on_recovery:
        adapter.known_saved.add(row.item.sku)
    # A new service/engine represents a clean process. Its real recovery loader
    # resets the durable CLAIMED row, then the worker dispatches normal START.
    service, _, _ = _real_runtime(harness, adapter)
    entry_id, _ = service._queue.enqueue(run.id)
    entry = service._queue.claim_next()
    assert entry.id == entry_id and entry.state == "CLAIMED"
    try:
        await service.recover_startup()
        await asyncio.wait_for(service.wait_idle(), timeout=5)
    finally:
        await service.shutdown_worker()
    assert adapter.scans == 1
    assert adapter.reconciled == [row.item.sku]
    assert adapter.saved == (["sku-0"] if confirmed_on_recovery else [])
    assert (await harness.ledger.get(row.id)).state == (CONFIRMED if confirmed_on_recovery else UNCERTAIN)
    with harness.factory() as session:
        restored = session.get(RunQueueEntry, entry_id)
        assert restored.state == "DONE" and restored.action == "START"


@pytest.mark.parametrize("residual", [False, True])
async def test_item_load_failure_captures_current_page_then_continues(harness, tmp_path, residual):
    from pathlib import Path
    from ziniao_automation.models import Evidence

    captures = []

    class CurrentPage:
        sku = ""

        async def screenshot(self, *, path, full_page, timeout):
            captures.append((self.sku, full_page, timeout))
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    page = CurrentPage()
    adapter = Adapter(items(3))
    adapter.timing = InventoryGpsrPolicy(evidence_timeout_seconds=5.0)
    adapter.results = {"sku-0": "no_attestation", "sku-1": ItemLoadError("fixture loading"), "sku-2": "no_attestation"}
    if residual:
        adapter.moved.add("sku-1")

    async def set_current(item):
        page.sku = item.sku

    adapter.on_inspect = set_current
    run = replace(harness.new_run(mode=RunMode.DRY_RUN), artifact_dir=tmp_path / "evidence")
    flow = harness.workflow(adapter)
    await flow.plan(run, page)
    assert captures == [("sku-1", False, 5000)]
    assert adapter.inspected == (["sku-2", "sku-0", "sku-1"] if residual else ["sku-2", "sku-1", "sku-0"])
    assert (await harness.ledger.summary(run.id))["failed"] == 1
    assert await harness.repo.get_site_status(run.id, "DE") == SiteStatus.FAILED
    with harness.factory() as session:
        evidence = session.scalar(select(Evidence).where(Evidence.run_id == run.id))
        assert evidence and evidence.metadata_json["label"] == "inventory_load-failure"
        assert evidence.site_run_id and Path(evidence.file_path).is_file()


@pytest.mark.parametrize("screenshot_failure", ["closed", "hung"])
async def test_load_failure_evidence_error_preserves_business_result(harness, tmp_path, screenshot_failure):
    class CurrentPage:
        async def screenshot(self, **kwargs):
            if screenshot_failure == "hung":
                await asyncio.Event().wait()
            raise RuntimeError("Target page has been closed")

    adapter = Adapter(items(3))
    adapter.timing = InventoryGpsrPolicy(evidence_timeout_seconds=0.02)
    adapter.results = {"sku-0": "no_attestation", "sku-1": ItemLoadError("fixture loading"), "sku-2": "no_attestation"}
    run = replace(harness.new_run(mode=RunMode.DRY_RUN), artifact_dir=tmp_path / "evidence")
    await asyncio.wait_for(harness.workflow(adapter).plan(run, CurrentPage()), timeout=2)
    assert adapter.inspected == ["sku-2", "sku-1", "sku-0"]
    failed = next(record for record in await harness.ledger.list_items(run.id) if record.item.sku == "sku-1")
    assert failed.state == FAILED and failed.reason == "item_load_timeout"
    with harness.factory() as session:
        event = session.scalar(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.event_type == "inventory_gpsr_evidence_unavailable"))
        assert event and event.details["stage"] == "load-failure"


@pytest.mark.parametrize("residual", [False, True])
async def test_cancelled_item_load_failure_never_attempts_screenshot(harness, tmp_path, residual):
    captures = []

    class CurrentPage:
        async def screenshot(self, **kwargs):
            captures.append(kwargs)

    adapter = Adapter(items(1))
    adapter.timing = InventoryGpsrPolicy(evidence_timeout_seconds=5.0)
    adapter.results["sku-0"] = ItemLoadError("fixture loading")
    if residual:
        adapter.moved.add("sku-0")
    run = replace(harness.new_run(mode=RunMode.DRY_RUN), artifact_dir=tmp_path / "evidence")

    async def cancel(item):
        await harness.repo.set_run_status(run.id, RunStatus.CANCELLED)

    adapter.on_inspect = cancel
    with pytest.raises(InventoryRunCancelled):
        await harness.workflow(adapter).plan(run, CurrentPage())
    assert not captures
    assert (await harness.ledger.list_items(run.id))[0].reason == "item_load_timeout"
