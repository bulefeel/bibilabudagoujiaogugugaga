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
        outcome = await self.page_adapter.open_list(page, marketplace.domain)
        if not outcome.ok:
            # Seller Central bounced us to the account switcher, which is what
            # it does when the store does not sell on this marketplace.  That
            # is a fact about the store, not a fault: the other sites must
            # still run, and the whole run must not be reported as failed.
            await self.repository.append_event(
                run.id,
                "feedback_site_unavailable",
                "该店铺未开通此站点（页面跳转到账户/站点选择），本站点跳过",
                marketplace_code=marketplace.code,
                details={"reason_code": outcome.reason or "marketplace_unavailable"},
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
        return actionable

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

        outcome = await self.page_adapter.open_list(page, marketplace.domain)
        if not outcome.ok:
            await self.repository.set_site_status(
                run.id, marketplace.code, SiteStatus.SKIPPED
            )
            return 0
        page_rows, _ = await self.page_adapter.read_all_rows(page)
        rows = {row.order_id: row for row in page_rows}

        submitted = 0
        for item in pending:
            if submitted >= remaining:
                await self._note_budget_stop(run, marketplace, submitted, _max_submissions(run))
                break
            outcome = await self._submit_one(run, page, marketplace, item, rows)
            if outcome == SUBMITTED:
                submitted += 1

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
    ) -> str:
        category, reason_code = item.category, item.reason_code
        if not category or not reason_code:
            await self.review_store.mark(item.id, NEEDS_HUMAN, run_id=run.id)
            _log_item(marketplace.code, NEEDS_HUMAN, "no_reason_recorded")
            return NEEDS_HUMAN

        row = rows.get(item.order_id)
        if row is None:
            await self.review_store.mark(
                item.id, FAILED, run_id=run.id,
                details={"reason_code": "row_disappeared"},
            )
            _log_item(marketplace.code, FAILED, "row_disappeared")
            return FAILED
        if not row.is_low_star:
            # The list changed underneath us.  Never act on a row that is no
            # longer a 1–3 star entry.
            await self.review_store.mark(
                item.id, FAILED, run_id=run.id,
                details={"reason_code": "rating_changed"},
            )
            _log_item(marketplace.code, FAILED, "rating_changed", rating=row.rating)
            return FAILED

        try:
            opened = await self.page_adapter.open_removal_panel(page, item.order_id)
            if not opened.get("ok"):
                return await self._abort_item(
                    run, marketplace, item, str(opened.get("reason") or "panel_failed")
                )

            chosen = await self.page_adapter.select_reason(
                page, item.order_id, category, reason_code
            )
            if not chosen.get("ok"):
                return await self._abort_item(
                    run, marketplace, item, str(chosen.get("reason") or "reason_failed")
                )

            result = await self.page_adapter.submit_removal(
                page, item.order_id, category, reason_code
            )
            if not result.get("ok"):
                return await self._abort_item(
                    run, marketplace, item, str(result.get("reason") or "submit_blocked")
                )
        finally:
            # Always return the table to its resting state, whatever happened.
            await self.page_adapter.close_panels(page)

        # The click went through.  Confirm by re-reading the row's own menu:
        # once a review is requested Amazon stops offering request_removal.
        confirmed = await self._confirm_submitted(page, item.order_id)
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
        return state

    async def _confirm_submitted(self, page: Any, order_id: str) -> bool:
        """Confirm from the page, not from the fact that we clicked.

        Amazon withdraws 「请求审核」 from a feedback once a review has been
        requested for it, so the action being gone is a positive signal that the
        request landed.  A row that has vanished, or one that still offers the
        action, is left UNCERTAIN — and UNCERTAIN is never retried either.
        """

        return await self.page_adapter.wait_until_action_gone(page, order_id)

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
