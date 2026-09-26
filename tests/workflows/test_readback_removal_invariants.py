"""删掉「资金回读核对」之后必须守住的几条。

运营 2026-09-23 的原话:「我基本用不上，反而平白多出挺多 bug 和资源消耗」。
面板上当时堆着 43 笔「已提交待平台审核」、44 笔「需要回读核对」,最老的一条是 09-10 的。
根因不是懒得处理 —— 代码库自己写下了结论(repositories.py:113-122):回读窗口约一分钟,
而亚马逊常隔天才把提现显示到结算页,**结构上解决不了**。

删掉之后最容易出事的两件:
1. 以为「防重提现」靠的是回读 —— 不是,靠的是 ``guard_key`` UNIQUE 的存在性;
2. 忘了改终态映射 —— 那样每一次**成功的**提现都会收在红色的 UNCERTAIN_FINANCIAL。
"""

from __future__ import annotations

import ast
from decimal import Decimal
import pathlib

import pytest

import ziniao_automation
from ziniao_automation.workflows.engine import run_status_from_facts
from ziniao_automation.workflows.types import GuardState, RunStatus, SiteStatus


SRC_ROOT = pathlib.Path(ziniao_automation.__file__).resolve().parent
SUBMITTED_CA = {"CA": SiteStatus.SUBMITTED}


def test_a_dispatched_payout_is_a_success_not_an_unknown() -> None:
    """⚠️ 这条是整次删除里最承重的一处。

    有回读的时候,查不到就写 UNCERTAIN —— 而亚马逊常隔天才显示,于是每一笔正常提现
    都会留下一条待办。删掉回读之后 ``SUBMITTED`` 是**正常终态**:那一下不可逆点击
    返回过,请求确实发出去了。

    不改这条映射的话,删完功能之后每一次成功提现都会收在红色的 UNCERTAIN_FINANCIAL,
    比删之前还糟 —— 待办没少,还全变成了报警。
    """

    assert (
        run_status_from_facts([("CA", GuardState.SUBMITTED)], SUBMITTED_CA)
        is RunStatus.SUCCEEDED
    )
    assert (
        run_status_from_facts(
            [("CA", GuardState.SUBMITTED), ("UK", GuardState.CONFIRMED)],
            {"CA": SiteStatus.SUBMITTED, "UK": SiteStatus.CONFIRMED},
        )
        is RunStatus.SUCCEEDED
    )


@pytest.mark.parametrize("state", [GuardState.ARMED, GuardState.UNCERTAIN])
def test_what_is_genuinely_unknown_still_reads_as_unknown(state: GuardState) -> None:
    """反向验证:上面那条不是靠把 UNCERTAIN_FINANCIAL 整个删掉来通过的。

    ``ARMED`` 意味着锁了但系统从没看到点击返回 —— 那是真的不知道,不能报成功。
    ``UNCERTAIN`` 是人工待裁定,以及删除之前留下的历史记录。
    """

    assert (
        run_status_from_facts([("CA", state)], SUBMITTED_CA)
        is RunStatus.UNCERTAIN_FINANCIAL
    )
    # 混在一起时,说不清的那条说了算。
    assert (
        run_status_from_facts(
            [("CA", GuardState.SUBMITTED), ("UK", state)],
            {"CA": SiteStatus.SUBMITTED, "UK": SiteStatus.UNCERTAIN_FINANCIAL},
        )
        is RunStatus.UNCERTAIN_FINANCIAL
    )


