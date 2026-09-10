"""Application service that owns invocation, human-auth waits and recovery."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence, TypeVar

from .contracts import FinancialSessionProvider, Notifier, WorkflowRepository
from .errors import HumanAuthRequired
from .registry import WorkflowRegistry
from .types import (
    ApprovalStatus,
    ExecutionResult,
    GuardState,
    PrepareResult,
    RunMode,
    RunStatus,
    SiteStatus,
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
        pending = await self._pending_operations(run.id)
        if pending:
            return await self.reconcile(run)
        claimed = await self.repository.set_run_status(
            run.id, RunStatus.RUNNING, allowed_from=(RunStatus.QUEUED,)
        )
        if not claimed:
            status = await self.repository.get_run_status(run.id)
            if status in (RunStatus.UNCERTAIN_FINANCIAL, RunStatus.RECONCILING):
                return await self.reconcile(run)
            saved = await self.repository.get_plan(run.id)
            if saved is None:
                raise RuntimeError(f"任务 {run.id} 当前为 {status.value}，不可重复启动")
            return PrepareResult(run.id, status, saved)

        workflow = self.registry.get(run.workflow)
        try:
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
        except Exception as exc:
            await self._record_failure(run, workflow, exc)
            raise

    async def _start_in_session(
        self, run: WorkflowRun, workflow: Any, page: Any
    ) -> PrepareResult | ExecutionResult:
        # This check runs again after any human-auth continuation.
        pending = await self._pending_operations(run.id)
        if pending:
            await self.repository.set_run_status(
                run.id,
                RunStatus.RECONCILING,
                allowed_from=(RunStatus.RUNNING,),
            )
            operations = await workflow.reconcile(run, page, pending)
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
            site_outcomes = await self._site_outcomes(run)
            if any(value is SiteStatus.NEEDS_HUMAN_AUTH for value in site_outcomes):
                empty_status = RunStatus.NEEDS_HUMAN_AUTH
            elif any(value is SiteStatus.FAILED for value in site_outcomes):
                empty_status = RunStatus.PARTIAL
            else:
                # 其余都是正常空跑：站点全部跳过／空跑完成，或者这个流程根本
                # 不维护站点行（`_site_outcomes` 返回空表）。
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
            raise RuntimeError("该任务已进入资金阶段，只允许回读")
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
            return await self.reconcile(run)
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
                return await self.reconcile(run)
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
        pending = await self._pending_operations(run.id)
        if pending:
            await self.repository.set_run_status(
                run.id,
                RunStatus.RECONCILING,
                allowed_from=(RunStatus.RUNNING,),
            )
            operations = await workflow.reconcile(run, page, pending)
        else:
            # Required again after approval; execute() independently verifies
            # every approved snapshot immediately before atomic ARMED.
            # execute() performs the complete per-site identity/domain/amount
            # check immediately before each action. A second whole-run
            # preflight here would restart CA after a UK auth pause and adds no
            # safety proof that execute does not already require.
            operations = await workflow.execute(run, page, plan)
        return await self._finish_operations(run, workflow, operations)

    async def reconcile(self, run: WorkflowRun) -> ExecutionResult:
        workflow = self.registry.get(run.workflow)
        current = await self.repository.get_run_status(run.id)
        if current is not RunStatus.RECONCILING:
            changed = await self.repository.set_run_status(
                run.id,
                RunStatus.RECONCILING,
                allowed_from=(
                    RunStatus.RUNNING,
                    RunStatus.WAITING_APPROVAL,
                    RunStatus.WAITING_AUTH,
                    RunStatus.NEEDS_HUMAN_AUTH,
                    RunStatus.UNCERTAIN_FINANCIAL,
                ),
            )
            if not changed and current in (
                RunStatus.SUCCEEDED,
                RunStatus.PARTIAL,
                RunStatus.FAILED,
            ):
                records = await self.repository.list_operations(run.id)
                return ExecutionResult(run.id, current, tuple(records))
            if not changed:
                raise RuntimeError(f"任务状态 {current.value} 不允许回读")
        try:
            async with self._financial_session(run) as handle:
                completed, result = await self._with_auth_wait(
                    run,
                    workflow,
                    handle,
                    resume_status=RunStatus.RECONCILING,
                    action=lambda: self._reconcile_in_session(
                        run, workflow, handle.page
                    ),
                )
                if completed:
                    return result
                records = await self.repository.list_operations(run.id)
                return ExecutionResult(
                    run.id,
                    await self.repository.get_run_status(run.id),
                    tuple(records),
                )
        except Exception as exc:
            await self._record_failure(run, workflow, exc)
            raise

    async def _reconcile_in_session(
        self, run: WorkflowRun, workflow: Any, page: Any
    ) -> ExecutionResult:
        operations = await workflow.reconcile(run, page)
        return await self._finish_operations(run, workflow, operations)

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
        # No in-memory lease means the process restarted.  A guard can only be
        # reconciled; a pre-submit task may safely restart its checks.
        if await self._pending_operations(run.id):
            return await self.reconcile(run)
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
            if await self._pending_operations(run.id):
                recovered.append(await self.reconcile(run))
        return recovered

    async def expire_auth(self, run: WorkflowRun) -> bool:
        changed = await self.repository.set_run_status(
            run.id,
            RunStatus.NEEDS_HUMAN_AUTH,
            allowed_from=(RunStatus.WAITING_AUTH,),
        )
        if changed:
            for marketplace in run.marketplaces:
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.NEEDS_HUMAN_AUTH
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
        changed = await self.repository.set_run_status(
            run.id,
            RunStatus.CANCELLED,
            allowed_from=(
                RunStatus.QUEUED,
                RunStatus.WAITING_APPROVAL,
                RunStatus.WAITING_AUTH,
                RunStatus.NEEDS_HUMAN_AUTH,
            ),
        )
        if changed:
            await self.repository.set_approval_status(
                run.id, ApprovalStatus.CANCELLED, actor="admin"
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
                await self.repository.set_run_status(
                    run.id,
                    RunStatus.WAITING_AUTH,
                    allowed_from=(resume_status,),
                    error=f"等待人工验证：{exc.kind}",
                )
                await self.repository.append_event(
                    run.id,
                    "human_auth_required",
                    "浏览器保留现场，等待人工完成验证",
                    details={"kind": exc.kind},
                )
                await self._notify(workflow, run, RunStatus.WAITING_AUTH)
                try:
                    await wait(
                        handle,
                        auth_key=run.id,
                        timeout_seconds=self.auth_timeout_seconds,
                    )
                except Exception as auth_exc:
                    if type(auth_exc).__name__ == "AuthWaitExpired":
                        if await self._pending_operations(run.id):
                            await self.repository.set_run_status(
                                run.id,
                                RunStatus.UNCERTAIN_FINANCIAL,
                                allowed_from=(RunStatus.WAITING_AUTH,),
                            )
                            await self._notify(
                                workflow, run, RunStatus.UNCERTAIN_FINANCIAL
                            )
                        else:
                            await self.expire_auth(run)
                        return False, None
                    if type(auth_exc).__name__ == "AuthWaitCancelled":
                        if await self.repository.list_operations(run.id):
                            await self.repository.set_run_status(
                                run.id,
                                RunStatus.UNCERTAIN_FINANCIAL,
                                allowed_from=(RunStatus.WAITING_AUTH,),
                            )
                        else:
                            await self.repository.set_run_status(
                                run.id,
                                RunStatus.CANCELLED,
                                allowed_from=(RunStatus.WAITING_AUTH,),
                            )
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
        status = _status_from_operations(operations)
        if not operations:
            site_outcomes = await self._site_outcomes(run)
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

    async def _site_outcomes(self, run: WorkflowRun) -> list[SiteStatus]:
        getter = getattr(self.repository, "get_site_status", None)
        if not callable(getter):
            return []
        values: list[SiteStatus] = []
        for marketplace in run.marketplaces:
            try:
                value = await getter(run.id, marketplace.code)
            except Exception:
                continue
            if value is not None:
                values.append(SiteStatus(value))
        return values

    async def _record_failure(
        self, run: WorkflowRun, workflow: Any, exc: Exception
    ) -> None:
        pending = await self._pending_operations(run.id)
        all_operations = await self.repository.list_operations(run.id)
        if pending:
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
            await self.repository.set_run_status(
                run.id,
                target,
                allowed_from=(current,),
                error=_safe_error(exc),
            )
            await self._notify(workflow, run, target)

    async def _pending_operations(self, run_id: str) -> Sequence[Any]:
        return await self.repository.list_operations(
            run_id,
            states=(GuardState.ARMED, GuardState.SUBMITTED, GuardState.UNCERTAIN),
        )

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


def _status_from_operations(operations: Sequence[Any]) -> RunStatus:
    if not operations:
        return RunStatus.SUCCEEDED
    states = {item.state for item in operations}
    if states & {GuardState.UNCERTAIN, GuardState.ARMED, GuardState.SUBMITTED}:
        return RunStatus.UNCERTAIN_FINANCIAL
    if states == {GuardState.CONFIRMED}:
        return RunStatus.SUCCEEDED
    return RunStatus.PARTIAL


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"
