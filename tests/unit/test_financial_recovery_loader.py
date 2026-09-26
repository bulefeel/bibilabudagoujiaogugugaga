from __future__ import annotations

from pathlib import Path

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import (
    OperationGuard,
    Run,
    RunEvent,
    RunQueueEntry,
    SiteRun,
    Store,
    StoreMarketplace,
)
from ziniao_automation.queue import DurableRunQueue
from ziniao_automation.workflows.amazon_disbursement import (
    build_amazon_disbursement_definition,
)
from ziniao_automation.workflows.registry import WorkflowRegistry
from ziniao_automation.workflows.runtime import AutomationService, DatabaseRunLoader


@pytest.fixture()
def recovery_database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'recovery.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    sessions = make_session_factory(engine)
    yield settings, sessions
    engine.dispose()


def _add_guarded_run(
    session,
    *,
    store: Store,
    marketplace: StoreMarketplace,
    workflow: str = "amazon_disbursement",
    config_version: int = 1,
    config: dict | None = None,
) -> str:
    run = Run(
        store_id=store.id,
        workflow=workflow,
        mode="approval",
        trigger="manual",
        status="RUNNING",
        requested_by="admin",
        workflow_config=config or {"marketplace_codes": ["CA"]},
        workflow_config_version=config_version,
    )
    session.add(run)
    session.flush()
    site = SiteRun(
        run_id=run.id,
        marketplace_id=marketplace.id,
        marketplace_code="CA",
        status="ARMED",
        currency="CAD",
        payable_amount=10,
        delayed_amount=0,
        settlement_key=f"cycle-{run.id}",
        plan_hash="a" * 64,
        snapshot_hash="b" * 64,
    )
    session.add(site)
    session.flush()
    session.add(
        OperationGuard(
            guard_key=f"guard-{run.id}",
            run_id=run.id,
            site_run_id=site.id,
            store_id=store.id,
            workflow=workflow,
            marketplace_code="CA",
            settlement_key=f"cycle-{run.id}",
            state="ARMED",
            amount=10,
            currency="CAD",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
    )
    session.flush()
    return run.id


@pytest.mark.asyncio
async def test_financial_recovery_uses_guards_and_isolates_bad_legacy_run(
    recovery_database,
) -> None:
    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store = Store(
            name="Recovery store",
            selector_type="oauth",
            selector_value="oauth-recovery",
            browser_oauth="oauth-recovery",
            expected_seller_id="SELLER-RECOVERY",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        marketplace = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        session.add(marketplace)
        session.flush()
        current_id = _add_guarded_run(
            session, store=store, marketplace=marketplace
        )
        # A contradictory terminal run status must not suppress an active
        # financial guard's platform read-back.
        session.get(Run, current_id).status = "SUCCEEDED"
        old_id = _add_guarded_run(
            session,
            store=store,
            marketplace=marketplace,
            config_version=99,
            config={"removed_field": "legacy"},
        )
        broken_id = _add_guarded_run(
            session,
            store=store,
            marketplace=marketplace,
            workflow="retired_financial_workflow",
            config_version=7,
            config={"anything": True},
        )
        settled_id = _add_guarded_run(
            session, store=store, marketplace=marketplace
        )
        session.get(Run, settled_id).status = "RECONCILING"
        session.query(OperationGuard).filter_by(run_id=settled_id).one().state = (
            "CONFIRMED"
        )
        session.query(SiteRun).filter_by(run_id=settled_id).one().status = "CONFIRMED"
        session.commit()

    queue = DurableRunQueue(sessions)
    current_entry, _ = queue.enqueue(current_id)
    old_entry, _ = queue.enqueue(old_id)
    broken_entry, _ = queue.enqueue(broken_id)
    settled_entry, _ = queue.enqueue(settled_id, action="RECONCILE")
    with sessions() as session:
        for entry_id in (current_entry, old_entry, broken_entry, settled_entry):
            entry = session.get(RunQueueEntry, entry_id)
            entry.state = "CLAIMED"
            entry.claim_token = f"claim-{entry_id}"
        session.commit()

    loader = DatabaseRunLoader(
        sessions,
        artifact_root=settings.evidence_dir,
        workflow_registry=registry,
    )
    plan = await loader.startup_recovery_plan()

    assert set(plan.financial_guarded_ids) == {current_id, old_id, broken_id}
    assert plan.financial_failed_ids == (broken_id,)
    assert plan.reconciling_ids == (settled_id,)
    assert {run.id for run in plan.financial} == {current_id, old_id}
    old_snapshot = next(run for run in plan.financial if run.id == old_id)
    assert old_snapshot.workflow_config == {"marketplace_codes": ["CA"]}
    assert old_snapshot.workflow_config_version == 99
    assert [market.code for market in old_snapshot.marketplaces] == ["CA"]

    class Engine:
        def __init__(self) -> None:
            self.registry = registry
            self.repository = object()
            self.reconciled: list[str] = []

        async def settle(self, run) -> None:
            # 这里只钉**路由**：启动恢复把谁交给了 settle。终态由真引擎的
            # settle 决定，见 tests/workflows/test_self_review_invariants.py ——
            # 替身自己写 SUCCEEDED 的话，真 settle 不落终态也照样绿。
            self.reconciled.append(run.id)

    engine = Engine()
    service = AutomationService(
        engine=engine,
        run_loader=loader,
        ziniao_controller=object(),
        session_factory=sessions,
    )
    await service.recover_startup()
    await service.wait_idle()
    await service.shutdown_worker()

    assert set(engine.reconciled) == {current_id, old_id, settled_id}
    with sessions() as session:
        assert session.get(RunQueueEntry, current_entry).state == "DONE"
        assert session.get(RunQueueEntry, old_entry).state == "DONE"
        assert session.get(RunQueueEntry, broken_entry).state == "CANCELLED"
        assert session.get(RunQueueEntry, settled_entry).state == "DONE"
        broken = session.get(Run, broken_id)
        assert broken.status == "UNCERTAIN_FINANCIAL"
        assert "人工核查" in broken.error
        assert session.query(RunEvent).filter_by(
            run_id=broken_id,
            event_type="FINANCIAL_RECOVERY_LOAD_FAILED",
        ).count() >= 1


@pytest.mark.parametrize(
    "crash_status",
    (
        "RUNNING",
        "QUEUED",
        "WAITING_APPROVAL",
        "WAITING_AUTH",
        "NEEDS_HUMAN_AUTH",
    ),
)
@pytest.mark.asyncio
async def test_confirmed_guard_crash_leftover_only_reconciles(
    recovery_database,
    crash_status: str,
) -> None:
    """A final CONFIRMED guard must still block ordinary startup replay."""

    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store = Store(
            name=f"Confirmed guard {crash_status}",
            selector_type="oauth",
            selector_value=f"oauth-{crash_status.lower()}",
            browser_oauth=f"oauth-{crash_status.lower()}",
            expected_seller_id="SELLER-CONFIRMED-GUARD",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        marketplace = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        session.add(marketplace)
        session.flush()
        run_id = _add_guarded_run(
            session,
            store=store,
            marketplace=marketplace,
        )
        session.get(Run, run_id).status = crash_status
        session.query(OperationGuard).filter_by(run_id=run_id).one().state = (
            "CONFIRMED"
        )
        session.query(SiteRun).filter_by(run_id=run_id).one().status = "CONFIRMED"
        session.commit()

    loader = DatabaseRunLoader(
        sessions,
        artifact_root=settings.evidence_dir,
        workflow_registry=registry,
    )
    plan = await loader.startup_recovery_plan()

    assert plan.financial_guarded_ids == ()
    assert plan.reconciling_ids == (run_id,)
    assert run_id not in plan.running_ids
    assert run_id not in plan.queued_ids
    assert run_id not in plan.waiting_auth_ids

    class Repository:
        async def set_run_status(self, *_args, **_kwargs) -> bool:
            # This is only reachable with the unsafe pre-fix RUNNING routing.
            return True

    class Engine:
        def __init__(self) -> None:
            self.registry = registry
            self.repository = Repository()
            self.start_calls: list[str] = []
            self.settle_calls: list[str] = []

        async def start(self, run) -> None:
            self.start_calls.append(run.id)

        async def settle(self, run) -> None:
            self.settle_calls.append(run.id)

    engine = Engine()
    service = AutomationService(
        engine=engine,
        run_loader=loader,
        ziniao_controller=object(),
        session_factory=sessions,
    )
    try:
        await service.recover_startup()
        await service.wait_idle()
    finally:
        await service.shutdown_worker()

    assert engine.start_calls == []
    assert engine.settle_calls == [run_id]
    with sessions() as session:
        entry = session.query(RunQueueEntry).filter_by(run_id=run_id).one()
        assert entry.action == "RECONCILE"
        assert entry.state == "DONE"
        assert session.query(OperationGuard).filter_by(run_id=run_id).count() == 1


@pytest.mark.asyncio
async def test_cancel_does_not_remove_reconcile_queue_entry(recovery_database) -> None:
    """重启时建出来的资金收口条目,必须挡住取消请求。

    已 CONFIRMED 的 guard 在 ARMED/SUBMITTED/UNCERTAIN 这个窄口径下不算"活跃",
    但队列里那条动作本身仍代表"这笔钱还没收口"。运行时必须在发出取消信号或调用
    ``cancel_ready`` **之前**就拒绝。

    ⚠️ 2026-09-23 删掉联网回读之后,这条依然成立 —— 变的只是那条队列动作派发到
    离线收口而不是开浏览器;"收口完成前不许取消"这道闸和回读无关。
    """

    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store = Store(
            name="Reconcile cancellation store",
            selector_type="oauth",
            selector_value="oauth-reconcile-cancel",
            browser_oauth="oauth-reconcile-cancel",
            expected_seller_id="SELLER-RECONCILE-CANCEL",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        marketplace = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        session.add(marketplace)
        session.flush()
        run_id = _add_guarded_run(
            session, store=store, marketplace=marketplace
        )
        session.get(Run, run_id).status = "RECONCILING"
        session.query(OperationGuard).filter_by(run_id=run_id).one().state = (
            "CONFIRMED"
        )
        session.query(SiteRun).filter_by(run_id=run_id).one().status = "CONFIRMED"
        session.commit()

    queue = DurableRunQueue(sessions)
    entry_id, created = queue.enqueue(run_id, action="RECONCILE")
    assert created is True

    class Controller:
        def __init__(self) -> None:
            self.cancel_calls = 0

        async def cancel_auth(self, _run_id: str) -> bool:
            self.cancel_calls += 1
            return True

    class Engine:
        def __init__(self) -> None:
            self.cancel_calls = 0

        async def cancel(self, _run) -> None:
            self.cancel_calls += 1

    controller = Controller()
    engine = Engine()
    loader = DatabaseRunLoader(
        sessions,
        artifact_root=settings.evidence_dir,
        workflow_registry=registry,
    )
    service = AutomationService(
        engine=engine,
        run_loader=loader,
        ziniao_controller=controller,
        session_factory=sessions,
    )

    with pytest.raises(RuntimeError, match="收口队列"):
        await service.cancel_run(run_id)

    assert controller.cancel_calls == 0
    assert engine.cancel_calls == 0
    with sessions() as session:
        assert session.get(RunQueueEntry, entry_id).state == "READY"


@pytest.mark.parametrize("guard_state", ["SUBMITTED", "CONFIRMED"])
@pytest.mark.asyncio
async def test_a_finished_payout_is_left_alone_on_restart(
    recovery_database, guard_state: str
) -> None:
    """⚠️ 2026-09-23 自审抓到的头号回归。

    删回读之后 ``SUBMITTED`` 是成功终态，但启动恢复还按旧口径把它算作「活跃
    guard」，于是**每次重启**都把每一笔成功提现的运行直接改写成
    ``UNCERTAIN_FINANCIAL``（事件 ``FINANCIAL_GUARD_STATUS_REPAIRED``），而收口又
    写不回去 —— 成功全变成红色的「待人工核对」。
    """

    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store = Store(
            name="Finished payout",
            selector_type="oauth",
            selector_value="oauth-finished",
            browser_oauth="oauth-finished",
            expected_seller_id="SELLER-FINISHED",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        marketplace = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        session.add(marketplace)
        session.flush()
        run_id = _add_guarded_run(session, store=store, marketplace=marketplace)
        session.get(Run, run_id).status = "SUCCEEDED"
        session.query(OperationGuard).filter_by(run_id=run_id).one().state = guard_state
        session.query(SiteRun).filter_by(run_id=run_id).one().status = guard_state
        session.commit()

    loader = DatabaseRunLoader(
        sessions,
        artifact_root=settings.evidence_dir,
        workflow_registry=registry,
    )
    plan = await loader.startup_recovery_plan()

    assert run_id not in plan.financial_guarded_ids
    assert run_id not in plan.reconciling_ids
    with sessions() as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert (
            session.query(RunEvent)
            .filter_by(run_id=run_id, event_type="FINANCIAL_GUARD_STATUS_REPAIRED")
            .count()
            == 0
        )


def _seed_store(session, name: str) -> tuple[Store, dict[str, StoreMarketplace]]:
    store = Store(
        name=name,
        selector_type="oauth",
        selector_value=f"oauth-{name}",
        browser_oauth=f"oauth-{name}",
        expected_seller_id="SELLER-X",
        identity_confirmed=True,
        enabled=True,
    )
    session.add(store)
    session.flush()
    markets = {}
    for code, domain, currency in (
        ("CA", "sellercentral.amazon.ca", "CAD"),
        ("UK", "sellercentral.amazon.co.uk", "GBP"),
    ):
        market = StoreMarketplace(
            store_id=store.id, code=code, domain=domain, currency=currency, enabled=True
        )
        session.add(market)
        markets[code] = market
    session.flush()
    return store, markets


@pytest.mark.parametrize("guard_state", ["SUBMITTED", "CONFIRMED"])
@pytest.mark.asyncio
async def test_the_queue_path_loader_does_not_repair_a_finished_payout(
    recovery_database, guard_state: str
) -> None:
    """``load_financial_recovery``(队列里的 RECONCILE/被改道的 START 走这条)也不许
    把 SUCCEEDED + 已派发 的运行改写成 UNCERTAIN_FINANCIAL。

    上面那条只走 ``startup_recovery_plan``,而快照过滤在前、修复条件在后,它根本
    看不到修复条件(09-23 自审复核:把修复条件改回旧集合,整套测试照样绿)。
    """

    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store, markets = _seed_store(session, f"queue-{guard_state}")
        run_id = _add_guarded_run(session, store=store, marketplace=markets["CA"])
        session.get(Run, run_id).status = "SUCCEEDED"
        session.query(OperationGuard).filter_by(run_id=run_id).one().state = guard_state
        session.query(SiteRun).filter_by(run_id=run_id).one().status = guard_state
        session.commit()

    loader = DatabaseRunLoader(
        sessions, artifact_root=settings.evidence_dir, workflow_registry=registry
    )
    await loader.load_financial_recovery(run_id)

    with sessions() as session:
        assert session.get(Run, run_id).status == "SUCCEEDED"
        assert (
            session.query(RunEvent)
            .filter_by(run_id=run_id, event_type="FINANCIAL_GUARD_STATUS_REPAIRED")
            .count()
            == 0
        )


@pytest.mark.asyncio
async def test_recovery_settle_sees_the_sites_that_have_no_money_record(
    recovery_database,
) -> None:
    """恢复加载器只按资金记录重建站点列表(这里只有 CA)。收口的终态却必须看这条
    运行的**全部**站点:UK 失败了,就不能报 SUCCEEDED、发绿卡。

    用真的 SQLAlchemy 仓储 + 真的加载器 + 真引擎走一遍(09-23 自审复核:原来
    ``_site_outcomes`` 按 ``run.marketplaces`` 逐个查,UK 根本不在里面)。
    """

    from ziniao_automation.workflows.amazon_disbursement import (
        AmazonDisbursementWorkflow,
    )
    from ziniao_automation.workflows.engine import WorkflowEngine
    from ziniao_automation.workflows.repository_sqlalchemy import (
        SqlAlchemyWorkflowRepository,
    )
    from ziniao_automation.workflows.types import RunStatus, utc_now

    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store, markets = _seed_store(session, "sibling-failed")
        run_id = _add_guarded_run(session, store=store, marketplace=markets["CA"])
        session.get(Run, run_id).status = "RUNNING"
        guard = session.query(OperationGuard).filter_by(run_id=run_id).one()
        guard.state = "SUBMITTED"
        # 生产上 transition_guard 写 SUBMITTED 时一定同时写 submitted_at。
        guard.submitted_at = utc_now()
        session.query(SiteRun).filter_by(run_id=run_id).one().status = "SUBMITTED"
        session.add(
            SiteRun(
                run_id=run_id,
                marketplace_id=markets["UK"].id,
                marketplace_code="UK",
                status="FAILED",
                currency="GBP",
            )
        )
        session.commit()

    loader = DatabaseRunLoader(
        sessions, artifact_root=settings.evidence_dir, workflow_registry=registry
    )
    run = await loader.load_financial_recovery(run_id)
    assert [m.code for m in run.marketplaces] == ["CA"], "前提:加载器只按资金记录重建"

    class NoBrowser:
        def financial_session(self, *args, **kwargs):
            raise AssertionError("收口不开浏览器")

    class Notes:
        def __init__(self) -> None:
            self.statuses = []

        async def send(self, report) -> None:
            self.statuses.append(report.status)

    repo = SqlAlchemyWorkflowRepository(sessions)
    notes = Notes()
    engine = WorkflowEngine(
        registry=WorkflowRegistry(
            (AmazonDisbursementWorkflow(repository=repo, page_adapter=object()),)
        ),
        repository=repo,
        browser_sessions=NoBrowser(),
        notifier=notes,
    )

    result = await engine.settle(run)

    assert result.status is RunStatus.PARTIAL
    assert notes.statuses == [RunStatus.PARTIAL]
    with sessions() as session:
        assert session.get(Run, run_id).status == "PARTIAL"


@pytest.mark.asyncio
async def test_a_wait_lost_to_a_restart_is_announced(recovery_database) -> None:
    """操作员手上最后一张卡是「等待登录验证，完成后回后台继续」。重启让那次验证
    作废了(运行转成 NEEDS_HUMAN_AUTH),必须再来一张卡告诉他 —— 否则他会去完成一个
    已经接不上的验证(09-23 自审复核:这条路径一张卡都不发)。"""

    settings, sessions = recovery_database
    registry = WorkflowRegistry((build_amazon_disbursement_definition(),))
    with sessions() as session:
        store, markets = _seed_store(session, "lease-lost")
        run = Run(
            store_id=store.id,
            workflow="amazon_disbursement",
            mode="auto",
            trigger="manual",
            status="WAITING_AUTH",
            requested_by="admin",
            workflow_config={"marketplace_codes": ["CA", "UK"]},
        )
        session.add(run)
        session.flush()
        # CA 在等验证,UK 还在读(没轮到)。两格都不能留在「等待验证」「读取中」。
        for code, status in (("CA", "WAITING_AUTH"), ("UK", "PREFLIGHT")):
            session.add(
                SiteRun(
                    run_id=run.id,
                    marketplace_id=markets[code].id,
                    marketplace_code=code,
                    status=status,
                )
            )
        session.commit()
        run_id = run.id

    class Deliveries:
        def __init__(self) -> None:
            self.auth_timeouts: list[str] = []

        async def notify_auth_timeout(self, run_id: str) -> bool:
            self.auth_timeouts.append(run_id)
            return True

    class Engine:
        registry = None
        repository = object()

    loader = DatabaseRunLoader(
        sessions, artifact_root=settings.evidence_dir, workflow_registry=registry
    )
    service = AutomationService(
        engine=Engine(),
        run_loader=loader,
        ziniao_controller=object(),
        session_factory=sessions,
    )
    deliveries = Deliveries()
    service.set_delivery_service(deliveries)
    try:
        await service.recover_startup()
        await service.wait_idle()
    finally:
        await service.shutdown_worker()

    with sessions() as session:
        assert session.get(Run, run_id).status == "NEEDS_HUMAN_AUTH"
        assert {
            site.marketplace_code: site.status
            for site in session.query(SiteRun).filter_by(run_id=run_id)
        } == {"CA": "NEEDS_HUMAN_AUTH", "UK": "NEEDS_HUMAN_AUTH"}
    assert deliveries.auth_timeouts == [run_id]
