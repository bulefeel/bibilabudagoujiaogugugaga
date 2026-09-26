"""Deterministic 1–3 star feedback removal plugin.

State machine, per the anchors agreed with the operator:

    open list → read rows → per row: dedupe → classify → open panel →
    select reason → ⚠️ submit → read back

Only ``submit`` is irreversible.  Amazon accepts one removal request per
feedback, ever, so every uncertain path stops instead of guessing: an entry
that cannot be classified, cannot be matched to its row, or whose panel does
not read back exactly as intended is left for a human and never retried.

This workflow creates no ``operation_guards`` rows.  Its "never retry"
guarantee lives in ``feedback_reviews``' unique constraint instead, which is
per feedback rather than the guard's per site per day.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Sequence

from ..contracts import WorkflowRepository
from ..errors import HumanAuthRequired
from ..types import (
    MarketplaceRef,
    MarketplaceSnapshot,
    OperationRecord,
    PlanLine,
    RunStatus,
    SiteStatus,
    WorkflowPlan,
    WorkflowReport,
    WorkflowRun,
    utc_now,
)
from .config import MAX_RATING_ELIGIBLE_FOR_REMOVAL, FeedbackDomContract, reason_label
from .classifier import FeedbackReasonClassifier
from .page import AmazonFeedbackPage, FeedbackRow
from .review_store import (
    ALREADY_REQUESTED,
    AMAZON_REMOVED,
    FAILED,
    NEEDS_HUMAN,
    PENDING,
    SUBMITTED,
    UNCERTAIN,
    FeedbackReviewStore,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_SUBMISSIONS_PER_RUN = 20
# How many previously-undecided entries one run will show the model again.
# Re-judging is what stops NEEDS_HUMAN being a dead end, but an operator who
# never resolves them should not pay for the whole backlog every run.
MAX_REJUDGE_PER_SITE = 20


def _log_item(
    marketplace_code: str, outcome: str, reason_code: str, **extra: Any
) -> None:
    """Log codes and counts only.

    Buyer wording never reaches the log file — it is exactly the kind of
    content the project keeps out of ``data/logs`` alongside amounts, payout
    account tails and seller names.  The order id is also withheld; the
    Feedback page in the console is where an operator looks an entry up.
    """

    detail = "".join(
        f" {key}={value}" for key, value in extra.items() if value not in (None, "")
    )
    logger.info(
        "feedback_item: marketplace=%s outcome=%s reason_code=%s%s",
        marketplace_code,
        outcome,
        reason_code,
        detail,
        extra={"marketplace": marketplace_code, "event": "feedback_item"},
    )


class AmazonFeedbackWorkflow:
    name = "amazon_feedback_removal"

    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        review_store: FeedbackReviewStore,
        classifier: FeedbackReasonClassifier,
        page_adapter: AmazonFeedbackPage | None = None,
    ) -> None:
        self.repository = repository
        self.review_store = review_store
        self.classifier = classifier
        self.page_adapter = page_adapter or AmazonFeedbackPage()

    # ------------------------------------------------------------------
    # Workflow protocol
    # ------------------------------------------------------------------
    async def preflight(self, run: WorkflowRun, page: Any) -> None:
        if not run.marketplaces:
            raise ValueError("该任务没有可执行的站点")

    async def plan(self, run: WorkflowRun, page: Any) -> WorkflowPlan:
        """Read every site's list and decide a reason for each new low-star row.

        Classification happens here rather than at execution so that DRY_RUN
        and APPROVAL show the operator exactly what would be submitted.
        """

        lines: list[PlanLine] = []
        for marketplace in run.marketplaces:
            actionable = await self._plan_marketplace(run, page, marketplace)
            lines.append(
                PlanLine(
                    marketplace=marketplace,
                    snapshot=_feedback_snapshot(marketplace, actionable),
                )
            )
        # A site with nothing to do still gets a line so the run records that
        # it was read; the engine only skips execution when *no* line exists.
        if not any(line.snapshot.can_submit for line in lines):
            return WorkflowPlan(self.name, run.id, run.store.id, ())
        return WorkflowPlan(self.name, run.id, run.store.id, tuple(lines))

    async def _plan_marketplace(
        self, run: WorkflowRun, page: Any, marketplace: MarketplaceRef
    ) -> int:
        # Create the site row before anything else.  Two reasons: every site
        # the run touched has to appear in the report — the card used to list
        # only the ones that went wrong, because a site that read fine and had
        # nothing to do never produced a row at all — and ``append_event``
        # binds an event to its site by looking the row up, so events logged
        # before it exists are orphaned.
        await self.repository.set_site_status(
            run.id, marketplace.code, SiteStatus.PREFLIGHT
        )

        outcome = await self.page_adapter.open_list(page, marketplace.domain)
        page = getattr(outcome, "page", None) or page
        if not outcome.ok:
            if outcome.reason == "auth_required":
                # 会话掉了。交给引擎的人工接管路径,**不能**当成「这个站点没活儿」。
                _log_item(marketplace.code, "NEEDS_HUMAN_AUTH", "auth_required")
                raise HumanAuthRequired(
                    f"{marketplace.code} 站点需要重新登录后才能读取反馈管理页",
                    kind="signin",
                )
            # ⚠️ 只说**看见了什么**。原来这里写死「该店铺未开通此站点」——
            # 那是没读过就下的断言,而自动选站已经替我们读过那一页了
            # (用户 2026-09-24 拍板,2026-09-26 实现)。
            details: dict[str, Any] = {
                "reason_code": outcome.reason or "marketplace_unavailable"
            }
            if outcome.reason == "marketplace_not_selected":
                if outcome.switch_reason == "not_offered":
                    seen = "选择页上没有与此站点完全一致的国家（带「待註冊」等后缀的不算）"
                elif outcome.switch_reason == "list_not_loaded":
                    seen = "选择页上的站点列表一直没有加载出来"
                else:
                    seen = f"自动选站没有完成（{outcome.switch_reason or '原因未知'}）"
                message = (
                    f"页面停在账户/站点选择页，{seen}，本站点跳过；"
                    "请在紫鸟窗口确认该账号是否开通了此站点："
                    "若已开通，在该页选定一次即可；若没有，请在店铺设置里关闭此站点"
                )
                details["switch_reason"] = outcome.switch_reason
            else:
                message = "页面未落在反馈管理页，也不是账户/站点选择页，本站点跳过"
            await self.repository.append_event(
                run.id,
                "feedback_site_unavailable",
                message,
                marketplace_code=marketplace.code,
                details=details,
            )
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.SKIPPED
            )
            _log_item(marketplace.code, "SKIPPED", outcome.reason or "unavailable")
            return 0

        rows, truncated = await self.page_adapter.read_all_rows(page)
        if truncated:
            await self.repository.append_event(
                run.id,
                "feedback_list_truncated",
                "反馈列表页数超过上限，本次只读了前面若干页",
                marketplace_code=marketplace.code,
                details={"reason_code": "page_cap_reached"},
            )
            _log_item(marketplace.code, "PARTIAL", "page_cap_reached")
        if not rows:
            await self.repository.append_event(
                run.id,
                "feedback_list_empty",
                "该站点没有读到任何反馈（通常是这个站点还没有评价）",
                marketplace_code=marketplace.code,
                details={"reason_code": "no_rows"},
            )
            _log_item(marketplace.code, "EMPTY", "no_rows")
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.SKIPPED
            )
            return 0

        known = await self.review_store.known_order_ids(
            int(run.store.id), marketplace.code
        )

        seen = len(rows)
        low_star = [row for row in rows if row.is_low_star]
        # A row whose action menu could not be inspected tells us nothing, so it
        # is left un-recorded for a later run rather than being written down
        # under a guess.  Recording is one-way: the unique constraint means a
        # wrong verdict here can never be revisited.
        unreadable = [row for row in low_star if not row.menu_readable]
        readable = [row for row in low_star if row.menu_readable]
        fresh = [row for row in readable if row.order_id not in known]

        for row in fresh:
            state, category, reason_code, source, note = await self._decide(row)
            review_id = await self.review_store.record_candidate(
                run_id=run.id,
                store_id=int(run.store.id),
                marketplace_code=marketplace.code,
                order_id=row.order_id,
                rating=row.rating,
                order_date=row.order_date,
                comment=row.comment,
                amazon_removed=row.amazon_removed,
                state=state,
                category=category,
                reason_code=reason_code,
                decision_source=source,
                decision_note=note,
            )
            if review_id is None:
                # Lost a race with a concurrent run; the other one owns it.
                _log_item(marketplace.code, "SKIPPED", "already_recorded")
                continue
            _log_item(marketplace.code, state, reason_code or "none")

        await self._rejudge_undecided(run, marketplace, rows)

        # Everything still waiting to be sent for this store and site — not just
        # what this run just wrote down.  Entries decided by an earlier dry run,
        # or by an operator on the Feedback page, are exactly as actionable.
        actionable = len(
            await self.review_store.pending_for_store(
                int(run.store.id), marketplace.code
            )
        )

        await self.repository.append_event(
            run.id,
            "feedback_scanned",
            f"读取 {seen} 条反馈，其中中差评 {len(low_star)} 条，"
            f"本次新增 {len(fresh)} 条，待提交合计 {actionable} 条",
            marketplace_code=marketplace.code,
            details={
                "seen": seen,
                "low_star": len(low_star),
                "fresh": len(fresh),
                "actionable": actionable,
                "already_known": len(readable) - len(fresh),
                "menu_unreadable": len(unreadable),
            },
        )
        if unreadable:
            await self.repository.append_event(
                run.id,
                "feedback_menu_unreadable",
                f"{len(unreadable)} 条的操作菜单没读出来，本次不记录，留待下次",
                marketplace_code=marketplace.code,
                details={"count": len(unreadable), "reason_code": "menu_unreadable"},
            )
            _log_item(marketplace.code, "DEFERRED", "menu_unreadable", count=len(unreadable))
        # 读成功了。有待办 → PLANNED(卡片写「待提交」);一条都没有才是 SKIPPED
        # (「已读取，无需提交」)。空跑不会走到 execute,站点就停在这一格 ——
        # 原来一律 SKIPPED,于是每张空跑卡都对着有 2 条待办的站点写
        # 「已读取，无需提交」(2026-09-26 自审)。execute 成功提交后升成 CONFIRMED。
        await self.repository.set_site_status(
            run.id,
            marketplace.code,
            SiteStatus.PLANNED if actionable else SiteStatus.SKIPPED,
        )
        return actionable

    async def _rejudge_undecided(
        self,
        run: WorkflowRun,
        marketplace: MarketplaceRef,
        rows: list[FeedbackRow],
    ) -> None:
        """Give previously-undecided entries another look.

        An entry judged by an older, more cautious prompt would otherwise sit in
        NEEDS_HUMAN forever: ``known_order_ids`` ignores state, so it is never
        re-recorded, and only PENDING entries are ever submitted.  Nothing has
        been sent for these, so looking again costs nothing but a model call.
        """

        stuck = await self.review_store.needs_human_for_store(
            int(run.store.id), marketplace.code
        )
        if not stuck:
            return
        by_order = {row.order_id: row for row in rows}
        considered = stuck[:MAX_REJUDGE_PER_SITE]
        resolved = 0

        for item in considered:
            row = by_order.get(item.order_id)
            # Gone from the page, or Amazon has since handled it — either way
            # there is nothing to submit, so do not pay for a judgement.
            if row is None or not row.removal_available or row.amazon_removed:
                continue
            decision = await self.classifier.classify(
                comment=row.comment, rating=row.rating, order_date=row.order_date
            )
            if decision is None:
                continue
            if await self.review_store.set_decision(
                item.id,
                category=decision.category,
                reason_code=decision.reason_code,
                decision_source="ai",
                decision_note=decision.note,
                run_id=run.id,
            ):
                resolved += 1
                _log_item(marketplace.code, PENDING, decision.reason_code)

        if resolved or len(stuck) > len(considered):
            await self.repository.append_event(
                run.id,
                "feedback_rejudged",
                f"重新判定了 {len(considered)} 条此前「待人工」的反馈，"
                f"其中 {resolved} 条已给出原因",
                marketplace_code=marketplace.code,
                details={
                    "considered": len(considered),
                    "resolved": resolved,
                    "skipped_over_cap": max(0, len(stuck) - len(considered)),
                },
            )

    async def _decide(
        self, row: FeedbackRow
    ) -> tuple[str, str | None, str | None, str | None, str | None]:
        """Return the ledger state and reason for one row.

        Fail closed: anything short of a whitelisted, confident decision ends
        up waiting for a human rather than being submitted on a guess.
        """

        if row.amazon_removed:
            # Amazon struck it out and said why.  It handles FBA delivery
            # complaints without being asked, so there was never anything for
            # this workflow to do here.
            return (
                AMAZON_REMOVED,
                None,
                None,
                None,
                "亚马逊已自行剔除这条评论（正文划掉并附了说明）",
            )
        if not row.removal_available:
            # The action is gone but Amazon did not claim the removal, so per
            # the seller someone has already requested a review for it.  The one
            # request is spent either way — nothing to classify, no model call.
            return (
                ALREADY_REQUESTED,
                None,
                None,
                None,
                "亚马逊已不提供「请求审核」：此前已发起过请求",
            )

        decision = await self.classifier.classify(
            comment=row.comment, rating=row.rating, order_date=row.order_date
        )
        if decision is None:
            return (NEEDS_HUMAN, None, None, None, "自动判定未给出明确原因，待人工选择")
        return (
            PENDING,
            decision.category,
            decision.reason_code,
            "ai",
            decision.note,
        )

    async def execute(
        self, run: WorkflowRun, page: Any, plan: WorkflowPlan
    ) -> Sequence[OperationRecord]:
        """⚠️ The only path that submits.  Returns no guards by design."""

        budget = _max_submissions(run)
        submitted = 0

        for line in plan.lines:
            marketplace = line.marketplace
            if submitted >= budget:
                await self._note_budget_stop(run, marketplace, submitted, budget)
                break
            try:
                submitted += await self._execute_marketplace(
                    run, page, marketplace, budget - submitted
                )
            except Exception as exc:  # noqa: BLE001 - one site must not sink the rest
                # Isolating a site is deliberate; hiding a defect is not.  The
                # traceback goes to the log, because a broad catch here once
                # turned an AttributeError into a plausible-looking "this site
                # failed" and the tests still passed.
                logger.exception(
                    "feedback site failed", extra={"marketplace": marketplace.code}
                )
                await self.repository.set_site_status(
                    run.id,
                    marketplace.code,
                    SiteStatus.FAILED,
                    error=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
                await self.repository.append_event(
                    run.id,
                    "site_execution_failed",
                    "该站点反馈处理失败，其余站点继续",
                    marketplace_code=marketplace.code,
                    details={"reason_code": "site_error"},
                )
                _log_item(marketplace.code, "FAILED", "site_error")
        return ()

    async def _execute_marketplace(
        self,
        run: WorkflowRun,
        page: Any,
        marketplace: MarketplaceRef,
        remaining: int,
    ) -> int:
        pending = await self.review_store.pending_for_store(
            int(run.store.id), marketplace.code
        )
        if not pending:
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.SKIPPED
            )
            return 0

        # Create the site row before anything else.  Two reasons: every site
        # the run touched has to appear in the report — the card used to list
        # only the ones that went wrong, because a site that read fine and had
        # nothing to do never produced a row at all — and ``append_event``
        # binds an event to its site by looking the row up, so events logged
        # before it exists are orphaned.
        await self.repository.set_site_status(
            run.id, marketplace.code, SiteStatus.PREFLIGHT
        )

        outcome = await self.page_adapter.open_list(page, marketplace.domain)
        page = getattr(outcome, "page", None) or page
        if outcome.reason == "auth_required":
            raise HumanAuthRequired(
                f"{marketplace.code} 站点需要重新登录后才能提交请求审核",
                kind="signin",
            )
        if not outcome.ok:
            # 走到执行阶段说明计划已经读到了待提交的条目:这时打不开列表是
            # **没做成**,不是「这站没活儿」。原来一条事件都不写就记 SKIPPED,
            # 于是卡片写「本次没有可处理的反馈」,而正文同时列着 2 条待提交
            # (2026-09-26 自审;GPSR 同一处已经这么修过)。
            await self.repository.append_event(
                run.id,
                "feedback_site_unavailable",
                "执行阶段重新打开反馈管理页失败，计划阶段读到的待提交条目本次没有提交",
                marketplace_code=marketplace.code,
                details={
                    "reason_code": outcome.reason or "marketplace_unavailable",
                    "during_execute": True,
                },
            )
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.FAILED
            )
            _log_item(
                marketplace.code, "FAILED", outcome.reason or "unavailable"
            )
            return 0
        page_rows, _ = await self.page_adapter.read_all_rows(page)
        rows = {row.order_id: row for row in page_rows}

        submitted = 0
        for item in pending:
            if submitted >= remaining:
                await self._note_budget_stop(run, marketplace, submitted, _max_submissions(run))
                break
            outcome, rows = await self._submit_one(
                run, page, marketplace, item, rows
            )
            if outcome == SUBMITTED:
                submitted += 1

        if pending and not submitted:
            # 有待办、却一条都没提交成(原因码不在候选里、面板打不开、行不见了……)。
            # 记 CONFIRMED 的话卡片写「已提交请求 / 反馈处理已完成 / 无需操作」,
            # 而正文同时说「本次没有提交任何请求审核」—— 自相矛盾且站点那行是假的
            # (2026-09-26 自审)。
            await self.repository.append_event(
                run.id,
                "feedback_site_nothing_submitted",
                f"{len(pending)} 条待提交全部没能发出，请到反馈处理台逐条核对",
                marketplace_code=marketplace.code,
                details={
                    "reason_code": "nothing_submitted",
                    "pending": len(pending),
                },
            )
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.FAILED
            )
            _log_item(marketplace.code, "FAILED", "nothing_submitted", pending=len(pending))
            return 0
        # CONFIRMED is this enum's terminal success; SiteStatus has no SUCCEEDED
        # member, and referring to one raised AttributeError at the very end of
        # a site that had already submitted its requests.
        await self.repository.set_site_status(
            run.id, marketplace.code, SiteStatus.CONFIRMED
        )
        return submitted

    async def _submit_one(
        self,
        run: WorkflowRun,
        page: Any,
        marketplace: MarketplaceRef,
        item: Any,
        rows: dict[str, FeedbackRow],
    ) -> tuple[str, dict[str, FeedbackRow]]:
        """One entry.  Returns its outcome and the rows to judge the next
        one against: confirming a submission reloads the list, so the
        caller's copy is stale from that point on."""

        category, reason_code = item.category, item.reason_code
        if not category or not reason_code:
            await self.review_store.mark(item.id, NEEDS_HUMAN, run_id=run.id)
            _log_item(marketplace.code, NEEDS_HUMAN, "no_reason_recorded")
            return NEEDS_HUMAN, rows

        row = rows.get(item.order_id)
        if row is None:
            await self.review_store.mark(
                item.id, FAILED, run_id=run.id,
                details={"reason_code": "row_disappeared"},
            )
            _log_item(marketplace.code, FAILED, "row_disappeared")
            return FAILED, rows
        if not row.is_low_star:
            # The list changed underneath us.  Never act on a row that is no
            # longer a 1–3 star entry.
            await self.review_store.mark(
                item.id, FAILED, run_id=run.id,
                details={"reason_code": "rating_changed"},
            )
            _log_item(marketplace.code, FAILED, "rating_changed", rating=row.rating)
            return FAILED, rows

        try:
            opened = await self.page_adapter.open_removal_panel(page, item.order_id)
            if not opened.get("ok"):
                return await self._abort_item(
                    run, marketplace, item, str(opened.get("reason") or "panel_failed")
                ), rows

            chosen = await self.page_adapter.select_reason(
                page, item.order_id, category, reason_code
            )
            if not chosen.get("ok"):
                return await self._abort_item(
                    run, marketplace, item, str(chosen.get("reason") or "reason_failed")
                ), rows

            result = await self.page_adapter.submit_removal(
                page, item.order_id, category, reason_code
            )
            if not result.get("ok"):
                return await self._abort_item(
                    run, marketplace, item, str(result.get("reason") or "submit_blocked")
                ), rows
        finally:
            # Always return the table to its resting state, whatever happened.
            await self.page_adapter.close_panels(page)

        # The click went through.  Confirm by re-reading the row's own menu:
        # once a review is requested Amazon stops offering request_removal.
        confirmed, fresh = await self.page_adapter.confirm_removal_requested(
            page, marketplace.domain, item.order_id
        )
        if fresh:
            rows = {row.order_id: row for row in fresh}
        state = SUBMITTED if confirmed else UNCERTAIN
        await self.review_store.mark(
            item.id,
            state,
            run_id=run.id,
            submitted_at=utc_now(),
            details={
                "category": category,
                "reason_code": reason_code,
                "reason_label": reason_label(category, reason_code),
            },
        )
        await self.repository.append_event(
            run.id,
            "feedback_removal_submitted",
            "已提交请求审核" if confirmed else "已点击提交，但页面未确认",
            marketplace_code=marketplace.code,
            details={"reason_code": reason_code, "category": category, "state": state},
        )
        _log_item(marketplace.code, state, reason_code)
        return state, rows

    async def _abort_item(
        self, run: WorkflowRun, marketplace: MarketplaceRef, item: Any, reason_code: str
    ) -> str:
        # Nothing was submitted, but the entry is never handed back to a later
        # run: it stays visible on the Feedback page for a human to decide.
        # The action disappearing between planning and execution means someone
        # requested a review in the meantime — the same terminal fact, not a
        # transient failure to retry.
        state = ALREADY_REQUESTED if reason_code == "removal_not_offered" else FAILED
        await self.review_store.mark(
            item.id, state, run_id=run.id, details={"reason_code": reason_code}
        )
        _log_item(marketplace.code, state, reason_code)
        return state

    async def _note_budget_stop(
        self, run: WorkflowRun, marketplace: MarketplaceRef, done: int, budget: int
    ) -> None:
        await self.repository.append_event(
            run.id,
            "feedback_budget_reached",
            f"已达到本次运行的提交上限（{budget} 条），其余留到下一次",
            marketplace_code=marketplace.code,
            details={"submitted": done, "budget": budget},
        )
        _log_item(marketplace.code, "STOPPED", "budget_reached", submitted=done)

    async def reconcile(
        self,
        run: WorkflowRun,
        page: Any,
        operations: Sequence[OperationRecord] | None = None,
    ) -> Sequence[OperationRecord]:
        """No guards are ever armed, so there is nothing to reconcile."""
        return ()

    async def report(self, run: WorkflowRun, status: RunStatus) -> WorkflowReport:
        counts = await self.review_store.counts_for_run(run.id)
        submitted = counts.get(SUBMITTED, 0)
        needs_human = counts.get(NEEDS_HUMAN, 0)
        summary = (
            f"提交 {submitted} 条，待人工 {needs_human} 条，"
            f"此前已请求 {counts.get(ALREADY_REQUESTED, 0)} 条，"
            f"亚马逊已自行剔除 {counts.get(AMAZON_REMOVED, 0)} 条"
        )
        if run.mode.value == "dry_run":
            summary = f"空跑：本次不提交。可提交 {counts.get(PENDING, 0)} 条，" + summary
        return WorkflowReport(
            run_id=run.id,
            status=status,
            title=f"{run.store.name} · 1-3 星反馈删除",
            summary=summary,
            fields={
                "店铺": run.store.name,
                "站点": "、".join(item.code for item in run.marketplaces),
                "已提交": submitted,
                "待人工": needs_human,
                "未确认": counts.get(UNCERTAIN, 0),
                "失败": counts.get(FAILED, 0),
            },
        )


