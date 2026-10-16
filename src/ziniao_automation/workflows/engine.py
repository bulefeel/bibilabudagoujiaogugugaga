"""Application service that owns invocation, human-auth waits and recovery."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import logging
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
    TypeVar,
)

from .contracts import FinancialSessionProvider, Notifier, WorkflowRepository
from .errors import HumanAuthRequired, PageRecoveryRequired
from .registry import WorkflowRegistry
from .types import (
    ApprovalStatus,
    ExecutionResult,
    GuardState,
    PrepareResult,
    RunMode,
    RunStatus,
    SiteStatus,
    UNSETTLED_GUARD_STATES,
    WorkflowPlan,
    WorkflowReport,
    WorkflowRun,
    utc_now,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class NullNotifier:
    async def send(self, report: WorkflowReport) -> None:
        del report


class WorkflowEngine:
    """Run code-registered plugins with a fail-closed financial invariant.

    Once a local operation reaches ``ARMED``, every entry point chooses
    reconciliation.  No restart, approval retry or auth continuation can call
    the destructive workflow step again.
    """

    def __init__(
        self,
        *,
        registry: WorkflowRegistry,
        repository: WorkflowRepository,
        browser_sessions: FinancialSessionProvider,
        notifier: Notifier | None = None,
        approval_ttl: timedelta = timedelta(hours=2),
        auth_timeout_seconds: float = 1800.0,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.browser_sessions = browser_sessions
        self.notifier = notifier or NullNotifier()
        self.approval_ttl = approval_ttl
        self.auth_timeout_seconds = auth_timeout_seconds

    async def start(self, run: WorkflowRun) -> PrepareResult | ExecutionResult:
        # 整条运行的重入屏障:只要动过钱(有**任何** guard),就绝不从头重跑 ——
        # 那等于在一次重启或一次过期的「继续」之后重新走一遍提现。只收口,不开浏览器。
        if await self.repository.list_operations(run.id):
            return await self.settle(run)
        claimed = await self.repository.set_run_status(
            run.id, RunStatus.RUNNING, allowed_from=(RunStatus.QUEUED,)
        )
        if not claimed:
            status = await self.repository.get_run_status(run.id)
            if status in (RunStatus.UNCERTAIN_FINANCIAL, RunStatus.RECONCILING):
                return await self.settle(run)
            saved = await self.repository.get_plan(run.id)
            if saved is None:
                raise RuntimeError(f"任务 {run.id} 当前为 {status.value}，不可重复启动")
            return PrepareResult(run.id, status, saved)

        workflow = self.registry.get(run.workflow)
        try:
            if run.workflow == "amazon_inventory_gpsr_attestation":
                begin_attempt = getattr(workflow, "begin_attempt", None)
                if callable(begin_attempt):
                    begin_attempt(run.id)
            async with self._financial_session(run) as handle:
                completed, result = await self._with_auth_wait(
                    run,
                    workflow,
                    handle,
                    resume_status=RunStatus.RUNNING,
                    action=lambda: self._start_in_session(run, workflow, handle.page),
                )
                if completed:
                    return result
                saved = await self.repository.get_plan(run.id)
                return PrepareResult(
                    run.id,
                    await self.repository.get_run_status(run.id),
                    saved or WorkflowPlan(run.workflow, run.id, run.store.id, ()),
                )
        except PageRecoveryRequired as exc:
            if run.workflow != "amazon_inventory_gpsr_attestation":
                await self._record_failure(run, workflow, exc)
                raise
            changed = await self.repository.set_run_status(
                run.id, RunStatus.PAUSED, allowed_from=(RunStatus.RUNNING,),
                error=f"{exc.public_reason}，进度已保留，等待手动继续",
            )
            if changed or await self.repository.get_run_status(run.id) is RunStatus.PAUSED:
                await self._notify(workflow, run, RunStatus.PAUSED)
            return ExecutionResult(run.id, await self.repository.get_run_status(run.id))
        except Exception as exc:
            await self._record_failure(run, workflow, exc)
            raise

    async def _start_in_session(
        self, run: WorkflowRun, workflow: Any, page: Any
    ) -> PrepareResult | ExecutionResult:
        # 人工验证之后这里会再跑一次。只有**说不清**的记录(ARMED/UNCERTAIN)才
        # 停下来只收口 —— 不知道点没点下去,就一下都不能再点。已经 SUBMITTED 的站点
        # 不拦:后面 execute 的逐站点防重会跳过它,其余站点照常做完。
        unsettled = await self._unsettled_operations(run.id)
        if unsettled:
            operations = await workflow.reconcile(run, None, unsettled)
            return await self._finish_operations(run, workflow, operations)

        await workflow.preflight(run, page)
        plan = await workflow.plan(run, page)
        await self.repository.save_plan(plan)
        if not plan.lines:
            # 空计划本身什么都不说明：可能是「这几站真的没钱可提」，也可能是
            # 压根没读成（被弹回登录页、页面结构不符）。只有站点结果能区分，
            # 所以状态由站点结果决定 —— 绝不把登录失败报成「没钱可提」。
            #
            # ⚠️ 这一段必须排在 DRY_RUN 短路之前，否则空跑的店铺永远显示成功，
            # 而目前多数店铺是刻意保持 dry_run 的，等于把问题全盖住。
            site_outcomes = list((await self._site_outcomes(run)).values())
            if any(value is SiteStatus.NEEDS_HUMAN_AUTH for value in site_outcomes):
                empty_status = RunStatus.NEEDS_HUMAN_AUTH
            elif any(value is SiteStatus.FAILED for value in site_outcomes):
                empty_status = RunStatus.PARTIAL
            elif site_outcomes and all(
                value is SiteStatus.SKIPPED for value in site_outcomes
            ):
                # ⚠️ 全跳过不是「已完成」。这条路径原来直接落到 SUCCEEDED，于是
                # 一次**一个站点都没读成**的运行会发出绿色的「安全证明已完成」——
                # 2026-09-18 正香-张刚CA 实测：唯一的 DE 站停在账户/站点选择页被
                # 跳过，卡片标题却是「安全证明已完成」。
                #
                # 引擎的另一条路径 `_finish_operations` 对同一情形给的是
                # RunStatus.SKIPPED（蓝卡），两条路径本来就该一致。提现流程没暴露
                # 这个 bug，只是因为它读到余额后计划非空、走的是那一条。
                empty_status = RunStatus.SKIPPED
            else:
                # 其余都是正常空跑：站点空跑完成，或者这个流程根本不维护站点行
                # （`_site_outcomes` 返回空表 —— 注意空表上 all() 恒真，所以上面
                # 那一档必须先判 `site_outcomes` 非空）。
                empty_status = RunStatus.SUCCEEDED
            await self.repository.set_run_status(
                run.id, empty_status, allowed_from=(RunStatus.RUNNING,)
            )
            await self._notify(workflow, run, empty_status)
            return PrepareResult(run.id, empty_status, plan)
        if run.mode is RunMode.DRY_RUN:
            await self.repository.set_run_status(
                run.id, RunStatus.SUCCEEDED, allowed_from=(RunStatus.RUNNING,)
            )
            await self._notify(workflow, run, RunStatus.SUCCEEDED)
            return PrepareResult(run.id, RunStatus.SUCCEEDED, plan)
        if run.mode is RunMode.APPROVAL:
            await self.repository.create_approval(
                plan, expires_at=utc_now() + self.approval_ttl
            )
            await self.repository.set_run_status(
                run.id,
                RunStatus.WAITING_APPROVAL,
                allowed_from=(RunStatus.RUNNING,),
            )
            for line in plan.lines:
                await self.repository.set_site_status(
                    run.id, line.marketplace.code, SiteStatus.WAITING_APPROVAL
                )
            await self._notify(workflow, run, RunStatus.WAITING_APPROVAL)
            return PrepareResult(run.id, RunStatus.WAITING_APPROVAL, plan)
        operations = await workflow.execute(run, page, plan)
        return await self._finish_operations(run, workflow, operations)

    async def approve(self, run: WorkflowRun, *, actor: str = "admin") -> ExecutionResult:
        """Compatibility wrapper for non-queued engine consumers."""
        await self.record_approval(run, actor=actor)
        return await self.execute_approved(run)

    async def record_approval(
        self, run: WorkflowRun, *, actor: str = "admin"
    ) -> None:
        """Persist approval and put the run back in QUEUED without a browser."""
        if await self.repository.list_operations(run.id):
            raise RuntimeError("该任务已进入资金阶段，不能再批准")
        approval = await self.repository.get_approval(run.id)
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            raise RuntimeError("当前任务没有可批准的金额清单")
        if approval.expires_at <= utc_now():
            await self.repository.set_approval_status(run.id, ApprovalStatus.EXPIRED)
            raise RuntimeError("金额清单已过期，请重新生成")
        await self.repository.set_approval_status(
            run.id, ApprovalStatus.APPROVED, actor=actor
        )
        claimed = await self.repository.set_run_status(
            run.id,
            RunStatus.QUEUED,
            allowed_from=(RunStatus.WAITING_APPROVAL,),
        )
        if not claimed:
            raise RuntimeError("任务状态已变化，批准未触发执行")

    async def execute_approved(self, run: WorkflowRun) -> ExecutionResult:
        """Execute only an already-approved durable plan."""
        if await self.repository.list_operations(run.id):
            return await self.settle(run)
        approval = await self.repository.get_approval(run.id)
        if approval is None or approval.status is not ApprovalStatus.APPROVED:
            raise RuntimeError("当前任务没有已批准的金额清单")
        claimed = await self.repository.set_run_status(
            run.id,
            RunStatus.RUNNING,
            allowed_from=(RunStatus.QUEUED,),
        )
        if not claimed:
            if await self.repository.list_operations(run.id):
                return await self.settle(run)
            raise RuntimeError("任务状态已变化，批准任务未执行")
        return await self._resume_execute(run)

    async def _resume_execute(self, run: WorkflowRun) -> ExecutionResult:
        workflow = self.registry.get(run.workflow)
        plan = await self.repository.get_plan(run.id)
        if plan is None:
            raise RuntimeError("找不到已保存的金额清单")
        try:
            async with self._financial_session(run) as handle:
                completed, result = await self._with_auth_wait(
                    run,
                    workflow,
                    handle,
                    resume_status=RunStatus.RUNNING,
                    action=lambda: self._execute_in_session(
                        run, workflow, handle.page, plan
                    ),
                )
                if completed:
                    return result
                return ExecutionResult(
                    run.id, await self.repository.get_run_status(run.id), ()
                )
        except Exception as exc:
            await self._record_failure(run, workflow, exc)
            raise

    async def _execute_in_session(
        self, run: WorkflowRun, workflow: Any, page: Any, plan: WorkflowPlan
    ) -> ExecutionResult:
        # 同 _start_in_session:只有说不清的记录才只收口。审批模式多站点时,
        # A 站已提交、B 站等人工验证 —— 验证完必须把 B、C 做完,而不是只收口、
        # 然后报一个「已完成」(2026-09-23 自审抓到的回归)。
        unsettled = await self._unsettled_operations(run.id)
        if unsettled:
            operations = await workflow.reconcile(run, None, unsettled)
        else:
            # Required again after approval; execute() independently verifies
            # every approved snapshot immediately before atomic ARMED.
            # execute() performs the complete per-site identity/domain/amount
            # check immediately before each action. A second whole-run
            # preflight here would restart CA after a UK auth pause and adds no
            # safety proof that execute does not already require.
            operations = await workflow.execute(run, page, plan)
        return await self._finish_operations(run, workflow, operations)

    async def settle(self, run: WorkflowRun) -> ExecutionResult:
        """把动过钱的运行按**本地事实**收口到终态。不开浏览器、不抢资金锁。

        这里原本叫 ``reconcile``,会开一次紫鸟去亚马逊结算页回读每一笔到没到。
        运营 2026-09-23 要求删掉那套(回读窗口约一分钟而亚马逊常隔天才显示,
        结构上解决不了,只留下越堆越多的待办)。

        **它必须让运行落到终态** —— 第一版只在「有待收口记录」时才动,于是只剩
        CONFIRMED 记录的 RUNNING/QUEUED 运行永远卡住,挡住立即执行、删排期和建档重置。

        三种起点:

        * 已是终态(SUCCEEDED/PARTIAL/FAILED/CANCELLED/SKIPPED)—— 原样返回。
        * ``UNCERTAIN_FINANCIAL`` / ``NEEDS_HUMAN_AUTH`` —— 本身是「等人」的终态。先把
          资金记录收口,再看**按事实该是什么**:仍说不清就原样不动、**不重发通知**;
          已经有结论了(例如重启时被错翻成 UNCERTAIN_FINANCIAL 的成功提现)就经
          RECONCILING 落到真正的终态 —— 顺带修好被那个 bug 翻过的运行。
        * 其余(QUEUED/WAITING_*/RUNNING/RECONCILING)—— 认领到 RUNNING 后收尾。
          ⚠️ 必须先认领:``_finish_operations`` 只从 RUNNING/RECONCILING 写,
          直接从 QUEUED 写终态会**静默失败**。
        """

        workflow = self.registry.get(run.workflow)
        current = await self.repository.get_run_status(run.id)
        operations = tuple(await self.repository.list_operations(run.id))
        if not operations or current in _FINAL_RUN_STATUSES:
            return ExecutionResult(run.id, current, operations)

        # 收口本身不联网:``page=None``,流程侧已经 ``del page``。
        settled = tuple(await workflow.reconcile(run, None, operations))

        if current in (RunStatus.UNCERTAIN_FINANCIAL, RunStatus.NEEDS_HUMAN_AUTH):
            target = run_status_from_facts(
                _guard_facts(settled), await self._site_outcomes(run)
            )
            if target is RunStatus.UNCERTAIN_FINANCIAL:
                return ExecutionResult(run.id, current, settled)
            moved = await self.repository.set_run_status(
                run.id, RunStatus.RECONCILING, allowed_from=(current,)
            )
            if not moved:
                return ExecutionResult(
                    run.id, await self.repository.get_run_status(run.id), settled
                )
            await self._close_parked_sites(run, SiteStatus.CANCELLED, _INTERRUPTED)
            return await self._finish_operations(run, workflow, settled)

        if current not in (RunStatus.RUNNING, RunStatus.RECONCILING):
            moved = await self.repository.set_run_status(
                run.id, RunStatus.RUNNING, allowed_from=(current,)
            )
            if not moved:
                return ExecutionResult(
                    run.id, await self.repository.get_run_status(run.id), settled
                )
        # 收口一定是在「这条运行不会再往下做了」的时候:还停在等验证 / 等审核 /
        # 还没轮到的站点,就是本次没做的站点(重启后的收口正是如此)。不落到终态,
        # 终态运行的卡片上还会写「等待验证」「等待审核」(09-23 第二轮复核)。
        await self._close_parked_sites(run, SiteStatus.CANCELLED, _INTERRUPTED)
        return await self._finish_operations(run, workflow, settled)

    async def continue_auth(self, run: WorkflowRun) -> PrepareResult | ExecutionResult:
        """Signal a live lease, or recover a WAITING_AUTH run after restart."""
        signal = getattr(self.browser_sessions, "continue_auth", None)
        if callable(signal) and await signal(run.id):
            plan = await self.repository.get_plan(run.id)
            return PrepareResult(
                run.id,
                RunStatus.WAITING_AUTH,
                plan or WorkflowPlan(run.workflow, run.id, run.store.id, ()),
            )
        # 没有内存里的租约,说明进程重启过。动过钱的运行只能收口;还没动钱的
        # 才可以安全地把检查从头再做一遍。
        if await self.repository.list_operations(run.id):
            return await self.settle(run)
        changed = await self.repository.set_run_status(
            run.id,
            RunStatus.QUEUED,
            allowed_from=(RunStatus.WAITING_AUTH, RunStatus.NEEDS_HUMAN_AUTH),
        )
        if not changed:
            raise RuntimeError("任务当前不在等待人工验证状态")
        return await self.start(run)

    async def recover_incomplete(
        self, runs: Sequence[WorkflowRun]
    ) -> list[ExecutionResult]:
        recovered: list[ExecutionResult] = []
        for run in runs:
            if await self.repository.list_operations(run.id):
                recovered.append(await self.settle(run))
        return recovered

    async def expire_auth(self, run: WorkflowRun) -> bool:
        changed = await self.repository.set_run_status(
            run.id,
            RunStatus.NEEDS_HUMAN_AUTH,
            allowed_from=(RunStatus.WAITING_AUTH,),
        )
        if changed:
            # 只收还在等 / 还没轮到的站点。原来把 ``run.marketplaces`` 全部重写一遍,
            # 已经读完的站点(余额为零跳过、空跑完成)连同它的原因一起被抹成
            # 「验证超时」—— 卡片上一秒还写「已跳过：标准订单可用资金为零」,
            # 下一秒变成「验证超时」(2026-09-26 自审)。
            await self._close_parked_sites(
                run, SiteStatus.NEEDS_HUMAN_AUTH, "人工验证超时，该站点本次未执行"
            )
            await self._notify(
                self.registry.get(run.workflow), run, RunStatus.NEEDS_HUMAN_AUTH
            )
        return changed

    async def cancel(self, run: WorkflowRun) -> bool:
        signal = getattr(self.browser_sessions, "cancel_auth", None)
        if callable(signal) and await signal(run.id):
            return True
        if await self.repository.list_operations(run.id):
            return False
        allowed = (
            RunStatus.QUEUED, RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_AUTH, RunStatus.NEEDS_HUMAN_AUTH,
        )
        # This plugin has per-action cancellation checks and durable per-item
        # intent. Cancelling stops its next save while retaining uncertain
        # items for readback. Existing financial cancellation rules stay intact.
        if run.workflow == "amazon_inventory_gpsr_attestation":
            allowed += (RunStatus.RUNNING, RunStatus.PAUSED)
        changed = await self.repository.set_run_status(
            run.id,
            RunStatus.CANCELLED,
            allowed_from=allowed,
        )
        if changed:
            await self.repository.set_approval_status(
                run.id, ApprovalStatus.CANCELLED, actor="admin"
            )
            await self._close_parked_sites(
                run, SiteStatus.CANCELLED, "任务已取消，该站点本次未执行"
            )
            await self._notify(
                self.registry.get(run.workflow), run, RunStatus.CANCELLED
            )
        return changed

    async def _with_auth_wait(
        self,
        run: WorkflowRun,
        workflow: Any,
        handle: Any,
        *,
        resume_status: RunStatus,
        action: Callable[[], Awaitable[T]],
    ) -> tuple[bool, T | None]:
        while True:
            try:
                return True, await action()
            except HumanAuthRequired as exc:
                wait = getattr(self.browser_sessions, "wait_for_auth", None)
                if not callable(wait):
                    raise RuntimeError(
                        "浏览器控制器未实现原地人工验证等待"
                    ) from exc
                async def publish_waiting() -> None:
                    """把 WAITING_AUTH 公之于众 —— **只在租约建好之后**。

                    ⚠️ 顺序是承重的。状态一写,运行详情页两秒一轮询就会亮出
                    「尝试自动登录并继续检测」;而租约是 ``wait_for_auth`` 内部才
                    注册的。先发布的话,这中间(要等一次飞书 HTTP,超时 10 秒)
                    操作员点下去,``continue_auth`` 找不到租约、当成「进程重启过」,
                    把运行改成 QUEUED 再入队 —— 而队列条目还 CLAIMED 着,入队被去重
                    吞掉:运行永久卡在 QUEUED、没有队列条目、页面上一个按钮都没有,
                    还一直否决这条排期的后续操作(2026-09-26 自审实测)。
                    ``controller.wait_for_auth`` 的 ``on_waiting`` 就是为这个留的,
                    建档探针一直在用,唯独资金主流程没用。
                    """

                    await self.repository.set_run_status(
                        run.id,
                        RunStatus.WAITING_AUTH,
                        allowed_from=(resume_status,),
                        error=f"等待人工验证（{exc.kind}）：{exc}",
                    )
                    # The reason has to survive: it is the only record of *which*
                    # predicate stopped the login.  Storing the kind alone made a
                    # real failure ("Amazon 登录操作按钮缺失、禁用或不唯一") read as a
                    # generic hand-off, and the page state is gone by the time
                    # anyone asks.  These messages are fixed strings; no account,
                    # code or amount reaches them.
                    await self.repository.append_event(
                        run.id,
                        "human_auth_required",
                        f"浏览器保留现场，等待人工完成验证：{exc}",
                        details={"kind": exc.kind, "reason": str(exc)},
                    )
                    await self._notify(workflow, run, RunStatus.WAITING_AUTH)

                try:
                    try:
                        await wait(
                            handle,
                            auth_key=run.id,
                            timeout_seconds=self.auth_timeout_seconds,
                            on_waiting=publish_waiting,
                        )
                    except TypeError:
                        # 不支持 on_waiting 的控制器(老替身):退回旧顺序。
                        await publish_waiting()
                        await wait(
                            handle,
                            auth_key=run.id,
                            timeout_seconds=self.auth_timeout_seconds,
                        )
                except Exception as auth_exc:
                    if type(auth_exc).__name__ == "AuthWaitExpired":
                        if await self._unsettled_operations(run.id):
                            await self.repository.set_run_status(
                                run.id,
                                RunStatus.UNCERTAIN_FINANCIAL,
                                allowed_from=(RunStatus.WAITING_AUTH,),
                            )
                            await self._notify(
                                workflow, run, RunStatus.UNCERTAIN_FINANCIAL
                            )
                        elif await self.repository.list_operations(run.id):
                            # 已有站点提交成功、这一站等人等超时了:不是「结果不明」,
                            # 也不能走 expire_auth(它会把**所有**站点改成
                            # NEEDS_HUMAN_AUTH,连已提交的那站一起)。按事实收尾。
                            await self._conclude_after_auth_wait(
                                run, workflow, cancelled=False
                            )
                        else:
                            await self.expire_auth(run)
                        return False, None
                    if type(auth_exc).__name__ == "AuthWaitCancelled":
                        if await self._unsettled_operations(run.id):
                            await self.repository.set_run_status(
                                run.id,
                                RunStatus.UNCERTAIN_FINANCIAL,
                                allowed_from=(RunStatus.WAITING_AUTH,),
                            )
                        elif await self.repository.list_operations(run.id):
                            await self._conclude_after_auth_wait(
                                run, workflow, cancelled=True
                            )
                        else:
                            await self.repository.set_run_status(
                                run.id,
                                RunStatus.CANCELLED,
                                allowed_from=(RunStatus.WAITING_AUTH,),
                            )
                            if run.workflow == "amazon_inventory_gpsr_attestation":
                                # A cancelled auth lease can still own a save
                                # intent. The notice builder checks the ledger;
                                # cancellation without intent remains silent.
                                await self._notify(workflow, run, RunStatus.CANCELLED)
                        return False, None
                    raise
                changed = await self.repository.set_run_status(
                    run.id,
                    resume_status,
                    allowed_from=(RunStatus.WAITING_AUTH,),
                )
                if not changed:
                    raise RuntimeError("人工验证恢复时任务状态已变化")
                await self.repository.append_event(
                    run.id,
                    "human_auth_resumed",
                    "人工验证完成；在同一紫鸟页面从当前站点继续，并重新检查后续域名、身份、账户和金额",
                )

    async def _finish_operations(
        self, run: WorkflowRun, workflow: Any, operations: Sequence[Any]
    ) -> ExecutionResult:
        # 终态按**这条运行的全部资金记录**来定,而不只看这一次调用传进来的那几条:
        # 人工验证续跑时 execute 从游标处接着做,前面已提交的站点不会再出现在
        # ``operations`` 里,只看它会把一次「A 站已提交、B 站被限流」算成「什么都没做」。
        recorded = tuple(await self.repository.list_operations(run.id))
        sites = await self._site_outcomes(run)
        site_outcomes = list(sites.values())
        status = run_status_from_facts(_guard_facts(recorded), sites)
        if not recorded:
            if site_outcomes and all(
                value in {SiteStatus.SKIPPED, SiteStatus.DRY_RUN_COMPLETE}
                for value in site_outcomes
            ):
                status = (
                    RunStatus.SKIPPED
                    if all(value is SiteStatus.SKIPPED for value in site_outcomes)
                    else RunStatus.SUCCEEDED
                )
            elif any(
                value in {SiteStatus.FAILED, SiteStatus.NEEDS_HUMAN_AUTH}
                for value in site_outcomes
            ):
                # No money moved and at least one site could not be attempted.
                # "已完成" in green is the wrong thing to tell an operator who
                # still has an unpaid balance sitting on that marketplace.
                #
                # ``NEEDS_HUMAN_AUTH`` belongs here too.  In auto mode a site
                # that wants a human is dropped rather than parked, so it
                # matched neither branch and the run fell through to the
                # default SUCCEEDED — RUN_EXAMPLE_B reported 「任务检查已完成」
                # while its marketplace still had a balance that nothing attempted.
                status = RunStatus.PARTIAL
        # 正常收尾同样不许留下「等待审核 / 还没轮到」的站点:审批期间站点被停用,
        # 执行时它就不在 run.marketplaces 里,于是永远停在 WAITING_APPROVAL
        # (2026-09-26 自审)。异常/取消/重启那几条路径已经各自收过。
        if status in _FINAL_RUN_STATUSES:
            await self._close_parked_sites(
                run, SiteStatus.CANCELLED, "本次运行结束时该站点未执行"
            )
        changed = await self.repository.set_run_status(
            run.id,
            status,
            allowed_from=(RunStatus.RUNNING, RunStatus.RECONCILING),
        )
        if not changed:
            # A terminal status is never an execution lease.  In particular,
            # an old cancellation race must not let a still-running coroutine
            # rewrite CANCELLED as SUCCEEDED (or emit a green success report).
            # The SQL ARMED barrier now prevents that race at its source; this
            # remains defence in depth for already-running/legacy workers.
            current = await self.repository.get_run_status(run.id)
            return ExecutionResult(run.id, current, tuple(operations))
        await self._notify(workflow, run, status)
        return ExecutionResult(run.id, status, tuple(operations))

    async def _site_outcomes(self, run: WorkflowRun) -> dict[str, SiteStatus]:
        """这条运行**全部**站点行的状态,按站点码。

        ⚠️ 不能按 ``run.marketplaces`` 逐个去查:资金收口的加载器只按资金记录重建
        站点列表(``runtime._load_financial_recovery_run``),于是没有资金记录的那一站
        (失败、等人、还没轮到)根本不会被看到 —— 一站付了、一站失败的运行在重启收口
        时会被报成 SUCCEEDED、发绿卡(2026-09-23 自审复核抓到)。
        """

        return dict(await self.repository.list_site_statuses(run.id))

    async def _record_failure(
        self, run: WorkflowRun, workflow: Any, exc: Exception
    ) -> None:
        # 一笔已提交(SUBMITTED)之后的异常不是「结果不明」:那笔有结论,是后面的
        # 站点没做完 —— PARTIAL。只有真说不清的记录才落 UNCERTAIN_FINANCIAL。
        unsettled = await self._unsettled_operations(run.id)
        all_operations = await self.repository.list_operations(run.id)
        if unsettled:
            target = RunStatus.UNCERTAIN_FINANCIAL
        elif all_operations:
            target = RunStatus.PARTIAL
        else:
            target = RunStatus.FAILED
        current = await self.repository.get_run_status(run.id)
        if current in (
            RunStatus.RUNNING,
            RunStatus.RECONCILING,
            RunStatus.WAITING_AUTH,
        ):
            changed = await self.repository.set_run_status(
                run.id,
                target,
                allowed_from=(current,),
                error=_safe_error(exc),
            )
            # 终态运行上不能还挂着「等待审核 / 等待验证 / 读取中」的站点(09-23 第三轮
            # 复核)。UNCERTAIN_FINANCIAL 不在这里收:它之后由 settle 收口时一并处理。
            if changed and target is not RunStatus.UNCERTAIN_FINANCIAL:
                await self._close_parked_sites(
                    run, SiteStatus.CANCELLED, "任务异常中止，该站点本次未执行"
                )
            await self._notify(workflow, run, target)

    async def _unsettled_operations(self, run_id: str) -> Sequence[Any]:
        """说不清点没点下去的资金记录。见 ``types.UNSETTLED_GUARD_STATES``。"""

        return await self.repository.list_operations(
            run_id, states=tuple(UNSETTLED_GUARD_STATES)
        )

    async def _conclude_after_auth_wait(
        self, run: WorkflowRun, workflow: Any, *, cancelled: bool
    ) -> None:
        """等人工验证落空(超时/取消),但已有站点提交成功:按事实收尾。

        状态机里 WAITING_AUTH 不能直接到 PARTIAL,要经 RECONCILING;之后由
        ``_finish_operations`` 按全部资金记录 + 站点结果定终态。

        等人的那一站、以及排在它后面还没轮到的站点,先落到「没做」的终态 ——
        否则终态运行的卡片上还写着「等待验证」「等待审核」,把人指去一个已经
        动不了的运行(09-23 自审复核)。
        """

        moved = await self.repository.set_run_status(
            run.id, RunStatus.RECONCILING, allowed_from=(RunStatus.WAITING_AUTH,)
        )
        if not moved:
            return
        await self._close_parked_sites(
            run,
            SiteStatus.CANCELLED if cancelled else SiteStatus.NEEDS_HUMAN_AUTH,
            (
                "人工验证已取消，该站点本次未执行"
                if cancelled
                else "人工验证超时，该站点本次未执行"
            ),
        )
        await self._finish_operations(run, workflow, ())

    async def _close_parked_sites(
        self, run: WorkflowRun, target: SiteStatus, error: str
    ) -> None:
        """运行要收尾了,还在等 / 还没轮到的站点落到「本次没做」的终态。"""

        for code, status in (await self._site_outcomes(run)).items():
            if status in _PARKED_SITE_STATUSES:
                await self.repository.set_site_status(run.id, code, target, error=error)

    @asynccontextmanager
    async def _financial_session(self, run: WorkflowRun) -> AsyncIterator[Any]:
        """Open the store's browser, taking the funds lock only when required.

        ``financial_session`` acquires the *global* funds lock, which serialises
        every store's payouts.  That is correct for money, but a workflow that
        merely reads and clicks non-financial controls can run for minutes and
        would then block payouts everywhere — and Amazon rate limits on-demand
        disbursement on a rolling 24-hour window, so a delayed payout is a
        missed one.  The per-store lock inside ``session`` still prevents two
        runs from driving the same Ziniao browser.
        """

        from ziniao_automation.ziniao.models import ProfileSelector

        selector = ProfileSelector(run.store.selector_type, run.store.selector_value)
        locking = getattr(self.browser_sessions, "financial_session", None)
        plain = getattr(self.browser_sessions, "session", None)
        # Providers that cannot open a non-financial session leave nothing to
        # choose; this is a capability check, not a fallback that hides an error.
        use_lock = self._needs_funds_lock(run) or not callable(plain)
        factory = locking if callable(locking) and use_lock else plain
        if factory is None:
            raise RuntimeError("浏览器会话提供方既没有 session 也没有 financial_session")
        async with factory(selector, store_key=run.store.id) as handle:
            yield handle

    def _needs_funds_lock(self, run: WorkflowRun) -> bool:
        try:
            definition = self.registry.definition(run.workflow)
        except Exception:
            # An unregistered workflow cannot be reasoned about; take the
            # stricter lock rather than assume it moves no money.
            return True
        return bool(getattr(definition, "requires_financial_lock", True))

    async def _notify(self, workflow: Any, run: WorkflowRun, status: RunStatus) -> None:
        try:
            await self.notifier.send(await workflow.report(run, status))
        except Exception:
            logger.exception("Notifier failed for run %s", run.id)
            await self.repository.append_event(
                run.id, "notification_failed", "通知发送失败"
            )


# 终态:收口不该再去碰它们。
_FINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.SKIPPED,
    }
)
# 没有资金记录的站点,只有这两种状态算「这一站做完了」。其余一律是没做完 ——
# 反着列(只列「没做完」的)会漏:还没轮到的 PENDING/PLANNED、被释放后留在
# UNCERTAIN_FINANCIAL 的站点,都曾因此被算成成功(09-23 自审复核)。
_DONE_SITE_STATUSES = frozenset({SiteStatus.SKIPPED, SiteStatus.DRY_RUN_COMPLETE})
# 人工验证落空时,这些站点是「还在等 / 还没轮到」,要落到没做的终态。
_PARKED_SITE_STATUSES = frozenset(
    {
        SiteStatus.PENDING,
        SiteStatus.PREFLIGHT,
        SiteStatus.PLANNED,
        SiteStatus.WAITING_APPROVAL,
        SiteStatus.WAITING_AUTH,
        SiteStatus.PAUSED,
    }
)
_DISPATCHED = frozenset({GuardState.SUBMITTED, GuardState.CONFIRMED})
_INTERRUPTED = "任务中断（服务重启或人工收尾），该站点本次未执行"


def _guard_facts(operations: Iterable[Any]) -> list[tuple[str, GuardState]]:
    return [(item.intent.marketplace_code, item.state) for item in operations]


def run_status_from_facts(
    guards: Iterable[tuple[str, GuardState]],
    sites: Mapping[str, SiteStatus],
) -> RunStatus:
    """由资金记录与站点结果定一条**动过钱**的运行的终态。

    引擎收口和运行详情页的人工收尾都用它 —— 两处原来各算各的,同一组事实一个
    报 SUCCEEDED、一个报 PARTIAL(09-23 自审复核)。

    ``guards`` 是 ``(站点码, 状态)``;``sites`` 是这条运行**全部**站点行的状态。
    一站算做完:它有已派发的资金记录(SUBMITTED/CONFIRMED),或者它是 SKIPPED /
    DRY_RUN_COMPLETE。有任何一站没做完,就不能报「已完成」。

    ⚠️ 这里原来把 ``SUBMITTED`` 也算进「结果不明」。那在有回读的年代说得通:
    回读跑完还停在 SUBMITTED,确实是「提交了但查不到」。2026-09-23 删掉回读之后
    ``SUBMITTED`` 变成**正常的终态** —— 那一下不可逆点击返回过,请求确实发出去了。
    不改这里,每一次**成功的**提现都会收在 ``UNCERTAIN_FINANCIAL``(红卡),
    比删之前还糟。

    留在「结果不明」里的只剩 ``ARMED``(锁了但从没看到点击返回)和 ``UNCERTAIN``
    (人工待裁定,以及删除前留下的历史记录)。那是真的不知道,不能报成功。
    """

    facts = [(str(code).upper(), GuardState(state)) for code, state in guards]
    if not facts:
        return RunStatus.SUCCEEDED
    states = {state for _, state in facts}
    if states & UNSETTLED_GUARD_STATES:
        return RunStatus.UNCERTAIN_FINANCIAL
    if not states <= _DISPATCHED:
        return RunStatus.PARTIAL
    dispatched = {code for code, state in facts if state in _DISPATCHED}
    for code, status in sites.items():
        if str(code).upper() in dispatched:
            continue
        if SiteStatus(status) not in _DONE_SITE_STATUSES:
            return RunStatus.PARTIAL
    return RunStatus.SUCCEEDED


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"