@pytest.mark.parametrize(
    "other",
    [
        SiteStatus.FAILED,
        SiteStatus.NEEDS_HUMAN_AUTH,
        SiteStatus.WAITING_AUTH,
        SiteStatus.WAITING_APPROVAL,
        # 还没轮到:服务在 CA 付完、UK/AU 开始之前被停掉
        SiteStatus.PENDING,
        SiteStatus.PREFLIGHT,
        SiteStatus.PLANNED,
        # 被人工「确认未发出，释放」之后,站点行留在这里、资金记录已删
        SiteStatus.UNCERTAIN_FINANCIAL,
        SiteStatus.CANCELLED,
    ],
)
def test_one_paid_site_does_not_make_an_unfinished_run_a_success(other) -> None:
    """一站付了、另一站没做完 → PARTIAL。**反着列**:只有 SKIPPED/DRY_RUN_COMPLETE
    或已派发的站点算做完,其余一律没做完(09-23 自审复核:原来只列了 4 种「没做完」,
    还没轮到的站点和被释放的站点都被算成了成功)。"""

    assert (
        run_status_from_facts(
            [("CA", GuardState.SUBMITTED)], {"CA": SiteStatus.SUBMITTED, "UK": other}
        )
        is RunStatus.PARTIAL
    )


@pytest.mark.parametrize("other", [SiteStatus.SKIPPED, SiteStatus.DRY_RUN_COMPLETE])
def test_a_site_skipped_for_zero_balance_does_not_spoil_a_success(other) -> None:
    """反向验证:余额为零被跳过的站点不是失败。"""

    assert (
        run_status_from_facts(
            [("AU", GuardState.CONFIRMED)], {"AU": SiteStatus.CONFIRMED, "CA": other}
        )
        is RunStatus.SUCCEEDED
    )


def test_an_acknowledged_site_counts_by_its_guard_not_its_row() -> None:
    """人工结案把记录改成 CONFIRMED,站点行仍留在 UNCERTAIN_FINANCIAL —— 按记录算做完。"""

    assert (
        run_status_from_facts(
            [("UK", GuardState.CONFIRMED)], {"UK": SiteStatus.UNCERTAIN_FINANCIAL}
        )
        is RunStatus.SUCCEEDED
    )


def test_no_source_file_calls_a_page_side_read_back_any_more() -> None:
    """按**类**扫源码,而不是逐个调用点断言。

    ``page_adapter.reconcile`` / ``self.page_adapter.reconcile`` 是「开浏览器去亚马逊
    结算页查这笔到没到」的唯一入口。以后有人想把它接回来,这条会红。

    适配器上那个 ``reconcile`` 方法已于 09-23 自审时删掉(没人调它了)。读结算页的
    代码(``_goto_statements`` / ``_find_payment``)留着,因为 ``lookup_existing`` 要用 ——
    那是**点击之前**的平台侧幂等检查,属于红线,见下面那条行为测试。
    """

    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "reconcile":
                continue
            # 只盯页面适配器上的那个;workflow.reconcile 现在是不联网的收口。
            owner = func.value
            name = getattr(owner, "attr", None) or getattr(owner, "id", None)
            if name == "page_adapter":
                offenders.append(f"{path.relative_to(SRC_ROOT)}:{node.lineno}")
    assert not offenders, (
        "回读已于 2026-09-23 删除，不要把它接回来:\n" + "\n".join(offenders)
    )