def _max_submissions(run: WorkflowRun) -> int:
    raw = (run.workflow_config or {}).get("max_submissions_per_run")
    if raw is None:
        return DEFAULT_MAX_SUBMISSIONS_PER_RUN
    value = int(raw)
    if value < 1:
        raise ValueError("单次运行提交上限必须至少为 1")
    return value


def _feedback_snapshot(
    marketplace: MarketplaceRef, actionable: int
) -> MarketplaceSnapshot:
    """A non-financial line, expressed in the engine's shared plan type.

    The money fields are structurally required by ``MarketplaceSnapshot`` and
    are zero here; the feedback figures that matter live in ``feedback_reviews``
    and in this run's events.
    """

    return MarketplaceSnapshot(
        marketplace_code=marketplace.code,
        domain=marketplace.domain,
        seller_id="",
        payment_account="",
        currency=marketplace.currency,
        payable_amount=Decimal("0"),
        delayed_amount=Decimal("0"),
        settlement_key=f"feedback:{marketplace.code}",
        can_submit=actionable > 0,
        contract_version=FeedbackDomContract().version,
        page_fingerprint=f"feedback-list:{marketplace.code}",
        skip_reason=None if actionable else "no_actionable_feedback",
    )


__all__ = [
    "MAX_RATING_ELIGIBLE_FOR_REMOVAL",
    "AmazonFeedbackWorkflow",
]
