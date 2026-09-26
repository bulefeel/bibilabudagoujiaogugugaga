"""2026-09-23 自审：删掉资金回读之后「SUBMITTED 算不算没结论」散在 8 处，只改了 1 处。

删回读之前 ``ARMED/SUBMITTED/UNCERTAIN`` 一起叫「活跃 guard」，一个集合同时表达两件事：

1. 这条运行**动过钱**，绝不能从头重跑 —— 现在看「有没有任何 guard」；
2. 这笔**还没结论**，需要人管 —— 现在是 ``UNSETTLED_GUARD_STATES``（ARMED/UNCERTAIN）。

SUBMITTED 属于 1、不属于 2。下面每一条都钉一处按旧意思跑会出的事，
而且每一条都在「把修复改回去」时验证过会红。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal

import pytest

from ziniao_automation.workflows.amazon_disbursement import (
    AmazonDisbursementWorkflow,
)
from ziniao_automation.workflows.engine import WorkflowEngine
from ziniao_automation.ziniao.errors import AuthWaitCancelled, AuthWaitExpired
from ziniao_automation.workflows.registry import WorkflowRegistry
from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
from ziniao_automation.workflows.types import (
    GuardState,
    OperationIntent,
    RunMode,
    RunStatus,
    SiteStatus,
)

from .test_auth_cursor import CursorAdapter, make_multi_site_run
from .test_disbursement_recovery import Collector


class _Handle:
    page = object()


class _HumanFinishes:
    """人工验证立刻完成的浏览器会话：``wait_for_auth`` 直接返回。"""

    def __init__(self) -> None:
        self.waits = 0

    @asynccontextmanager
    async def financial_session(self, selector, store_key=None):
        yield _Handle()

    async def wait_for_auth(self, handle, auth_key, *, timeout_seconds=1800):
        self.waits += 1
        return handle

    async def continue_auth(self, auth_key):
        return False

    async def cancel_auth(self, auth_key):
        return False


def _engine(repo, adapter, sessions=None, notifier=None):
    workflow = AmazonDisbursementWorkflow(repository=repo, page_adapter=adapter)
    return WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=sessions or _HumanFinishes(),
        notifier=notifier,
    ), workflow


async def _seed_guard(repo, run, code: str, *, key: str, final: GuardState) -> None:
    """在仓储里直接造一条本运行的资金记录，停在 ``final``。"""

    marketplace = next(m for m in run.marketplaces if m.code == code)
    intent = OperationIntent(
        guard_key=key,
        run_id=run.id,
        site_run_key=f"{run.id}:{code}",
        store_id=run.store.id,
        marketplace_code=code,
        settlement_key=f"cycle-{code}",
        amount=Decimal("10.00"),
        currency=marketplace.currency,
        plan_hash="a" * 64,
        snapshot_hash="b" * 64,
    )
    await repo.arm_operation(intent)
    if final is GuardState.ARMED:
        return
    await repo.transition_operation(
        key, expected=(GuardState.ARMED,), target=GuardState.SUBMITTED,
        receipt_id=f"click-{code}",
    )
    if final is GuardState.CONFIRMED:
        await repo.transition_operation(
            key, expected=(GuardState.SUBMITTED,), target=GuardState.CONFIRMED,
        )
    elif final is GuardState.UNCERTAIN:
        await repo.transition_operation(
            key, expected=(GuardState.SUBMITTED,), target=GuardState.UNCERTAIN,
        )


# ---------------------------------------------------------------------------
# 引擎：续跑、异常、收口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_resume_after_auth_finishes_the_remaining_sites() -> None:
    """审批模式：CA 已提交、UK 要人工验证 → 验证完**把 UK、AU 做完**，CA 不再点。

    旧口径下「有 SUBMITTED」= 有待收口记录 → 验证完只收口不续跑，UK/AU 已批准的
    金额一次没试，运行却报成功。
    """

    repo = InMemoryWorkflowRepository()
    run = replace(make_multi_site_run(), mode=RunMode.APPROVAL)
    await repo.add_run(run)
    adapter = CursorAdapter()  # UK 第一次 open_confirmation 要人工验证
    sessions = _HumanFinishes()
    engine, _ = _engine(repo, adapter, sessions)

    prepared = await engine.start(run)
    assert prepared.status is RunStatus.WAITING_APPROVAL
    adapter.open_calls.clear()

    result = await engine.approve(run)

    assert sessions.waits == 1, "UK 那一站应该停下来等人"
    assert adapter.submit_calls == ["CA", "UK", "AU"], "CA 只点一次，UK/AU 验证完照常做"
    assert result.status is RunStatus.SUCCEEDED
    assert len(await repo.list_operations(run.id)) == 3


@pytest.mark.asyncio
async def test_an_exception_after_a_submitted_site_is_partial_not_unknown() -> None:
    """CA 已提交之后 UK 抛了运行时异常：CA 有结论，是后面的站点没做完 —— PARTIAL。

    旧口径下 SUBMITTED 也算「说不清」，整条运行被钉成 UNCERTAIN_FINANCIAL，
    操作员被叫去核对一笔明明已经发出去的提现。
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()

    async def uk_blows_up(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        if marketplace.code == "UK":
            raise RuntimeError("进程级故障")
        return replace(expected, payment_account="493")

    adapter.open_confirmation = uk_blows_up
    engine, _ = _engine(repo, adapter)

    with pytest.raises(RuntimeError, match="进程级故障"):
        await engine.start(run)

    assert adapter.submit_calls == ["CA"]
    assert await repo.get_run_status(run.id) is RunStatus.PARTIAL


@pytest.mark.asyncio
async def test_a_run_wrongly_flipped_to_uncertain_settles_back_to_success_once() -> None:
    """已被旧 bug 翻成 UNCERTAIN_FINANCIAL、其实全部 SUBMITTED 的运行：
    收口把它落回 SUCCEEDED，并且只通知一次。"""

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    for code in ("CA", "UK", "AU"):
        await _seed_guard(repo, run, code, key=f"g-{code}", final=GuardState.SUBMITTED)
        await repo.set_site_status(run.id, code, SiteStatus.SUBMITTED)
    await repo.set_run_status(run.id, RunStatus.RUNNING)
    await repo.set_run_status(run.id, RunStatus.UNCERTAIN_FINANCIAL)
    collector = Collector()
    engine, _ = _engine(repo, CursorAdapter(), notifier=collector)

    first = await engine.settle(run)
    second = await engine.settle(run)

    assert first.status is RunStatus.SUCCEEDED
    assert second.status is RunStatus.SUCCEEDED
    assert [report.status for report in collector.reports] == [RunStatus.SUCCEEDED]


@pytest.mark.asyncio
async def test_a_genuinely_unknown_run_is_left_where_it_is_and_not_renotified() -> None:
    """反向验证：真的说不清（ARMED 没派发）时，收口不动状态、不重发通知。"""

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    await _seed_guard(repo, run, "CA", key="g-CA", final=GuardState.ARMED)
    await repo.set_run_status(run.id, RunStatus.RUNNING)
    await repo.set_run_status(run.id, RunStatus.UNCERTAIN_FINANCIAL)
    collector = Collector()
    engine, _ = _engine(repo, CursorAdapter(), notifier=collector)

    result = await engine.settle(run)

    assert result.status is RunStatus.UNCERTAIN_FINANCIAL
    assert collector.reports == []


@pytest.mark.asyncio
async def test_a_run_holding_only_confirmed_records_reaches_a_final_state() -> None:
    """只剩 CONFIRMED 记录的 RUNNING 运行必须落终态。

    第一版 settle 只在「有待收口记录」时才动，这种运行就永远卡在 RUNNING ——
    挡住立即执行、删排期和建档重置。
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    for code in ("CA", "UK", "AU"):
        await _seed_guard(repo, run, code, key=f"g-{code}", final=GuardState.CONFIRMED)
        await repo.set_site_status(run.id, code, SiteStatus.CONFIRMED)
    await repo.set_run_status(run.id, RunStatus.RUNNING)
    engine, _ = _engine(repo, CursorAdapter())

    result = await engine.settle(run)

    assert result.status is RunStatus.SUCCEEDED
    assert await repo.get_run_status(run.id) is RunStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# 流程：逐站点防重
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_site_this_run_already_paid_is_never_armed_again_across_midnight() -> None:
    """续跑跨过午夜：今天的 ``guard_key`` 变了，只查今天的 key 就会给同一站点
    重新 arm、再点一次提现。必须按**本运行**有没有该站点的记录来拦。"""

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()
    adapter.open_confirmation = _open_without_auth(adapter)
    _, workflow = _engine(repo, adapter)

    # 「昨天」的 key：和今天 execute 会算出来的 key 不同。
    await _seed_guard(repo, run, "CA", key="yesterday-CA", final=GuardState.SUBMITTED)
    plan = await workflow.plan(run, object())

    operations = await workflow.execute(run, object(), plan)

    assert "CA" not in adapter.submit_calls, "本运行已经点过 CA，绝不能再点"
    assert adapter.submit_calls == ["UK", "AU"]
    keys = sorted(op.intent.guard_key for op in await repo.list_operations(run.id))
    assert len(keys) == 3 and "yesterday-CA" in keys
    assert {op.intent.marketplace_code for op in operations} == {"CA", "UK", "AU"}


def _open_without_auth(adapter: CursorAdapter):
    async def opener(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        return replace(expected, payment_account="493")

    return opener


# ---------------------------------------------------------------------------
# 人工验证落空(超时 / 取消),但已有站点提交成功
# ---------------------------------------------------------------------------


class _HumanNeverFinishes(_HumanFinishes):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def wait_for_auth(self, handle, auth_key, *, timeout_seconds=1800):
        self.waits += 1
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "parked_site"),
    [
        (AuthWaitExpired("超时"), SiteStatus.NEEDS_HUMAN_AUTH),
        (AuthWaitCancelled("取消"), SiteStatus.CANCELLED),
    ],
)
async def test_a_wait_that_falls_through_after_a_payout_ends_partial(
    error, parked_site
) -> None:
    """CA 已提交、UK 等人没等到:CA 有结论,是后面的站点没做完 —— PARTIAL,
    不是「结果不明」。

    ⚠️ 旧口径下这里一律 UNCERTAIN_FINANCIAL(红卡叫人去核对一笔明明发出去了的钱),
    取消时还一张卡都不发。另外,等人的那一站和还没轮到的站点要落到「没做」的终态 ——
    否则终态运行的卡片上还写着「等待验证」「等待审核」(09-23 自审复核)。
    """

    repo = InMemoryWorkflowRepository()
    run = replace(make_multi_site_run(), mode=RunMode.APPROVAL)
    await repo.add_run(run)
    adapter = CursorAdapter()  # UK 的 open_confirmation 要人工验证
    collector = Collector()
    engine, _ = _engine(repo, adapter, _HumanNeverFinishes(error), collector)

    assert (await engine.start(run)).status is RunStatus.WAITING_APPROVAL
    await engine.approve(run)

    assert adapter.submit_calls == ["CA"]
    assert await repo.get_run_status(run.id) is RunStatus.PARTIAL
    statuses = [report.status for report in collector.reports]
    assert statuses.count(RunStatus.PARTIAL) == 1
    assert RunStatus.UNCERTAIN_FINANCIAL not in statuses
    sites = await repo.list_site_statuses(run.id)
    assert sites["CA"] is SiteStatus.SUBMITTED
    assert sites["UK"] is parked_site
    assert sites["AU"] is parked_site


class _ProcessDies(_HumanFinishes):
    """模拟服务在人工验证等待期间被停掉:等待永远不会正常返回。"""

    async def wait_for_auth(self, handle, auth_key, *, timeout_seconds=1800):
        raise SystemExit("service stopped")


@pytest.mark.asyncio
async def test_a_restart_during_a_wait_closes_the_waiting_sites_when_it_settles() -> None:
    """重启后的收口(RECONCILE → settle)也必须把「等验证 / 等审核」的站点落到终态。

    ⚠️ 第二轮只修了进程内的超时/取消;重启走的是 settle,终态 PARTIAL 的卡片上
    还写着 UK「等待验证」、AU「等待审核」(09-23 第二轮复核,真组件复现过)。
    """

    repo = InMemoryWorkflowRepository()
    run = replace(make_multi_site_run(), mode=RunMode.APPROVAL)
    await repo.add_run(run)
    adapter = CursorAdapter()  # UK 要人工验证
    engine, _ = _engine(repo, adapter, _ProcessDies())
    assert (await engine.start(run)).status is RunStatus.WAITING_APPROVAL
    with pytest.raises(SystemExit):
        await engine.approve(run)
    sites = await repo.list_site_statuses(run.id)
    assert sites["UK"] is SiteStatus.WAITING_AUTH, "前提:停在等验证"

    restarted, _ = _engine(repo, CursorAdapter(), _HumanFinishes(), Collector())
    result = await restarted.settle(run)

    assert result.status is RunStatus.PARTIAL
    sites = await repo.list_site_statuses(run.id)
    assert sites["CA"] is SiteStatus.SUBMITTED
    assert sites["UK"] is SiteStatus.CANCELLED
    assert sites["AU"] is SiteStatus.CANCELLED


@pytest.mark.asyncio
async def test_a_failure_after_a_payout_leaves_no_site_waiting_on_a_finished_run() -> None:
    """审批模式:CA 已提交,UK 在确认页前发现计划变了(PlanChanged 在审批模式里会
    向上抛)。运行落 PARTIAL,UK/AU 不能还挂着「等待审核」—— 审批已经作废,
    没有任何东西可批了(09-23 第三轮复核,真组件复现过)。"""

    from ziniao_automation.workflows.errors import PlanChanged

    repo = InMemoryWorkflowRepository()
    run = replace(make_multi_site_run(), mode=RunMode.APPROVAL)
    await repo.add_run(run)
    adapter = CursorAdapter()

    async def uk_plan_changed(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        if marketplace.code == "UK":
            raise PlanChanged("收款账户变了")
        return replace(expected, payment_account="493")

    adapter.open_confirmation = uk_plan_changed
    engine, _ = _engine(repo, adapter, _HumanFinishes(), Collector())
    assert (await engine.start(run)).status is RunStatus.WAITING_APPROVAL

    with pytest.raises(PlanChanged):
        await engine.approve(run)

    assert await repo.get_run_status(run.id) is RunStatus.PARTIAL
    sites = await repo.list_site_statuses(run.id)
    assert sites["CA"] is SiteStatus.SUBMITTED
    assert sites["UK"] is SiteStatus.CANCELLED
    assert sites["AU"] is SiteStatus.CANCELLED


@pytest.mark.asyncio
async def test_a_cancelled_run_leaves_no_site_waiting_for_approval() -> None:
    repo = InMemoryWorkflowRepository()
    run = replace(make_multi_site_run(), mode=RunMode.APPROVAL)
    await repo.add_run(run)
    engine, _ = _engine(repo, CursorAdapter(), _HumanFinishes(), Collector())
    assert (await engine.start(run)).status is RunStatus.WAITING_APPROVAL

    assert await engine.cancel(run) is True

    sites = await repo.list_site_statuses(run.id)
    assert set(sites.values()) == {SiteStatus.CANCELLED}


class _RecordsPublishOrder(_HumanFinishes):
    """照 ZiniaoController 的语义:租约在 wait_for_auth 里才建,且支持 on_waiting。"""

    def __init__(self) -> None:
        super().__init__()
        self.published_before_lease: bool | None = None
        self.lease_open = False

    async def wait_for_auth(self, handle, auth_key, *, timeout_seconds=1800, on_waiting=None):
        self.waits += 1
        self.lease_open = True          # ← 租约在这里才存在
        if on_waiting is not None:
            await on_waiting()
        return handle


@pytest.mark.asyncio
async def test_waiting_auth_is_published_only_after_the_lease_exists() -> None:
    """⚠️ 顺序是承重的。状态一写,运行详情页两秒一轮询就亮出「继续」按钮;而租约
    是 ``wait_for_auth`` 内部才注册的。先发布的话,这中间(要等一次飞书 HTTP,
    超时 10 秒)操作员点下去会被当成「进程重启过」,运行永久卡在 QUEUED、没有队列
    条目、页面上一个按钮都没有(2026-09-26 自审实测)。
    """

    repo = InMemoryWorkflowRepository()
    run = replace(make_multi_site_run(), mode=RunMode.APPROVAL)
    await repo.add_run(run)
    sessions = _RecordsPublishOrder()
    seen: list[bool] = []

    original = repo.set_run_status

    async def watch(run_id, status, **kwargs):
        if status is RunStatus.WAITING_AUTH:
            seen.append(sessions.lease_open)
        return await original(run_id, status, **kwargs)

    engine, _ = _engine(repo, CursorAdapter(), sessions)  # UK 要人工验证
    assert (await engine.start(run)).status is RunStatus.WAITING_APPROVAL
    repo.set_run_status = watch   # 审批阶段不等人,从执行阶段开始盯

    await engine.approve(run)

    assert sessions.waits == 1, "前提:UK 那一站确实停下来等过人"
    assert seen == [True], "WAITING_AUTH 必须在租约建好之后才发布"


@pytest.mark.asyncio
async def test_an_auth_timeout_keeps_what_other_sites_already_decided() -> None:
    """超时只收「还在等 / 还没轮到」的站点。原来把所有站点重写一遍,已经读完的
    站点(余额为零跳过)连原因一起被抹成「验证超时」(2026-09-26 自审)。"""

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    await repo.set_site_status(run.id, "CA", SiteStatus.SKIPPED, error="标准订单可用资金为零")
    await repo.set_site_status(run.id, "UK", SiteStatus.WAITING_AUTH)
    await repo.set_run_status(run.id, RunStatus.RUNNING)
    await repo.set_run_status(run.id, RunStatus.WAITING_AUTH)
    engine, _ = _engine(repo, CursorAdapter(), notifier=Collector())

    assert await engine.expire_auth(run) is True

    sites = await repo.list_site_statuses(run.id)
    assert sites["CA"] is SiteStatus.SKIPPED, "已经有结论的站点不许被改写"
    assert sites["UK"] is SiteStatus.NEEDS_HUMAN_AUTH


@pytest.mark.asyncio
async def test_a_site_dropped_from_the_plan_does_not_stay_waiting_for_approval() -> None:
    """审批期间站点被停用 → 执行时它不在 run.marketplaces 里 → 原来永远停在
    「等待审核」,而运行早就结束了(2026-09-26 自审)。"""

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    await repo.set_site_status(run.id, "UK", SiteStatus.WAITING_APPROVAL)
    adapter = CursorAdapter()
    adapter.open_confirmation = _open_without_auth(adapter)
    engine, workflow = _engine(repo, adapter)
    shrunk = replace(run, marketplaces=tuple(m for m in run.marketplaces if m.code != "UK"))
    await repo.set_run_status(run.id, RunStatus.RUNNING)
    plan = await workflow.plan(shrunk, object())

    await engine._finish_operations(shrunk, workflow, await workflow.execute(shrunk, object(), plan))

    assert (await repo.list_site_statuses(run.id))["UK"] is SiteStatus.CANCELLED