@pytest.mark.asyncio
async def test_settling_a_guarded_run_never_opens_a_browser() -> None:
    """⚠️ 这是整次删除最该守住的一条,和文案、面板都无关。

    有活资金记录的运行**不许重跑**,也不许为了收口去开紫鸟 —— 前者会二次提现,
    后者正是运营说的「平白多出的资源消耗」:每次服务启动都为一条 09-10 的旧记录
    开一次浏览器、失败、再排一次。

    ⚠️ 造场景的方式是承重的。第一版先跑一次 ``start()`` 再跑第二次,结果第二次是
    因为**运行已是 SUCCEEDED 终态**而从别的分支返回的 —— 把重入屏障整个注释掉,
    测试依然全绿。现在直接在仓储里造一条已派发记录、运行保持 ``QUEUED``,
    ``start()`` 就一定会撞上那道屏障。
    """

    from ziniao_automation.workflows.engine import WorkflowEngine
    from ziniao_automation.workflows.registry import WorkflowRegistry
    from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
    from ziniao_automation.workflows.types import OperationIntent, RunMode

    from .test_disbursement_recovery import FakePageAdapter, make_run
    from ziniao_automation.workflows.amazon_disbursement import (
        AmazonDisbursementWorkflow,
    )

    class NoBrowser:
        def financial_session(self, *args, **kwargs):
            raise AssertionError("收口不该开浏览器")

        def session(self, *args, **kwargs):
            raise AssertionError("收口不该开浏览器")

    repo = InMemoryWorkflowRepository()
    adapter = FakePageAdapter()
    run = make_run(RunMode.AUTO, "run-no-browser")
    await repo.add_run(run)

    marketplace = run.marketplaces[0]
    intent = OperationIntent(
        guard_key="guard-no-browser",
        run_id=run.id,
        site_run_key=f"{run.id}:{marketplace.code}",
        store_id=run.store.id,
        marketplace_code=marketplace.code,
        settlement_key="2026/9/1 - 至今",
        amount=Decimal("12.34"),
        currency="CAD",
        plan_hash="a" * 64,
        snapshot_hash="b" * 64,
    )
    await repo.arm_operation(intent)
    await repo.transition_operation(
        intent.guard_key,
        expected=(GuardState.ARMED,),
        target=GuardState.SUBMITTED,
        receipt_id="local-click",
    )

    workflow = AmazonDisbursementWorkflow(repository=repo, page_adapter=adapter)
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=NoBrowser(),
    )

    result = await engine.start(run)

    assert result.status is RunStatus.SUCCEEDED
    assert adapter.submit_calls == 0, "绝不能去点提现"
    assert adapter.reconcile_calls == 0
    assert (await repo.list_operations(run.id))[0].state is GuardState.SUBMITTED


@pytest.mark.asyncio
async def test_a_legacy_uncertain_record_is_left_for_the_human() -> None:
    """已经在等人裁定的记录,收口原样返回,**站点状态一个字都不改**。

    09-10 留下的那条 UK 记录是 ``UNCERTAIN``、站点是 ``UNCERTAIN_FINANCIAL``。
    收口原本对每条记录无条件把站点写成 ``SUBMITTED`` —— 那等于替人宣布
    「这笔已发出」,把「待人工核对」悄悄抹成成功。

    ⚠️ 纠正 09-23 我自己的一次误判:当时以为删除后第一次重启炸在这里的
    ``InvalidTransition`` 上。**不是**——两个仓储的 ``set_site_status`` 都不校验
    站点状态机,写得进去。真正炸的是 ``WorkflowDispatcher`` 上没有 ``settle``
    (``AttributeError``),见下面那条调度器扫描。
    """

    from decimal import Decimal as _Decimal

    from ziniao_automation.workflows.amazon_disbursement import (
        AmazonDisbursementWorkflow,
    )
    from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
    from ziniao_automation.workflows.types import (
        OperationIntent,
        RunMode,
        SiteStatus,
    )

    from .test_disbursement_recovery import FakePageAdapter, make_run

    repo = InMemoryWorkflowRepository()
    run = make_run(RunMode.AUTO, "run-legacy-uncertain")
    await repo.add_run(run)
    marketplace = run.marketplaces[0]

    intent = OperationIntent(
        guard_key="guard-legacy",
        run_id=run.id,
        site_run_key=f"{run.id}:{marketplace.code}",
        store_id=run.store.id,
        marketplace_code=marketplace.code,
        settlement_key="2026/9/1 - 至今",
        amount=_Decimal("12.34"),
        currency="CAD",
        plan_hash="a" * 64,
        snapshot_hash="b" * 64,
    )
    await repo.arm_operation(intent)
    await repo.transition_operation(
        intent.guard_key,
        expected=(GuardState.ARMED,),
        target=GuardState.SUBMITTED,
        receipt_id="local-click",
    )
    await repo.transition_operation(
        intent.guard_key,
        expected=(GuardState.SUBMITTED,),
        target=GuardState.UNCERTAIN,
        details={"reason": "已提交提现请求，回读时亚马逊尚未显示结果"},
    )
    await repo.set_site_status(
        run.id, marketplace.code, SiteStatus.UNCERTAIN_FINANCIAL
    )

    workflow = AmazonDisbursementWorkflow(
        repository=repo, page_adapter=FakePageAdapter()
    )

    settled = await workflow.reconcile(run, None)

    assert [item.state for item in settled] == [GuardState.UNCERTAIN]
    assert (
        await repo.get_site_status(run.id, marketplace.code)
        is SiteStatus.UNCERTAIN_FINANCIAL
    ), "待人工核对的站点不能被收口改写成已提交"


