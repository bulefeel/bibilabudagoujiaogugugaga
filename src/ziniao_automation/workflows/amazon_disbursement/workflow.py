"""Deterministic Amazon PAYABLE disbursement plugin."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from ..contracts import MarketplacePageAdapter, WorkflowRepository
from ..errors import (
    ApprovalExpired,
    ApprovalRequired,
    HumanAuthRequired,
    PayoutRateLimited,
    PlanChanged,
    PlatformDisbursementExists,
    PreflightRejected,
    SubmissionNotDispatched,
)
from ..types import (
    ApprovalStatus,
    GuardState,
    MarketplaceRef,
    OperationIntent,
    OperationRecord,
    PlanLine,
    ReconcileStatus,
    RunMode,
    RunStatus,
    SiteStatus,
    WorkflowPlan,
    WorkflowReport,
    WorkflowRun,
    disbursement_day_key,
    operation_guard_key,
    utc_now,
)


logger = logging.getLogger(__name__)


def _zero_reason_code(snapshot: Any) -> str:
    """Distinguish "Amazon has no payments data" from "the balance is zero".

    Both end the site the same way, but only the first means the marketplace
    is not producing statements at all — worth telling apart in a report the
    operator is meant to act on.
    """

    return (
        "no_payments_data"
        if str(getattr(snapshot, "settlement_key", "")).startswith("NO_DATA:")
        else "zero_or_not_submittable"
    )


def _log_site_outcome(
    marketplace: MarketplaceRef,
    outcome: str,
    reason_code: str,
    **extra: Any,
) -> None:
    """Put every per-site verdict in the log file, content-free.

    Until now a site's fate was written only to ``run_events`` in SQLite, so a
    run that opened the browser, read both marketplaces and closed again left
    the log file completely empty — the operator saw a payout "fail" with
    nothing anywhere to explain it, and the actual answer (Amazon's rolling
    24-hour cap) was invisible.

    Follows the same discipline as ``_describe_details_dom`` on the page
    adapter: codes, counts and durations only.  No amount, no payout account
    tail, no seller name and no page text ever reaches the log.
    """

    detail = "".join(
        f" {key}={value}" for key, value in extra.items() if value not in (None, "")
    )
    logger.info(
        "site_outcome: marketplace=%s outcome=%s reason_code=%s%s",
        marketplace.code,
        outcome,
        reason_code,
        detail,
        extra={"marketplace": marketplace.code, "event": "site_outcome"},
    )


class AmazonDisbursementWorkflow:
    name = "amazon_disbursement"

    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        page_adapter: MarketplacePageAdapter,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.repository = repository
        self.page_adapter = page_adapter
        self._sleep = sleep
        # In-memory cursors are valid only while WorkflowEngine retains the
        # same live Ziniao handle during WAITING_AUTH.  They are not recovery
        # state: a process restart still follows the durable ARMED guards.
        self._auth_cursors: dict[tuple[str, str], int] = {}

    async def _fail_site_and_continue(
        self,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        exc: Exception,
        *,
        reason_code: str,
    ) -> None:
        """Record a per-site execution failure without ending the run.

        Only safe for failures raised *before* ``arm_operation``: nothing has
        been dispatched for this site, so the remaining marketplaces are
        untouched and may still be paid out.
        """

        await self.repository.set_site_status(
            run.id,
            marketplace.code,
            SiteStatus.FAILED,
            error=_safe_site_error(exc),
        )
        await self.repository.append_event(
            run.id,
            "site_execution_failed",
            "该站点执行前检查未通过，已跳过；其余站点继续",
            marketplace_code=marketplace.code,
            details={"reason": _safe_site_error(exc), "reason_code": reason_code},
        )
        _log_site_outcome(marketplace, "FAILED", reason_code)

    async def _auto_drops_site_needing_human(
        self,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        exc: HumanAuthRequired,
        *,
        guard_key: str | None = None,
    ) -> bool:
        """In ``auto`` mode a site that needs a human is dropped, not parked.

        Parking holds the browser, store and funds locks for the whole auth
        lease.  On the intended deployment — dozens of stores plus other
        automations sharing one machine — one site that wants a human would
        otherwise stall the entire queue, and the remaining sites of the same
        run never get their turn.  Dropping just that site keeps the run
        moving; the operator still sees it as ``NEEDS_HUMAN_AUTH``.

        A site whose money may already be in flight is **never** dropped: it
        still parks so the existing reconcile-only path runs.  ``approval`` and
        ``dry_run`` are unchanged — a human is already in the loop there.
        """

        if run.mode is not RunMode.AUTO:
            return False
        if guard_key is not None:
            if await self.repository.get_operation(guard_key) is not None:
                return False
        # 原因必须留下来:它是唯一能说明「卡在哪一步」的东西(登录?选站?),
        # 和引擎 WAITING_AUTH 那条路径的做法一致(09-23 第二轮复核)。
        await self.repository.set_site_status(
            run.id,
            marketplace.code,
            SiteStatus.NEEDS_HUMAN_AUTH,
            # 「已跳过」放最前:卡片的原因栏只截到第一个「；」。
            error=f"自动模式已跳过该站点（{exc.kind}）：{exc}",
        )
        await self.repository.append_event(
            run.id,
            "site_needs_human_auth",
            f"自动模式不等待人工处理，该站点已跳过（{exc}）；下次运行会重新尝试",
            marketplace_code=marketplace.code,
            details={"kind": exc.kind, "reason": str(exc)},
        )
        return True

    async def preflight(self, run: WorkflowRun, page: Any) -> None:
        if run.workflow != self.name:
            raise ValueError(f"任务工作流应为 {self.name}")
        if not run.marketplaces:
            raise ValueError("任务至少需要一个站点")
        cursor_key = (run.id, "preflight")
        start_index = self._auth_cursors.get(cursor_key, 0)
        for index, marketplace in enumerate(run.marketplaces[start_index:], start_index):
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.PREFLIGHT
            )
            # Per-site failures are isolated: one inaccessible marketplace
            # never prevents later CA/UK/AU sites from producing a result.
            try:
                await self.page_adapter.preflight(page, run, marketplace)
            except HumanAuthRequired as exc:
                if await self._auto_drops_site_needing_human(run, marketplace, exc):
                    self._auth_cursors[cursor_key] = index + 1
                    continue
                self._auth_cursors[cursor_key] = index
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.WAITING_AUTH
                )
                raise
            except Exception as exc:
                await self.repository.set_site_status(
                    run.id,
                    marketplace.code,
                    SiteStatus.SKIPPED,
                    error=_safe_site_error(exc),
                )
                await self.repository.append_event(
                    run.id,
                    "site_preflight_skipped",
                    "该站点域名、身份或页面契约检查未通过",
                    marketplace_code=marketplace.code,
                    details={"reason": _safe_site_error(exc)},
                )
                self._auth_cursors[cursor_key] = index + 1
                continue
            await self.repository.append_event(
                run.id,
                "preflight_passed",
                "域名、登录和卖家身份检查通过",
                marketplace_code=marketplace.code,
            )
            self._auth_cursors[cursor_key] = index + 1
        self._auth_cursors.pop(cursor_key, None)

    async def plan(self, run: WorkflowRun, page: Any) -> WorkflowPlan:
        lines: list[PlanLine] = []
        cursor_key = (run.id, "plan")
        start_index = self._auth_cursors.get(cursor_key, 0)
        saved = await self.repository.get_plan(run.id)
        if start_index and saved is not None:
            lines.extend(saved.lines)
        for index, marketplace in enumerate(run.marketplaces[start_index:], start_index):
            try:
                snapshot = await self.page_adapter.read_snapshot(
                    page, run, marketplace
                )
            except HumanAuthRequired as exc:
                await self.repository.save_plan(
                    WorkflowPlan(
                        workflow=self.name,
                        run_id=run.id,
                        store_id=run.store.id,
                        lines=tuple(lines),
                    )
                )
                if await self._auto_drops_site_needing_human(run, marketplace, exc):
                    self._auth_cursors[cursor_key] = index + 1
                    continue
                self._auth_cursors[cursor_key] = index
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.WAITING_AUTH
                )
                raise
            except Exception as exc:
                await self.repository.set_site_status(
                    run.id,
                    marketplace.code,
                    SiteStatus.SKIPPED,
                    error=_safe_site_error(exc),
                )
                await self.repository.append_event(
                    run.id,
                    "site_plan_skipped",
                    "该站点无法生成安全金额快照",
                    marketplace_code=marketplace.code,
                    details={
                        "reason": _safe_site_error(exc),
                        "reason_code": "snapshot_unreadable",
                    },
                )
                _log_site_outcome(marketplace, "SKIPPED", "snapshot_unreadable")
                self._auth_cursors[cursor_key] = index + 1
                continue
            # Keep financial reporting identical across dry_run / approval /
            # auto.  Non-actionable zero/no-data snapshots deliberately stay
            # out of ``plan.lines``, but their safe numeric facts still belong
            # on SiteRun for local history and database-built notifications.
            await self.repository.save_site_snapshot(run.id, marketplace, snapshot)
            evidence = await self.page_adapter.capture_evidence(
                page, run, marketplace, label="plan"
            )
            if evidence is not None:
                await self.repository.save_evidence(
                    run.id, marketplace.code, evidence
                )
            line = PlanLine(
                marketplace=marketplace,
                snapshot=snapshot,
                screenshot_path=evidence.file_path if evidence else None,
            )
            await self.repository.append_event(
                run.id,
                "site_planned",
                "已读取 PAYABLE 与延迟交易资金",
                marketplace_code=marketplace.code,
                details={
                    "currency": snapshot.currency,
                    "payable_amount": str(snapshot.payable_amount),
                    "delayed_amount": str(snapshot.delayed_amount),
                    "settlement_key": snapshot.settlement_key,
                    "skip_reason": snapshot.skip_reason,
                    "identity_source": snapshot.identity_source,
                },
            )
            if run.mode is RunMode.DRY_RUN:
                lines.append(line)
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.DRY_RUN_COMPLETE
                )
            elif snapshot.payable_amount <= Decimal("0") or not snapshot.can_submit:
                # Zero/no-data is a normal terminal result.  Keeping it out of
                # an actionable plan also prevents an empty approval request.
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.SKIPPED
                )
                await self.repository.append_event(
                    run.id,
                    "site_skipped",
                    snapshot.skip_reason
                    or "标准订单可用资金为零或页面明确不可提交",
                    marketplace_code=marketplace.code,
                    details={"reason_code": _zero_reason_code(snapshot)},
                )
                _log_site_outcome(
                    marketplace, "SKIPPED", _zero_reason_code(snapshot)
                )
            else:
                lines.append(line)
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.PLANNED
                )
            self._auth_cursors[cursor_key] = index + 1
        self._auth_cursors.pop(cursor_key, None)
        return WorkflowPlan(
            workflow=self.name,
            run_id=run.id,
            store_id=run.store.id,
            lines=tuple(lines),
        )

    async def execute(
        self, run: WorkflowRun, page: Any, plan: WorkflowPlan
    ) -> Sequence[OperationRecord]:
        """Re-check -> platform duplicate check -> atomic ARMED -> one click.

        After ``arm_operation`` succeeds there is no path back to submission.
        An exception (including process death) leaves ARMED persisted and the
        engine's next invocation redirects to :meth:`reconcile`.
        """
        if run.mode is RunMode.DRY_RUN:
            return ()
        if plan.run_id != run.id or plan.store_id != run.store.id:
            raise PlanChanged("金额清单不属于当前任务或店铺")
        if run.mode is RunMode.APPROVAL:
            approval = await self.repository.get_approval(run.id)
            if approval is None or approval.status is not ApprovalStatus.APPROVED:
                raise ApprovalRequired("金额清单尚未批准")
            if approval.expires_at <= utc_now():
                await self.repository.set_approval_status(
                    run.id, ApprovalStatus.EXPIRED
                )
                raise ApprovalExpired("金额清单已过期")
            if approval.plan_hash != plan.plan_hash:
                await self.repository.set_approval_status(
                    run.id, ApprovalStatus.INVALIDATED
                )
                raise PlanChanged("批准记录与金额清单不一致")

        result: list[OperationRecord] = []
        lines_by_code = {line.marketplace.code: line for line in plan.lines}
        cursor_key = (run.id, "execute")
        start_index = self._auth_cursors.get(cursor_key, 0)
        # 本运行已经动过钱的站点。人工验证续跑时 execute 会再进来一次 ——
        # 那些站点**绝不能再 arm**。只查今天的 ``guard_key`` 不够:续跑跨过午夜时
        # 日期变了、key 也变了,旧逻辑会给同一站点重新 arm、再点一次提现。
        # (2026-09-23 删回读之后,引擎不再对已 SUBMITTED 的运行一律只收口,
        # 这一道逐站点的闸就成了续跑时防二次点击的承重墙。)
        already_touched = {
            record.intent.marketplace_code: record
            for record in await self.repository.list_operations(run.id)
        }
        for index, marketplace in enumerate(run.marketplaces[start_index:], start_index):
            line = lines_by_code.get(marketplace.code)
            if line is None:
                # Planning already recorded a precise per-site skip reason.
                self._auth_cursors[cursor_key] = index + 1
                continue
            touched = already_touched.get(marketplace.code)
            if touched is not None:
                result.append(touched)
                self._auth_cursors[cursor_key] = index + 1
                continue
            original = line.snapshot
            if original.payable_amount <= Decimal("0") or not original.can_submit:
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.SKIPPED
                )
                await self.repository.append_event(
                    run.id,
                    "site_skipped",
                    original.skip_reason or "标准订单可用资金为零或页面明确不可提交",
                    marketplace_code=marketplace.code,
                    details={"reason_code": _zero_reason_code(original)},
                )
                _log_site_outcome(
                    marketplace, "SKIPPED", _zero_reason_code(original)
                )
                self._auth_cursors[cursor_key] = index + 1
                continue

            guard_key = operation_guard_key(
                workflow=self.name,
                store_id=run.store.id,
                marketplace_code=marketplace.code,
                settlement_key=original.settlement_key,
                disbursement_date=disbursement_day_key(),
            )
            existing_local = await self.repository.get_operation(guard_key)
            if existing_local is not None:
                if existing_local.intent.run_id == run.id:
                    # 本运行的记录已在循环开头按站点拦下;走到这里说明站点码
                    # 对不上(历史数据),同样只计入、不再 arm。
                    result.append(existing_local)
                else:
                    # Any other record for this store/site/day — settled by an
                    # earlier run, or still in flight — answers this site's money
                    # question already.  Skip THIS SITE and carry on: raising
                    # here used to kill the whole run, so one site that had
                    # already been paid today stopped every remaining site from
                    # being attempted at all.  Never re-arm: that is what keeps
                    # a second payout impossible.
                    await self.repository.set_site_status(
                        run.id, marketplace.code, SiteStatus.SKIPPED
                    )
                    await self.repository.append_event(
                        run.id,
                        "site_skipped",
                        f"今日该站点已有资金记录（{existing_local.state.value}），本次不再重复提交",
                        marketplace_code=marketplace.code,
                        details={
                            "guard_key": guard_key,
                            "guard_state": existing_local.state.value,
                            "guard_run_id": existing_local.intent.run_id,
                            "reason_code": "existing_guard_today",
                        },
                    )
                    _log_site_outcome(
                        marketplace,
                        "SKIPPED",
                        "existing_guard_today",
                        guard_state=existing_local.state.value,
                    )
                self._auth_cursors[cursor_key] = index + 1
                continue
            # Phase 1: navigate to statements before any click, then click only
            # the dashboard button and verify the details page.  Authentication
            # may interrupt this call; engine resumes it in the same session.
            try:
                # Keep this workflow-level check even though the production
                # page adapter repeats it immediately before the dashboard
                # click.  It is the compatibility boundary for every adapter:
                # an approval always binds to the exact amount/identity/DOM
                # snapshot that the administrator reviewed.
                current = await self.page_adapter.read_snapshot(
                    page, run, marketplace
                )
                # Compare the binding, not the balance: a payable amount that
                # grew since planning is normal (more orders settled) and is
                # not a reason to abort.  Seller, marketplace, payout account,
                # settlement cycle and page structure must still match.
                if current.binding_hash != original.binding_hash:
                    raise PlanChanged(
                        "执行前身份、账户、站点或页面结构发生变化"
                    )
                opener = getattr(self.page_adapter, "open_confirmation", None)
                confirmation = (
                    await opener(page, run, marketplace, original)
                    if callable(opener)
                    else current
                )
            except HumanAuthRequired as exc:
                # ``guard_key`` is in scope here: a site whose ARMED/SUBMITTED
                # record already exists must keep parking so the reconcile-only
                # path runs.  Only a site that never armed can be dropped.
                if await self._auto_drops_site_needing_human(
                    run, marketplace, exc, guard_key=guard_key
                ):
                    self._auth_cursors[cursor_key] = index + 1
                    continue
                self._auth_cursors[cursor_key] = index
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.WAITING_AUTH
                )
                raise
            except PlanChanged as exc:
                if run.mode is RunMode.APPROVAL:
                    # An approval binds to one reviewed plan: if any part of it
                    # moved, the whole approval is void.
                    await self.repository.set_approval_status(
                        run.id, ApprovalStatus.INVALIDATED
                    )
                    await self.repository.append_event(
                        run.id,
                        "plan_invalidated",
                        "打开确认页前金额、身份、账户或结构发生变化",
                        marketplace_code=marketplace.code,
                    )
                    raise
                await self.repository.append_event(
                    run.id,
                    "plan_invalidated",
                    "打开确认页前金额、身份、账户或结构发生变化",
                    marketplace_code=marketplace.code,
                )
                await self._fail_site_and_continue(
                    run, marketplace, exc, reason_code="plan_changed"
                )
                self._auth_cursors[cursor_key] = index + 1
                continue
            except PayoutRateLimited as exc:
                # Amazon's own throttle, not a fault of ours and not a fault of
                # this site's data.  Same shape as a zero balance: skip it, keep
                # the money accruing, try again next run.  Caught before the
                # PreflightRejected handler below, which would call it a failure.
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.SKIPPED
                )
                await self.repository.append_event(
                    run.id,
                    "site_skipped",
                    str(exc),
                    marketplace_code=marketplace.code,
                    details={
                        "reason_code": "payout_rate_limited",
                        "retry_after": getattr(exc, "retry_after", None),
                    },
                )
                _log_site_outcome(
                    marketplace,
                    "SKIPPED",
                    "payout_rate_limited",
                    retry_after=getattr(exc, "retry_after", None),
                )
                self._auth_cursors[cursor_key] = index + 1
                continue
            except PreflightRejected as exc:
                # A preflight rejection belongs to this site.  Everything in
                # this ``try`` runs before ``arm_operation``, so no money moved
                # and the other marketplaces are unaffected.  Letting it escape
                # used to fail the whole run: a payout account changed on one
                # marketplace — correctly refused here — stopped every other
                # site from being paid out at all.  ``DomContractError`` is a
                # subclass and keeps the same per-site treatment it had.
                await self._fail_site_and_continue(
                    run, marketplace, exc, reason_code="preflight_rejected"
                )
                self._auth_cursors[cursor_key] = index + 1
                continue
            except PlatformDisbursementExists as duplicate:
                existing_platform = duplicate.result
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.SKIPPED
                )
                await self.repository.append_event(
                    run.id,
                    "platform_duplicate",
                    "亚马逊付款记录已有同日相同金额/结算周期",
                    marketplace_code=marketplace.code,
                    details={
                        "reason_code": "platform_duplicate",
                        "platform_reference": getattr(
                            existing_platform, "platform_reference", None
                        ),
                        "platform_status": getattr(
                            existing_platform, "platform_status", None
                        ),
                    },
                )
                _log_site_outcome(marketplace, "SKIPPED", "platform_duplicate")
                self._auth_cursors[cursor_key] = index + 1
                continue
            except Exception as exc:
                # Intentional process/runtime failures must still reach the
                # engine recovery path; isolate ordinary browser/page errors
                # such as locator timeouts before ARMED.
                if isinstance(exc, RuntimeError):
                    raise
                # A locator timeout or page-contract failure before ARMED is
                # isolated to this marketplace. Continue later sites, while
                # preserving the exact sanitized error for the report.
                await self._fail_site_and_continue(
                    run, marketplace, exc, reason_code="site_execution_failed"
                )
                self._auth_cursors[cursor_key] = index + 1
                continue

            intent = OperationIntent(
                guard_key=guard_key,
                run_id=run.id,
                site_run_key=f"{run.id}:{marketplace.code}",
                store_id=run.store.id,
                marketplace_code=marketplace.code,
                settlement_key=original.settlement_key,
                # The confirmation page's figure is what Amazon is about to
                # transfer; the planned one is only what the dashboard showed
                # moments earlier.  Record the former so the guard, the report
                # and the Feishu card all state the amount actually requested.
                amount=confirmation.payable_amount,
                currency=original.currency,
                plan_hash=plan.plan_hash,
                snapshot_hash=original.snapshot_hash,
                # Where Amazon said this transfer was going.  Recorded, never
                # compared — it is the durable answer if a payout is ever
                # queried, and the only place that answer exists.
                payout_account_tail=confirmation.payment_account,
            )
            armed = await self.repository.arm_operation(intent)
            if not armed.created or armed.state is not GuardState.ARMED:
                # The same "this site's money question is already answered" case
                # as the pre-check above, just discovered one step later — the
                # barrier is wider than ``guard_key`` (uq_financial_operation is
                # unique per settlement cycle, not per day), so it also catches
                # an earlier payout in the same open cycle.  Skip THIS SITE and
                # keep going rather than failing the run: raising here stopped
                # every remaining marketplace from being attempted.  Nothing was
                # armed by this call, so nothing is released.
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.SKIPPED
                )
                await self.repository.append_event(
                    run.id,
                    "site_skipped",
                    f"该站点已有资金防重复记录（{armed.state.value}），本次不再重复提交",
                    marketplace_code=marketplace.code,
                    details={
                        "guard_key": guard_key,
                        "guard_state": armed.state.value,
                        "guard_run_id": armed.intent.run_id,
                        "guard_settlement_key": armed.intent.settlement_key,
                        "reason_code": "existing_guard_cycle",
                    },
                )
                _log_site_outcome(
                    marketplace,
                    "SKIPPED",
                    "existing_guard_cycle",
                    guard_state=armed.state.value,
                )
                self._auth_cursors[cursor_key] = index + 1
                continue
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.ARMED
            )
            await self.repository.append_event(
                run.id,
                "operation_armed",
                "确认页身份和金额已核对；数据库已提交 ARMED，此后任何异常都只收口、绝不重新提交",
                marketplace_code=marketplace.code,
                details={
                    "guard_key": guard_key,
                    "identity_source": confirmation.identity_source,
                    "payout_account_tail": confirmation.payment_account,
                },
            )

            try:
                receipt = await self.page_adapter.submit_once(
                    page, run, marketplace, confirmation
                )
            except HumanAuthRequired as exc:
                # 亚马逊在最终点击**之前**又要求认证(付款区的 step-up 重认证)。
                #
                # ⚠️ 判据只认适配器给的 ``dispatch_recorded``,它来自写在 ``click()``
                # 上一行的标记。**不能**拿「``submitted_at`` 还空着」当判据 ——
                # 那一列是点击之后才写的,点完抛异常时它同样是空的,照它释放就会把
                # 一笔**已经花出去**的钱的当日屏障删掉(2026-09-26 自审时第一版写错过)。
                # 不知道(None)就当点过了:一个字不动,交回引擎。
                if exc.dispatch_recorded is not False:
                    self._auth_cursors[cursor_key] = index
                    raise
                # 确知没点下去。锁定原样留着的话:引擎按「有说不清的记录」只收口不
                # 续跑,人验证完点继续,整条运行落 UNCERTAIN_FINANCIAL、后面的站点
                # 一个没试,而那笔钱一分没动。释放之后按**未锁定**时的老路径处理:
                # 自动模式跳过该站点,审批/空跑模式照常停下来等人。
                released = await self.repository.release_operation(guard_key)
                await self.repository.append_event(
                    run.id,
                    "operation_released",
                    "最终提交前亚马逊要求重新认证，已撤销本站点的资金锁定；未发出任何提现请求",
                    marketplace_code=marketplace.code,
                    details={
                        "guard_key": guard_key,
                        "released": released,
                        "kind": exc.kind,
                        "reason": _safe_site_error(exc),
                    },
                )
                if not released:
                    # 双保险:``release_operation`` 自己要求 submitted_at 为空。
                    # 删不掉说明这条记录已经记下了派发,与上面的判据矛盾 —— 那就
                    # 什么都不动,交回引擎。
                    self._auth_cursors[cursor_key] = index
                    raise
                if await self._auto_drops_site_needing_human(run, marketplace, exc):
                    self._auth_cursors[cursor_key] = index + 1
                    continue
                self._auth_cursors[cursor_key] = index
                await self.repository.set_site_status(
                    run.id, marketplace.code, SiteStatus.WAITING_AUTH
                )
                raise
            except SubmissionNotDispatched as exc:
                # The adapter proved the irreversible click never happened: the
                # marker it sets one line above ``click()`` was never written.
                # Releasing is therefore not forgetting a payout, it is deleting
                # the record of something that did not occur.  Keeping it would
                # block this site for the rest of the day AND make the next
                # read-back announce a payout nobody ever requested.
                released = await self.repository.release_operation(guard_key)
                await self.repository.append_event(
                    run.id,
                    "operation_released",
                    "确认页最终提交按钮未被点击，已撤销本站点的资金锁定；未发出任何提现请求",
                    marketplace_code=marketplace.code,
                    details={
                        "guard_key": guard_key,
                        "released": released,
                        "reason": _safe_site_error(exc),
                    },
                )
                await self._fail_site_and_continue(
                    run, marketplace, exc, reason_code="submission_not_dispatched"
                )
                self._auth_cursors[cursor_key] = index + 1
                continue
            submitted = await self.repository.transition_operation(
                guard_key,
                expected=(GuardState.ARMED,),
                target=GuardState.SUBMITTED,
                receipt_id=receipt.receipt_id,
                details={"submitted_at": receipt.submitted_at.isoformat()},
            )
            # The single most important fact this workflow produces is that an
            # irreversible payout click was dispatched.  It used to live only in
            # a column of ``operation_guards``; the event stream jumped straight
            # from ``operation_armed`` to a status change, so an operator
            # reading the timeline could not see that money had left.
            await self.repository.append_event(
                run.id,
                "operation_submitted",
                f"已派发不可撤销的请求付款点击：{original.currency} {confirmation.payable_amount}",
                marketplace_code=marketplace.code,
                details={
                    "guard_key": guard_key,
                    "amount": str(confirmation.payable_amount),
                    "planned_amount": str(original.payable_amount),
                    "currency": original.currency,
                    "submitted_at": receipt.submitted_at.isoformat(),
                    "receipt_id": receipt.receipt_id,
                    # Whether the success banner was actually seen on the page
                    # right after the click; absence is not failure.
                    "observed_status": receipt.observed_status,
                },
            )
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.SUBMITTED
            )
            _log_site_outcome(marketplace, "SUBMITTED", "dispatched")
            # 提交完当场收口,不再回头去亚马逊查。见 _settle_one 的说明。
            result.append(await self._settle_one(run, marketplace, submitted))
            self._auth_cursors[cursor_key] = index + 1
        self._auth_cursors.pop(cursor_key, None)
        return tuple(result)

    async def reconcile(
        self,
        run: WorkflowRun,
        page: Any,
        operations: Sequence[OperationRecord] | None = None,
    ) -> Sequence[OperationRecord]:
        """把还没收口的资金记录一次性落到终态。**不联网。**

        这里原本会开浏览器去亚马逊结算页回读每一笔到没到。运营 2026-09-23 要求
        删掉那套:它只在那次任务里跑约一分钟(最多三次),而亚马逊常隔天才把提现
        显示到结算页——代码库自己早就写下了结论(repositories.py:113-122):
        「the read-back **structurally cannot resolve it**」。于是它几乎必然失败、
        留下一条 UNCERTAIN、下次服务启动再排一次回读、再开一次紫鸟、再失败。
        实测面板上堆了 43 笔,最老的一条是 09-10 的。

        现在按**本地已有事实**判,当场落终态:

        * ``submitted_at`` 非空 —— 那一下不可逆点击**返回过**,请求确实发出去了。
          停在 ``SUBMITTED``,这就是终态。
        * ``submitted_at`` 为空(只 ARMED)—— 系统**从没看到**点击返回,不知道点没点。
          落 ``UNCERTAIN``,留给人工。

        ⚠️ **不自动落 CONFIRMED。**「已确认」的含义是「在亚马逊那边看到了这笔」,
        而我们不再去看;自动写 CONFIRMED 就是声称一件没验证过的事。
        ``CONFIRMED`` 今后只由人工结案写入(运行详情页那颗「我已在亚马逊核对，结案」)。

        ⚠️ 防重提现不受影响:它靠的是 ``guard_key`` UNIQUE 的**存在性**,
        不是状态(见 plan 里引的 workflow.py:441-448)。
        """

        del page  # 收口不需要页面;形参留着是为了不动 Workflow 协议
        records = tuple(
            operations
            or await self.repository.list_operations(
                run.id,
                states=(GuardState.ARMED, GuardState.SUBMITTED, GuardState.UNCERTAIN),
            )
        )
        by_code = {item.code: item for item in run.marketplaces}
        result: list[OperationRecord] = []
        for record in records:
            marketplace = by_code.get(record.intent.marketplace_code)
            if marketplace is None:
                # 站点配置后来被删了。已提交 / 已结案的记录**有结论**,原样保留 ——
                # 把一笔成功的提现改成 UNCERTAIN 是在说假话(自审抓到过)。
                # 只有从没看到点击返回的 ARMED 才需要人裁定。
                if record.state is GuardState.ARMED and not record.dispatch_recorded:
                    record = await self.repository.transition_operation(
                        record.intent.guard_key,
                        expected=(record.state,),
                        target=GuardState.UNCERTAIN,
                        details={"reason": "站点配置已不存在"},
                    )
                result.append(record)
                continue
            result.append(await self._settle_one(run, marketplace, record))
        return tuple(result)

    async def _settle_one(
        self,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        record: OperationRecord,
    ) -> OperationRecord:
        """按本地事实给一条资金记录收口。**不开浏览器、不轮询、不 sleep。**

        判据只有一个:那一下不可逆点击**返回过**没有。它写在 ``submitted_at`` 上
        (``record.dispatch_recorded``),是本地记录,不需要问亚马逊。
        """

        if record.state in (GuardState.CONFIRMED, GuardState.UNCERTAIN):
            # 已有结论(人工结案)或已在等人裁定:自动化没什么可做的,**原样返回**,
            # 也不去动站点状态 —— 否则会把一个等人的站点静默改写成 SUBMITTED。
            # (SQLAlchemy 仓储的 ``set_site_status`` 不校验站点状态机,写错了不会报错。)
            return record

        if record.dispatch_recorded:
            # 请求确实发出去了。``SUBMITTED`` 就是终态——不自动升成 CONFIRMED,
            # 因为「已确认」意味着在亚马逊那边看到了这笔,而我们不再去看。
            # execute 在点击之后已经写过站点状态并记过一行日志,这里只在恢复路径
            # (崩在点击之后、写站点之前)补齐站点状态,不再重复记日志。
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.SUBMITTED
            )
            return record

        # 从没看到点击返回。声称「已提交」就是在这个流程唯一必须说准的事情上
        # 无凭据地断言,所以只说已知的,交给人工(运行详情页的「确认未发出，释放」)。
        uncertain = await self.repository.transition_operation(
            record.intent.guard_key,
            expected=(GuardState.ARMED,),
            target=GuardState.UNCERTAIN,
            details={
                "reason": "未能确认是否已发出提现请求（系统没有记录到派发）",
                "dispatch_recorded": False,
            },
        )
        await self.repository.set_site_status(
            run.id, marketplace.code, SiteStatus.UNCERTAIN_FINANCIAL
        )
        _log_site_outcome(marketplace, "UNCERTAIN_FINANCIAL", "dispatch_not_recorded")
        return uncertain

    async def report(self, run: WorkflowRun, status: RunStatus) -> WorkflowReport:
        operations = await self.repository.list_operations(run.id)
        fields = {
            "store": run.store.name,
            "mode": run.mode.value,
            "marketplaces": [item.code for item in run.marketplaces],
            "operations": [
                {
                    "marketplace": item.intent.marketplace_code,
                    "currency": item.intent.currency,
                    "amount": str(item.intent.amount),
                    "state": item.state.value,
                    "reference": item.receipt_id,
                }
                for item in operations
            ],
        }
        summary = {
            RunStatus.SUCCEEDED: "任务完成；已发出的提现请求见站点明细。",
            RunStatus.WAITING_APPROVAL: "金额清单已生成，请在本地后台审核。",
            RunStatus.WAITING_AUTH: "浏览器等待人工完成安全验证。",
            RunStatus.NEEDS_HUMAN_AUTH: "人工验证等待已超时。",
            RunStatus.UNCERTAIN_FINANCIAL: (
                "有资金记录无法确认是否已发出提现请求；请去亚马逊卖家后台核对，"
                "再在运行详情页结案或释放。切勿重新提交。"
            ),
            RunStatus.FAILED: "任务在资金提交前失败。",
            RunStatus.CANCELLED: "任务已取消。",
        }.get(status, f"任务状态：{status.value}")
        return WorkflowReport(
            run_id=run.id,
            status=status,
            title=f"紫鸟提现 · {run.store.name}",
            summary=summary,
            fields=fields,
        )


def _safe_site_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


