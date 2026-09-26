"""空计划的任务状态由站点结果决定，不许一律报成功。

空计划本身分不清「这几站真的没钱可提」和「压根没读成」（被弹回登录页、页面
结构不符）。0.6.3 之前引擎对空计划一律置 SUCCEEDED，于是登录失败的空跑在
流水里显示成「成功（没钱可提）」——操作员看不出任何异常。

修正后的路由：任一站点 NEEDS_HUMAN_AUTH → 整个任务 NEEDS_HUMAN_AUTH；
任一站点 FAILED → PARTIAL；站点**全部跳过** → SKIPPED（蓝卡）；
其余（空跑完成 / 流程不维护站点行）仍是正常的成功空跑。

⚠️ 这段判定必须排在 DRY_RUN 短路**之前**——目前多数店铺刻意保持 dry_run
（memory「反馈流程当前状态」），排在后面等于把登录失败全部盖住。第一条
测试专门钉这个顺序。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from ziniao_automation.workflows.engine import WorkflowEngine
from ziniao_automation.workflows.registry import WorkflowRegistry
from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
from ziniao_automation.workflows.types import (
    MarketplaceRef,
    RunMode,
    RunStatus,
    SiteStatus,
    StoreRef,
    WorkflowPlan,
    WorkflowRun,
)


class EmptyPlanWorkflow:
    """preflight 把站点标成指定状态，plan 永远返回空计划。"""

    name = "empty_plan_probe"

    def __init__(
        self,
        repository: InMemoryWorkflowRepository,
        site_status: SiteStatus | None,
    ) -> None:
        self.repository = repository
        self.site_status = site_status

    async def preflight(self, run, page) -> None:
        if self.site_status is None:
            return
        for marketplace in run.marketplaces:
            await self.repository.set_site_status(
                run.id, marketplace.code, self.site_status
            )

    async def plan(self, run, page) -> WorkflowPlan:
        return WorkflowPlan(self.name, run.id, run.store.id, ())

    async def execute(self, run, page, plan):  # pragma: no cover
        raise AssertionError("空计划不该走到 execute")

    async def reconcile(self, run, page, pending):  # pragma: no cover
        raise AssertionError("本测试没有待对账操作")

    async def report(self, run, status):
        return None


class Sessions:
    @asynccontextmanager
    async def financial_session(self, selector, store_key=None):
        yield type("Handle", (), {"page": object()})()


def make_run(mode: RunMode, *, marketplaces: tuple[str, ...] = ("CA",)) -> WorkflowRun:
    return WorkflowRun(
        id=f"empty-plan-{mode.value}-{'-'.join(marketplaces) or 'none'}",
        workflow="empty_plan_probe",
        mode=mode,
        store=StoreRef(
            id="store-1",
            name="Store",
            selector_type="oauth",
            selector_value="selector",
            expected_seller_id="SELLER",
            identity_confirmed=True,
        ),
        marketplaces=tuple(
            MarketplaceRef(
                id=f"market-{code.lower()}",
                code=code,
                domain=f"sellercentral.amazon.{code.lower()}",
                currency="CAD",
            )
            for code in marketplaces
        ),
    )


async def run_engine(
    mode: RunMode,
    site_status: SiteStatus | None,
    *,
    marketplaces: tuple[str, ...] = ("CA",),
) -> RunStatus:
    repo = InMemoryWorkflowRepository()
    run = make_run(mode, marketplaces=marketplaces)
    await repo.add_run(run)
    workflow = EmptyPlanWorkflow(repo, site_status)
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=Sessions(),
    )
    result = await engine.start(run)
    return result.status


@pytest.mark.asyncio
async def test_dry_run_with_an_auth_blocked_site_is_not_a_success() -> None:
    """钉顺序的测试：dry_run 短路必须在空计划判定**之后**。

    dry_run + 登录失败 + 空计划——旧代码先走 dry_run 短路，报 SUCCEEDED。
    """

    assert await run_engine(RunMode.DRY_RUN, SiteStatus.NEEDS_HUMAN_AUTH) is (
        RunStatus.NEEDS_HUMAN_AUTH
    )


@pytest.mark.asyncio
async def test_a_failed_site_makes_the_empty_run_partial() -> None:
    assert await run_engine(RunMode.AUTO, SiteStatus.FAILED) is RunStatus.PARTIAL


@pytest.mark.asyncio
async def test_needs_human_auth_wins_over_failed() -> None:
    """两种都有时报 NEEDS_HUMAN_AUTH——那是唯一操作员能当场救的。"""

    repo = InMemoryWorkflowRepository()
    run = make_run(RunMode.AUTO, marketplaces=("CA", "UK"))
    await repo.add_run(run)
    workflow = EmptyPlanWorkflow(repo, None)

    async def preflight(run_, page) -> None:
        await repo.set_site_status(run_.id, "CA", SiteStatus.FAILED)
        await repo.set_site_status(run_.id, "UK", SiteStatus.NEEDS_HUMAN_AUTH)

    workflow.preflight = preflight
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=Sessions(),
    )
    assert (await engine.start(run)).status is RunStatus.NEEDS_HUMAN_AUTH


@pytest.mark.asyncio
async def test_all_sites_skipped_is_reported_as_skipped_not_completed() -> None:
    """站点全部跳过 → 任务 SKIPPED，**不是** SUCCEEDED。

    ⚠️ 这条测试原来钉的是 SUCCEEDED，理由写的是「不能变成告警」——而那个前提是错的：
    ``RUN_SKIPPED`` 是**刻意的蓝卡**（feishu ``_card_colour`` 的注释：红卡会把亚马逊
    正常的 24 小时限流训练成事故）。真正的后果是另一回事：2026-09-18 正香-张刚CA
    的德国站停在账户/站点选择页被跳过，**一个站点都没读成**，飞书卡标题却是绿色的
    「GPSR 安全证明已完成」。

    引擎的另一条路径 ``_finish_operations`` 一直对同一情形给 SKIPPED，两条路径本来
    就该一致；提现流程没暴露这个 bug，只是因为它读到余额后计划非空、走的是那一条。
    memory「跳过也要发通知」已经确立：全跳过的 run 必须发蓝卡。
    """

    assert await run_engine(RunMode.AUTO, SiteStatus.SKIPPED) is RunStatus.SKIPPED


@pytest.mark.asyncio
async def test_a_workflow_without_site_rows_keeps_its_successful_no_op() -> None:
    """不维护站点行的流程（marketplaces 为空）保持原行为。"""

    assert (
        await run_engine(RunMode.AUTO, None, marketplaces=())
        is RunStatus.SUCCEEDED
    )