def test_the_dispatcher_forwards_every_engine_entry_point_it_claims_to() -> None:
    """调度器转发的每个方法，引擎上都必须真的有。

    ⚠️ 2026-09-23 实测漏过一次:引擎的 ``reconcile`` 改名成 ``settle``，
    ``WorkflowDispatcher`` 那一层忘了跟着改。离线单测全绿(它们直接用引擎)，
    真机上每次服务启动都抛
    ``AttributeError: 'WorkflowDispatcher' object has no attribute 'settle'``，
    被 worker 吞掉、队列条目标 DONE、只在日志里留一行。

    这类「多一层转发、改名时漏一层」的缺陷不该靠人眼扫，所以按**类**钉:
    调度器上每个 ``self.engine_for(...).X`` 里的 X，``WorkflowEngine`` 上必须有。
    """

    from ziniao_automation.workflows.engine import WorkflowEngine

    source = (SRC_ROOT / "workflows" / "dispatcher.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forwarded: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        inner = node.func.value
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "engine_for"
        ):
            forwarded.add(node.func.attr)

    assert forwarded, "没扫到任何转发，扫描器本身坏了"
    missing = sorted(name for name in forwarded if not hasattr(WorkflowEngine, name))
    assert not missing, f"调度器转发了引擎没有的方法: {missing}"


def test_every_engine_call_in_the_runtime_exists_on_the_dispatcher() -> None:
    """反方向:运行时里每个 ``self.engine.X``,调度器上都必须有 X。

    ⚠️ 上面那条只查「调度器转发的,引擎上有没有」—— 它**抓不住** 09-23 那次:
    运行时调的是 ``self.engine.settle``,而调度器根本没转发 ``settle``,
    上面那条扫到的集合里压根没有它。生产上 ``self.engine`` 是 ``WorkflowDispatcher``。
    """

    from ziniao_automation.workflows.dispatcher import WorkflowDispatcher

    runtime = ast.parse(
        (SRC_ROOT / "workflows" / "runtime.py").read_text(encoding="utf-8")
    )
    used: set[str] = set()
    for node in ast.walk(runtime):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "engine"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        ):
            used.add(node.attr)
        # getattr(self.engine, "execute_approved", None) 这种写法同样是在调调度器;
        # 只认属性访问会漏掉它(09-23 自审复核:删掉 WorkflowDispatcher.execute_approved
        # 这条照样绿,而审批过的提现会一次都不执行)。
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "engine"
            and isinstance(node.args[0].value, ast.Name)
            and node.args[0].value.id == "self"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            used.add(node.args[1].value)

    dispatcher = ast.parse(
        (SRC_ROOT / "workflows" / "dispatcher.py").read_text(encoding="utf-8")
    )
    instance_attributes = {
        target.attr
        for node in ast.walk(dispatcher)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }

    assert {"settle", "execute_approved"} <= used, "扫描器没扫全,它本身坏了"
    missing = sorted(
        name
        for name in used
        if not hasattr(WorkflowDispatcher, name) and name not in instance_attributes
    )
    assert not missing, f"运行时调用了调度器上没有的方法: {missing}"


@pytest.mark.asyncio
async def test_the_platform_side_idempotency_check_runs_before_any_click() -> None:
    """``lookup_existing`` 长得像回读,但它在**任何点击之前**跑,是红线。

    它同样返回 ``ReconcileResult``、同样去读 /payments/allstatements/,所以极易被
    当成回读一起删掉。这里钉**行为**,不钉符号:用真的 ``open_confirmation``,
    亚马逊那边今天已有同额记录时,走不到面板上那颗按钮;没有时,先查、再往下走。
    """

    from ziniao_automation.workflows.amazon_disbursement import AmazonPaymentsPage
    from ziniao_automation.workflows.errors import PlatformDisbursementExists
    from ziniao_automation.workflows.types import (
        ReconcileResult,
        ReconcileStatus,
        RunMode,
        utc_now,
    )

    from .test_disbursement_recovery import make_run, make_snapshot

    class _PastTheCheck(Exception):
        """走过了幂等检查,再往下就是面板上的点击。"""

    class Page:
        url = "https://sellercentral.amazon.ca/payments/dashboard/index.html"

    class Adapter(AmazonPaymentsPage):
        def __init__(self, status: ReconcileStatus) -> None:
            super().__init__(pacing_range_ms=(0, 0))
            self._status = status
            self.calls: list[str] = []

        async def lookup_existing(self, page, run, marketplace, expected):
            self.calls.append("lookup_existing")
            return ReconcileResult(self._status, utc_now())

        async def read_snapshot(self, page, run, marketplace):
            self.calls.append("toward_the_click")
            raise _PastTheCheck()

    run = make_run(RunMode.AUTO, "run-idempotency")
    marketplace = run.marketplaces[0]

    for status in (
        ReconcileStatus.CONFIRMED,
        ReconcileStatus.PENDING,
        ReconcileStatus.CONFLICT,
    ):
        adapter = Adapter(status)
        with pytest.raises(PlatformDisbursementExists):
            await adapter.open_confirmation(Page(), run, marketplace, make_snapshot())
        assert adapter.calls == ["lookup_existing"], status

    adapter = Adapter(ReconcileStatus.NOT_FOUND)
    with pytest.raises(_PastTheCheck):
        await adapter.open_confirmation(Page(), run, marketplace, make_snapshot())
    assert adapter.calls == ["lookup_existing", "toward_the_click"]


@pytest.mark.asyncio
async def test_a_payout_amazon_already_shows_skips_the_site_without_arming() -> None:
    """流程侧:平台已有同日同额记录 → 这一站跳过,不 arm、不点。"""

    from ziniao_automation.workflows.amazon_disbursement import (
        AmazonDisbursementWorkflow,
    )
    from ziniao_automation.workflows.errors import PlatformDisbursementExists
    from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
    from ziniao_automation.workflows.types import (
        ReconcileResult,
        ReconcileStatus,
        RunMode,
        SiteStatus,
        utc_now,
    )

    from .test_disbursement_recovery import FakePageAdapter, make_run

    class Adapter(FakePageAdapter):
        async def open_confirmation(self, page, run, marketplace, expected):
            raise PlatformDisbursementExists(
                "付款记录已存在同日相同金额/周期，未打开确认页",
                result=ReconcileResult(ReconcileStatus.CONFIRMED, utc_now()),
            )

    repo = InMemoryWorkflowRepository()
    run = make_run(RunMode.AUTO, "run-platform-duplicate")
    await repo.add_run(run)
    adapter = Adapter()
    workflow = AmazonDisbursementWorkflow(repository=repo, page_adapter=adapter)
    plan = await workflow.plan(run, object())

    await workflow.execute(run, object(), plan)

    assert adapter.submit_calls == 0
    assert await repo.list_operations(run.id) == ()
    assert await repo.get_site_status(run.id, "CA") is SiteStatus.SKIPPED
