"""Build and deliver safe Feishu notices from explicit SQLite facts.

This module is the persistence boundary for notifications.  It deliberately
does not inspect ``result_summary``, ``SiteRun.details``, event ``details``,
guard metadata or approval plan JSON.  The objects it returns therefore cannot
accidentally transport a browser/session payload to Feishu.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..db import utc_now
from ..models import (
    ApprovalRequest,
    FeedbackReview,
    NotificationDelivery,
    OperationGuard,
    Run,
    RunEvent,
    Schedule,
    SiteRun,
    Store,
    StoreMarketplace,
)
from .dto import (
    NotificationKind,
    SafeFeedbackNotice,
    SafeRunNotice,
    SafeSiteNotice,
)

logger = logging.getLogger(__name__)

_ALLOWED_KINDS = frozenset(NotificationKind)

# A run of this workflow moves no money, so every payout phrase in this module
# would be a false statement about it.
FEEDBACK_WORKFLOW_KEY = "amazon_feedback_removal"
GPSR_WORKFLOW_KEY = "amazon_gpsr_attestation"

# 这些流程一分钱都不碰，所以卡片上任何金额字段都会是一句假话：站点行会渲染成
# 「EUR 0.00 可提现」，读起来像这家店提不出钱。
NON_FINANCIAL_WORKFLOWS = frozenset({FEEDBACK_WORKFLOW_KEY, GPSR_WORKFLOW_KEY})
FEEDBACK_PENDING = "PENDING"
# Entries whose submit button was actually clicked.  UNCERTAIN belongs here:
# the request went out, only the read-back could not confirm it.
FEEDBACK_DISPATCHED_STATES = ("SUBMITTED", "UNCERTAIN")
# Cards are read on a phone.  Beyond this the list stops being scannable and
# the count in the summary is the better answer.
FEEDBACK_CARD_LIMIT = 10
FEEDBACK_COMMENT_CHARS = 60

# Event types whose ``message`` explains what happened to one site.  Only
# the message is read, never ``details`` — these strings are composed by
# this codebase, so nothing off a page can ride along.  Without the
# feedback entries here a card listed only the sites that went wrong and
# silently omitted every site that was read successfully.
SITE_REASON_EVENTS = (
    "site_skipped",
    "feedback_scanned",
    "feedback_list_empty",
    "feedback_site_unavailable",
    "feedback_list_truncated",
    # ⚠️ 只列**站点级**事件。``gpsr_candidate`` 是逐 ASIN 的，放进来会一条条把
    # 站点汇总覆盖掉，卡片上只剩最后一个商品的名字。
    "gpsr_scanned",
    "gpsr_list_empty",
    "gpsr_site_unavailable",
    "gpsr_unknown_reason",
    # 站点为什么失败 / 为什么一条没提交。漏了它们,卡片的原因栏里只剩计划阶段
    # 那句「待提交 N 个」,被当成失败原因显示(09-23 自审复核)。
    "gpsr_site_failed",
    "gpsr_nothing_to_submit",
    "gpsr_panel_stuck",
    "gpsr_canary_failed",
    "gpsr_site_incomplete",
    "gpsr_site_completed",
    "feedback_site_nothing_submitted",
    # 提现:点击前的平台幂等检查查到同日同额记录 → 这一站跳过。
    "platform_duplicate",
)


class SafeNoticeSender(Protocol):
    async def send(self, notice: SafeRunNotice) -> None: ...


class DatabaseNoticeBuilder:
    """Read a run's notification facts through an explicit field allow-list."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def build(
        self,
        run_id: str,
        kind: NotificationKind,
        *,
        next_action: str = "",
        site_run_id: str | None = None,
        now: datetime | None = None,
    ) -> SafeRunNotice | None:
        """Return a safe DTO, or ``None`` when database facts do not justify it.

        A caller cannot turn a generic ``SUCCEEDED`` run into a green payment
        card: PAYMENT_SUBMITTED requires at least one dispatched guard
        (SUBMITTED/CONFIRMED) and none left unsettled.  Conversely,
        RUN_COMPLETED is rejected when a payout went out, so a real payout can
        never be downgraded to a card that says 「本次没有发出提现」.
        """

        if not isinstance(kind, NotificationKind) or kind not in _ALLOWED_KINDS:
            raise TypeError("kind must be a NotificationKind")
        now = now or utc_now()
        with self.session_factory() as session:
            run_row = session.execute(
                select(
                    Run.id,
                    Run.status,
                    Run.mode,
                    Run.trigger,
                    Run.scheduled_for_at,
                    Run.started_at,
                    Run.auth_deadline,
                    Run.error,
                    Run.workflow,
                    Run.store_id,
                    Store.name.label("store_name"),
                    Schedule.name.label("schedule_name"),
                    Schedule.timezone.label("schedule_timezone"),
                )
                .join(Store, Store.id == Run.store_id)
                .outerjoin(Schedule, Schedule.id == Run.schedule_id)
                .where(Run.id == run_id)
            ).one_or_none()
            if run_row is None:
                return None

            site_stmt = (
                select(
                    SiteRun.id,
                    SiteRun.marketplace_code,
                    SiteRun.status,
                    SiteRun.currency,
                    SiteRun.payable_amount,
                    SiteRun.delayed_amount,
                    SiteRun.error,
                )
                .where(SiteRun.run_id == run_id)
                .order_by(SiteRun.marketplace_code)
            )
            if site_run_id is not None:
                site_stmt = site_stmt.where(SiteRun.id == site_run_id)
            site_rows = session.execute(site_stmt).all()

            # A benign skip records its reason only in the event stream, so a
            # card built from SiteRun alone could say "已跳过" and nothing more.
            # Only ``message`` is read here — event ``details`` stays outside
            # this module's whitelist.
            skip_reasons: dict[str, str] = {}
            for row in session.execute(
                select(RunEvent.site_run_id, RunEvent.message)
                .where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type.in_(SITE_REASON_EVENTS),
                    RunEvent.site_run_id.is_not(None),
                )
                .order_by(RunEvent.id)
            ):
                skip_reasons[row.site_run_id] = row.message or ""

            guards = session.execute(
                select(
                    OperationGuard.site_run_id,
                    OperationGuard.marketplace_code,
                    OperationGuard.state,
                    OperationGuard.amount,
                    OperationGuard.currency,
                    OperationGuard.payout_account_tail,
                    OperationGuard.armed_at,
                    OperationGuard.store_id,
                )
                .where(OperationGuard.run_id == run_id)
                .order_by(OperationGuard.marketplace_code)
            ).all()
            guard_by_site = {row.site_run_id: row for row in guards}

            # The tail Amazon showed for the payout *before* this one, per
            # marketplace.  Only used to say "this differs from last time" —
            # the transfer has already happened either way.
            previous_tail: dict[str, str] = {}
            for row in guards:
                if not row.payout_account_tail:
                    continue
                earlier = session.scalar(
                    select(OperationGuard.payout_account_tail)
                    .where(
                        OperationGuard.store_id == row.store_id,
                        OperationGuard.marketplace_code == row.marketplace_code,
                        OperationGuard.armed_at < row.armed_at,
                        OperationGuard.payout_account_tail.is_not(None),
                    )
                    .order_by(OperationGuard.armed_at.desc())
                    .limit(1)
                )
                if earlier:
                    previous_tail[row.site_run_id] = earlier

            pending_approval = session.execute(
                select(ApprovalRequest.status, ApprovalRequest.expires_at)
                .where(ApprovalRequest.run_id == run_id)
                .order_by(ApprovalRequest.created_at.desc())
                .limit(1)
            ).one_or_none()

            # Event message is the only selected event payload.  ``details``
            # is never queried. It is used solely for a cleaned human-readable
            # failure explanation when Run/SiteRun.error is empty.
            # 这条运行到底有没有真的停下来等过人。自动模式是当场跳过,从不进
            # WAITING_AUTH —— 卡片文案要按这个事实分岔(2026-09-26 自审)。
            waited_for_human = bool(
                session.scalar(
                    select(RunEvent.id)
                    .where(
                        RunEvent.run_id == run_id,
                        RunEvent.to_status == "WAITING_AUTH",
                    )
                    .limit(1)
                )
            )
            # 这次停下来等人,是卡在哪一步:登录、验证码,还是选站?
            park = session.execute(
                select(RunEvent.details)
                .where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type.in_(
                        ("human_auth_required", "site_needs_human_auth")
                    ),
                )
                .order_by(RunEvent.id.desc())
                .limit(1)
            ).one_or_none()
            park_kind = str(((park.details if park else None) or {}).get("kind") or "")
            latest_event = session.execute(
                select(
                    RunEvent.event_type,
                    RunEvent.message,
                    RunEvent.to_status,
                    RunEvent.created_at,
                )
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
                .limit(1)
            ).one_or_none()

        # 「发出去了」= 看到点击返回的(SUBMITTED)或人工结案的(CONFIRMED)。
        dispatched_codes = sorted(
            {
                str(row.marketplace_code).upper()
                for row in guards
                if row.state in ("SUBMITTED", "CONFIRMED")
            }
        )
        unsettled_guards = [row for row in guards if row.state in ("ARMED", "UNCERTAIN")]
        if kind is NotificationKind.PAYMENT_SUBMITTED and (
            not dispatched_codes or unsettled_guards
        ):
            return None
        if kind is NotificationKind.RUN_COMPLETED and dispatched_codes:
            return None
        if kind is NotificationKind.WAITING_APPROVAL and (
            pending_approval is None or pending_approval.status != "PENDING"
        ):
            return None
        if not _kind_matches_facts(
            kind,
            run_status=run_row.status,
            guard_states={row.state for row in guards},
            scheduled_for=_as_utc(run_row.scheduled_for_at),
            started_at=_as_utc(run_row.started_at),
            schedule_timezone=run_row.schedule_timezone,
        ):
            return None

        # A run that moves no money must not be described in payout language.
        # Counting here rather than in the card builder keeps the notice a
        # plain data object with no database access of its own.
        feedback_counts: dict[str, int] | None = None
        feedback_backlog: dict[str, int] = {}
        feedback_items: list[SafeFeedbackNotice] = []
        feedback_omitted = 0
        if run_row.workflow == FEEDBACK_WORKFLOW_KEY:
            with self.session_factory() as session:
                feedback_counts = {}
                for (state,) in session.execute(
                    select(FeedbackReview.state).where(
                        FeedbackReview.run_id == run_id
                    )
                ):
                    key = str(state)
                    feedback_counts[key] = feedback_counts.get(key, 0) + 1

                # What is still waiting for this STORE, not just this run.  A
                # card that only reports the run's own delta says "nothing to
                # handle" while an entry sits waiting for the operator to pick
                # a reason — which is exactly what happened.
                for (state,) in session.execute(
                    select(FeedbackReview.state).where(
                        FeedbackReview.store_id == run_row.store_id,
                        FeedbackReview.state.in_(("PENDING", "NEEDS_HUMAN")),
                    )
                ):
                    key = str(state)
                    feedback_backlog[key] = feedback_backlog.get(key, 0) + 1

                # A dry run has not sent anything, so the interesting entries
                # are the ones it *would* send; a real run's are the ones it
                # actually clicked.
                wanted = (
                    (FEEDBACK_PENDING,)
                    if str(run_row.mode).lower() == "dry_run"
                    else FEEDBACK_DISPATCHED_STATES
                )
                rows = session.execute(
                    select(
                        FeedbackReview.order_id,
                        FeedbackReview.rating,
                        FeedbackReview.marketplace_code,
                        FeedbackReview.amazon_removed,
                        FeedbackReview.comment,
                        FeedbackReview.category,
                        FeedbackReview.reason_code,
                        FeedbackReview.state,
                    )
                    .where(
                        FeedbackReview.run_id == run_id,
                        FeedbackReview.state.in_(wanted),
                    )
                    .order_by(
                        FeedbackReview.marketplace_code,
                        FeedbackReview.rating,
                        FeedbackReview.created_at,
                    )
                ).all()
                feedback_omitted = max(0, len(rows) - FEEDBACK_CARD_LIMIT)
                for row in rows[:FEEDBACK_CARD_LIMIT]:
                    feedback_items.append(
                        SafeFeedbackNotice(
                            order_id=str(row.order_id or ""),
                            rating=int(row.rating or 0),
                            marketplace_code=str(row.marketplace_code or ""),
                            amazon_removed=bool(row.amazon_removed),
                            comment=_clip(row.comment, FEEDBACK_COMMENT_CHARS),
                            reason_label=_reason_label(row.category, row.reason_code),
                            state=str(row.state or ""),
                        )
                    )

        summary = _summary_for(
            kind,
            workflow=run_row.workflow,
            feedback_counts=feedback_counts,
            feedback_backlog=feedback_backlog,
            run_mode=run_row.mode,
            run_error=run_row.error,
            site_payables=[row.payable_amount for row in site_rows],
            site_errors=[row.error for row in site_rows if row.error],
            has_sites=bool(site_rows),
            site_skip_reasons=[
                reason
                for reason in (skip_reasons.get(row.id, "") for row in site_rows)
                if reason
            ],
            event_message=(latest_event.message if latest_event else None),
            event_type=(latest_event.event_type if latest_event else None),
            waited_for_human=waited_for_human,
            park_kind=park_kind,
            dispatched_codes=dispatched_codes,
        )
        sites: list[SafeSiteNotice] = []
        for row in site_rows:
            guard = guard_by_site.get(row.id)
            # The destination Amazon showed for this payout.  A site with no
            # guard moved no money and therefore has no destination to report.
            account = (guard.payout_account_tail if guard else "") or ""
            earlier_tail = previous_tail.get(row.id, "")
            is_feedback = run_row.workflow in NON_FINANCIAL_WORKFLOWS
            sites.append(
                SafeSiteNotice(
                    code=row.marketplace_code,
                    currency=""
                    if is_feedback
                    else ((guard.currency if guard else row.currency) or ""),
                    payable=None
                    if is_feedback
                    else (guard.amount if guard else row.payable_amount),
                    delayed=None if is_feedback else row.delayed_amount,
                    outcome=(guard.state if guard else row.status) or "",
                    account_tail=account,
                    # Platform references live in free-form guard metadata and
                    # are deliberately omitted by this database builder.
                    reference_suffix="",
                    # A recorded error is the precise cause; otherwise the skip
                    # event says why this site was left alone.
                    reason=_without_exception_prefix(
                        row.error or skip_reasons.get(row.id, "")
                    ),
                    account_changed=bool(
                        account and earlier_tail and account != earlier_tail
                    ),
                )
            )

        deadline = None
        if kind is NotificationKind.WAITING_APPROVAL and pending_approval is not None:
            deadline = _as_utc(pending_approval.expires_at)
        elif kind in (NotificationKind.WAITING_AUTH, NotificationKind.AUTH_TIMEOUT):
            deadline = _as_utc(run_row.auth_deadline)

        delay = None
        scheduled = _as_utc(run_row.scheduled_for_at)
        started = _as_utc(run_row.started_at)
        if scheduled is not None and started is not None:
            delay = max(0, int((started - scheduled).total_seconds() // 60))

        return SafeRunNotice(
            kind=kind,
            title=_title_for(
                kind, run_row.store_name, run_row.workflow, run_row.mode or ""
            ),
            summary=summary,
            run_short_id=run_row.id[:8],
            store_name=run_row.store_name,
            workflow=run_row.workflow or "",
            feedback_items=tuple(feedback_items),
            feedback_omitted=feedback_omitted,
            mode=run_row.mode,
            trigger=run_row.trigger,
            schedule_name=run_row.schedule_name or "",
            scheduled_for=scheduled,
            started_at=started,
            queue_delay_minutes=delay,
            next_action=next_action,
            sites=tuple(sites),
            deadline_at=deadline,
        )


class NotificationDeliveryService:
    """Persistently de-duplicate and send database-built notifications.

    ``deliver`` reserves the key before network I/O.  SENT rows are permanent
    no-ops. FAILED rows may be retried using the same key.  Notification errors
    are contained here and never update Run, SiteRun, Approval or Guard state.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        sender: SafeNoticeSender,
        *,
        builder: DatabaseNoticeBuilder | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.sender = sender
        self.builder = builder or DatabaseNoticeBuilder(session_factory)

    async def deliver(
        self,
        run_id: str,
        kind: NotificationKind,
        *,
        dedupe_key: str | None = None,
        site_run_id: str | None = None,
        next_action: str = "",
        now: datetime | None = None,
    ) -> bool:
        if not isinstance(kind, NotificationKind) or kind not in _ALLOWED_KINDS:
            raise TypeError("kind must be a NotificationKind")
        notice = self.builder.build(
            run_id,
            kind,
            next_action=next_action,
            site_run_id=site_run_id,
            now=now,
        )
        if notice is None:
            return False
        key = dedupe_key or notification_dedupe_key(
            run_id,
            kind,
            site_run_id=site_run_id,
            occurrence=self._occurrence(run_id, kind),
        )
        if not self._reserve(
            run_id=run_id,
            site_run_id=site_run_id,
            kind=kind,
            dedupe_key=key,
        ):
            return False
        try:
            await self.sender.send(notice)
        except Exception as exc:
            self._mark_failed(key, _safe_delivery_error(exc))
            logger.warning(
                "notification_delivery_failed run_id=%s kind=%s",
                run_id[:8],
                kind.value,
            )
            return False
        self._mark_sent(key, now=now)
        return True

    def _occurrence(self, run_id: str, kind: NotificationKind) -> str:
        """同一条运行里**可以发生多次**的通知,每一次各自一张卡。

        多站点运行每个站点各要登录一次(``assoc_handle`` 各不相同),于是
        「等待登录验证」一条运行里可能出现三四次。去重键只有 运行+类型 的话,
        第二次起全被当成重复吞掉,紫鸟窗口就在没人知道的情况下等满 30 分钟
        (09-23 自审复核)。用「这是第几次进入该状态」区分;同一次的重试仍然去重。
        """

        target = {
            NotificationKind.WAITING_AUTH: "WAITING_AUTH",
            NotificationKind.AUTH_TIMEOUT: "NEEDS_HUMAN_AUTH",
        }.get(kind)
        if target is None:
            return ""
        with self.session_factory() as session:
            count = session.scalar(
                select(func.count(RunEvent.id)).where(
                    RunEvent.run_id == run_id, RunEvent.to_status == target
                )
            )
        return str(int(count or 0))

    async def notify_waiting_approval(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.WAITING_APPROVAL)

    async def notify_waiting_auth(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.WAITING_AUTH)

    async def notify_auth_timeout(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.AUTH_TIMEOUT)

    async def notify_run_completed(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.RUN_COMPLETED)

    async def notify_run_failed(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.RUN_FAILED)

    async def notify_run_partial(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.RUN_PARTIAL)

    async def notify_run_skipped(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.RUN_SKIPPED)

    async def notify_uncertain_financial(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.UNCERTAIN_FINANCIAL)

    async def notify_cross_day_started(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.CROSS_DAY_STARTED)

    def _reserve(
        self,
        *,
        run_id: str,
        site_run_id: str | None,
        kind: NotificationKind,
        dedupe_key: str,
    ) -> bool:
        with self.session_factory() as session:
            row = session.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.dedupe_key == dedupe_key
                )
            )
            if row is not None:
                if row.status == "SENT":
                    return False
                if row.status == "PENDING" and not _is_abandoned(row, now=utc_now()):
                    return False
                if row.run_id != run_id or row.site_run_id != site_run_id:
                    # A custom key must never be re-used to mutate another
                    # run/site's receipt, even after the first attempt failed.
                    return False
                row.status = "PENDING"
                row.kind = kind.value
                row.attempts += 1
                row.last_error = None
                session.commit()
                return True
            if session.get(Run, run_id) is None:
                return False
            if site_run_id is not None:
                belongs = session.scalar(
                    select(SiteRun.id).where(
                        SiteRun.id == site_run_id,
                        SiteRun.run_id == run_id,
                    )
                )
                if belongs is None:
                    return False
            session.add(
                NotificationDelivery(
                    run_id=run_id,
                    site_run_id=site_run_id,
                    dedupe_key=dedupe_key,
                    kind=kind.value,
                    status="PENDING",
                    attempts=1,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                # A parallel worker won the unique dedupe-key reservation.
                session.rollback()
                return False
            return True

    def _mark_sent(self, dedupe_key: str, *, now: datetime | None) -> None:
        with self.session_factory() as session:
            row = session.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.dedupe_key == dedupe_key
                )
            )
            if row is None:
                return
            row.status = "SENT"
            row.sent_at = now or utc_now()
            row.last_error = None
            session.commit()

    def _mark_failed(self, dedupe_key: str, message: str) -> None:
        with self.session_factory() as session:
            row = session.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.dedupe_key == dedupe_key
                )
            )
            if row is None:
                return
            row.status = "FAILED"
            row.last_error = message
            session.commit()


# 一条 PENDING 回执超过这么久还没有结局,只可能是**进程在发送途中被杀**
# (Stop.bat / 托盘退出 / 安装器升级在 Windows 上都是硬杀)。飞书单次调用超时
# 10 秒,给它宽出一大截。留着不管的话,那张卡 —— 包括红色的「资金结果待人工核对」
# —— 永远不会再发一次(2026-09-26 自审)。
_ABANDONED_PENDING = timedelta(minutes=5)


def _is_abandoned(row: NotificationDelivery, *, now: datetime) -> bool:
    stamp = row.updated_at or row.created_at
    if stamp is None:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return now - stamp > _ABANDONED_PENDING


def notification_dedupe_key(
    run_id: str,
    kind: NotificationKind,
    *,
    site_run_id: str | None = None,
    occurrence: str = "",
) -> str:
    """Create a stable opaque key without storing business content."""

    if not isinstance(kind, NotificationKind) or kind not in _ALLOWED_KINDS:
        raise TypeError("kind must be a NotificationKind")
    raw = "|".join((run_id, kind.value, site_run_id or "", occurrence))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{run_id[:8]}:{kind.value.lower()}:{digest}"


_EXCEPTION_PREFIX = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*"
    r"(?:Error|Rejected|Required|Exists|Changed|Dispatched|Limited|Operation|Expired)"
    r"\s*[:：]\s*"
)


def _clip(value: str | None, limit: int) -> str:
    """One line, length-capped.  Newlines would break the card's list layout."""

    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _reason_label(category: str | None, reason_code: str | None) -> str:
    if not category or not reason_code:
        return ""
    from ..workflows.amazon_feedback.config import REASON_CATALOG

    return REASON_CATALOG.get(str(category), {}).get(str(reason_code), "")


def _without_exception_prefix(value: str | None) -> str:
    """Drop a leading exception class name from an operator-facing sentence."""

    return _EXCEPTION_PREFIX.sub("", " ".join(str(value or "").split()))


def _gpsr_summary(
    kind: NotificationKind,
    *,
    run_mode: str,
    run_error: str | None,
    site_errors: list[str],
    site_skip_reasons: list[str],
    event_message: str | None,
    has_sites: bool = False,
) -> str:
    """GPSR 卡片的正文。

    这条流程没有台账表——「未开始 / 审核中」写在亚马逊页面上，所以数字只能来自
    站点级运行事件的正文。那些正文是本模块所在代码库自己拼的，页面上的任何内容
    都搭不了顺风车。
    """

    if kind in (NotificationKind.RUN_FAILED, NotificationKind.RUN_PARTIAL):
        reason = run_error or (site_errors[0] if site_errors else None) or event_message
        if reason:
            return f"GPSR 安全证明异常：{_without_exception_prefix(reason)}"
        return "GPSR 安全证明未能完成。"

    detail = "；".join(
        message for message in site_skip_reasons if message
    ) or (event_message or "")
    # ⚠️ 「已完成」只配用在确实提交了的运行上。全跳过(RUN_SKIPPED)的运行一条都没
    # 提交 —— 2026-09-18 正香那张卡停在账户/站点选择页、正文却写「GPSR 安全证明已完成」。
    if str(run_mode).lower() == "dry_run":
        head = "空跑：本次只读取与判定，未提交任何安全证明。"
    elif kind is NotificationKind.RUN_SKIPPED:
        head = "本次没有提交任何安全证明。"
    else:
        head = "GPSR 安全证明已完成。"
    if detail:
        return f"{head}{detail}"
    if has_sites:
        return f"{head}各站点读取情况见下方。"
    return head


def _feedback_summary(
    kind: NotificationKind,
    counts: dict[str, int],
    *,
    run_mode: str,
    run_error: str | None,
    site_errors: list[str],
    event_message: str | None,
    has_sites: bool = False,
    backlog: dict[str, int] | None = None,
) -> str:
    submitted = counts.get("SUBMITTED", 0)
    pending = counts.get("PENDING", 0)
    needs_human = counts.get("NEEDS_HUMAN", 0)
    uncertain = counts.get("UNCERTAIN", 0)
    already = counts.get("ALREADY_REQUESTED", 0)

    if kind in (NotificationKind.RUN_FAILED, NotificationKind.RUN_PARTIAL):
        reason = run_error or (site_errors[0] if site_errors else None) or event_message
        if reason:
            return f"反馈处理异常：{_without_exception_prefix(reason)}"
        return "反馈处理未能完成。"
    removed = counts.get("AMAZON_REMOVED", 0)

    waiting = backlog or {}
    waiting_human = waiting.get("NEEDS_HUMAN", 0)
    waiting_pending = waiting.get("PENDING", 0)
    outstanding = ""
    if waiting_human or waiting_pending:
        outstanding = (
            f"该店铺目前还有 {waiting_human} 条待人工"
            f"（去反馈处理台选原因）、{waiting_pending} 条待提交。"
        )

    if not any(counts.values()):
        # Every number zero means this run found nothing NEW — usually because
        # the store's feedback was all handled by earlier runs.  Printing four
        # zeros made that look like a broken run.
        head = (
            "本次没有发现需要处理的新反馈；各站点读取情况见下方。"
            if has_sites
            else "本次没有发现需要处理的新反馈。"
        )
        return f"{head}{outstanding}" if outstanding else head

    seen = (
        f"待提交 {pending} 条，待人工 {needs_human} 条，"
        f"此前已请求 {already} 条，亚马逊已剔除 {removed} 条。"
    )
    if str(run_mode).lower() == "dry_run":
        return f"只读检查已完成，未提交任何请求审核。{seen}{outstanding}"
    # 「发出去了」= SUBMITTED + UNCERTAIN。UNCERTAIN 的意思是**点了但页面没确认**,
    # 那一下同样不可逆(本模块自己的注释:the request went out)。只数 SUBMITTED 的话,
    # 两条都点出去却全是 UNCERTAIN 时,卡片写「本次没有提交任何请求审核」,而同一张卡的
    # 「本次已提交」底下正列着这两条(2026-09-26 自审)。
    dispatched = submitted + uncertain
    if dispatched:
        unconfirmed = f"（其中 {uncertain} 条页面未确认）" if uncertain else ""
        return (
            f"已提交 {dispatched} 条请求审核{unconfirmed}；亚马逊是否采纳由其决定。"
            f"{seen}{outstanding}"
        )
    return f"本次没有提交任何请求审核。{seen}{outstanding}"


def _summary_for(
    kind: NotificationKind,
    *,
    run_mode: str,
    run_error: str | None,
    site_payables: list[object],
    site_errors: list[str],
    site_skip_reasons: list[str],
    event_message: str | None,
    workflow: str = "",
    feedback_counts: dict[str, int] | None = None,
    feedback_backlog: dict[str, int] | None = None,
    has_sites: bool = False,
    dispatched_codes: list[str] | None = None,
    event_type: str | None = None,
    waited_for_human: bool = True,
    park_kind: str = "",
) -> str:
    if kind is NotificationKind.PAYMENT_SUBMITTED:
        # 用户 2026-09-23 拍板的口径:直说「已提交，到账请自行核对」。
        # 系统不再去亚马逊查到没到账,所以只陈述确知的那件事:请求发出去了。
        codes = dispatched_codes or []
        where = f"（{'、'.join(codes)}）" if codes else ""
        return f"已提交 {len(codes)} 笔提现请求{where}；是否到账请在亚马逊卖家后台核对。"
    # 「被拦在登录页」「验证超时」「排队至次日」描述的是运行停在哪里，与它是哪条
    # 流程无关，所以在分流到各流程之前先答掉。此前这三句只写在提现分支里，GPSR 和
    # 反馈流程各自的汇总函数都够不着它——一次停在德国站登录页、等着人工接管的运行，
    # 飞书卡上写的是「GPSR 安全证明已完成」。默认落到“完成”那一句，是把未知当成功。
    if kind is NotificationKind.WAITING_AUTH:
        if park_kind == "marketplace_switch":
            # 停在账户/站点选择页不是登录问题。说成「自动登录尝试已停止」会把人
            # 指去处理一个根本没发生的登录(2026-09-26 自审)。
            return (
                "页面停在亚马逊账户/站点选择页，自动选站没有完成；"
                "请在紫鸟窗口中选定该站点后回本地后台继续。"
            )
        return "自动登录尝试已停止，对应紫鸟店铺窗口正在等待人工处理。"
    if kind is NotificationKind.AUTH_TIMEOUT:
        if event_type == "AUTH_LEASE_LOST_ON_RESTART":
            return "服务重启后，原来那次人工验证已失效，这次运行没有继续；请在本地后台重新发起。"
        if not waited_for_human:
            # 自动模式不等人:需要登录的站点当场跳过,没有任何等待发生过。
            # 说「等待期限内未完成」是假话(2026-09-26 自审)。
            return "自动模式不等待人工验证，需要登录的站点已跳过；下次运行会重新尝试。"
        return "登录验证在等待期限内未完成。"
    if kind is NotificationKind.CROSS_DAY_STARTED:
        return "该任务排队至次日后开始执行。"
    # GPSR 排在提现分支之前：它没有自己的台账表，汇总完全来自站点级事件的正文，
    # 那些正文是本代码库自己拼的，页面上的任何东西都搭不了顺风车。
    if workflow == GPSR_WORKFLOW_KEY:
        return _gpsr_summary(
            kind,
            run_mode=run_mode,
            run_error=run_error,
            site_errors=site_errors,
            site_skip_reasons=site_skip_reasons,
            event_message=event_message,
            has_sites=has_sites,
        )
    if feedback_counts is not None or workflow == FEEDBACK_WORKFLOW_KEY:
        return _feedback_summary(
            kind,
            feedback_counts or {},
            run_mode=run_mode,
            run_error=run_error,
            site_errors=site_errors,
            event_message=event_message,
            has_sites=has_sites,
            backlog=feedback_backlog or {},
        )
    if kind is NotificationKind.RUN_SKIPPED:
        # Lead with the reason, not the outcome.  "已跳过" alone is what sent
        # the operator looking for a fault that does not exist; the sentence
        # Amazon actually showed — including how long the throttle still has
        # to run — is the whole point of sending this card at all.
        throttled = [
            reason for reason in site_skip_reasons if "24 小时" in reason
        ]
        if throttled:
            return f"本次没有站点发起提现。{throttled[0]}"
        if site_skip_reasons:
            return f"本次没有站点发起提现。{site_skip_reasons[0]}"
        return "本次没有站点发起提现；每个站点的具体原因见下方列表。"
    if kind in (NotificationKind.RUN_FAILED, NotificationKind.RUN_PARTIAL):
        reason = run_error or (site_errors[0] if site_errors else None) or event_message
        if reason:
            # ``PreflightRejected: `` and friends are class names that mean
            # nothing to the person reading the card; the sentence after them
            # was written for that person.
            return f"任务执行异常：{_without_exception_prefix(reason)}"
    if kind is NotificationKind.RUN_COMPLETED:
        is_dry_run = str(run_mode).lower() == "dry_run"
        if not site_payables or all(value is None for value in site_payables):
            if is_dry_run:
                return "只读检查已完成，未读取到站点付款数据，且未执行提现提交；这不代表提现成功。"
            return "任务已完成，未读取到站点付款数据；这不代表提现成功。"
        if all(_is_zero_or_missing(value) for value in site_payables):
            if is_dry_run:
                return "只读检查已完成，当前无可提现资金，且未执行提现提交；这不代表提现成功。"
            return "任务已完成，当前无可提现资金；这不代表提现成功。"
        if is_dry_run:
            return "只读检查已完成，未执行提现提交；这不代表提现成功。"
        # 走到这里说明运行成功、但一笔提现请求都没发出去(发出去了的会是
        # PAYMENT_SUBMITTED)。原来的「没有已确认的提现记录」在删回读之后
        # 会贴到**每一笔成功提现**上,是反话。
        return "任务已完成，本次没有发出提现请求。"
    # WAITING_AUTH / AUTH_TIMEOUT / CROSS_DAY_STARTED 已在函数开头答掉，不再列在这里。
    return {
        NotificationKind.WAITING_APPROVAL: "金额清单已生成，等待本地管理员审核。",
        NotificationKind.RUN_FAILED: "任务在资金提交前失败。",
        NotificationKind.RUN_PARTIAL: "部分站点已处理，另有站点执行失败。",
        # 删回读之后,这个状态只来自「锁了、却没看到点击返回」和人工待裁定的
        # 历史记录 —— 原来那句「已提交提现请求」在这里是无凭据的断言。
        NotificationKind.UNCERTAIN_FINANCIAL: (
            "有资金记录无法确认是否已发出提现请求；请去亚马逊卖家后台核对，"
            "再在运行详情页结案或释放。"
        ),
    }[kind]


def _title_for(
    kind: NotificationKind, store_name: str, workflow: str = "", mode: str = ""
) -> str:
    # 空跑只读取与判定,什么都没提交。标题写「已完成」会被读成「证明已提交 /
    # 反馈已处理」(09-23 自审复核:空跑卡的标题和正文自相矛盾)。
    dry_run_done = kind is NotificationKind.RUN_COMPLETED and mode == "dry_run"
    if workflow == GPSR_WORKFLOW_KEY and dry_run_done:
        return f"GPSR 安全证明 · {store_name} · 空跑完成（未提交）"
    if workflow == FEEDBACK_WORKFLOW_KEY and dry_run_done:
        return f"反馈删除 · {store_name} · 空跑完成（未提交）"
    if workflow == GPSR_WORKFLOW_KEY:
        label = {
            NotificationKind.WAITING_AUTH: "等待登录验证",
            NotificationKind.AUTH_TIMEOUT: "登录验证未完成",
            NotificationKind.RUN_COMPLETED: "安全证明已完成",
            NotificationKind.RUN_FAILED: "安全证明失败",
            NotificationKind.RUN_PARTIAL: "安全证明部分失败",
            # 不说「没有可提交的」:跳过也可能是页面没读到(停在选择页)。只说确知的。
            NotificationKind.RUN_SKIPPED: "本次没有提交安全证明",
            NotificationKind.CROSS_DAY_STARTED: "跨日任务开始",
        }.get(kind)
        if label is not None:
            return f"GPSR 安全证明 · {store_name} · {label}"
    if workflow == FEEDBACK_WORKFLOW_KEY:
        label = {
            NotificationKind.WAITING_AUTH: "等待登录验证",
            NotificationKind.AUTH_TIMEOUT: "登录验证未完成",
            NotificationKind.RUN_COMPLETED: "反馈处理已完成",
            NotificationKind.RUN_FAILED: "反馈处理失败",
            NotificationKind.RUN_PARTIAL: "反馈处理部分失败",
            NotificationKind.RUN_SKIPPED: "本次没有可处理的反馈",
            NotificationKind.CROSS_DAY_STARTED: "跨日任务开始",
        }.get(kind)
        if label is not None:
            return f"反馈删除 · {store_name} · {label}"
    label = {
        NotificationKind.WAITING_APPROVAL: "待审核",
        NotificationKind.WAITING_AUTH: "等待登录验证",
        NotificationKind.AUTH_TIMEOUT: "登录验证未完成",
        NotificationKind.PAYMENT_SUBMITTED: "提现已提交",
        NotificationKind.RUN_COMPLETED: "任务已完成（本次未发出提现）",
        NotificationKind.RUN_FAILED: "任务失败",
        NotificationKind.RUN_PARTIAL: "任务部分失败",
        # 不说「无站点可提现」:站点全跳过也可能是**读不出来**(身份不符、页面结构变了),
        # 那句话会把「没读到」说成「没钱可提」。只陈述确知的:本次没有发起提现。
        NotificationKind.RUN_SKIPPED: "本次没有发起提现",
        # Not "待审核": that is WAITING_APPROVAL above, the local approval queue.
        NotificationKind.UNCERTAIN_FINANCIAL: "资金结果待人工核对",
        NotificationKind.CROSS_DAY_STARTED: "跨日任务开始",
    }[kind]
    return f"紫鸟提现 · {store_name} · {label}"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_delivery_error(exc: Exception) -> str:
    # Persist type only. Exception text may contain URLs, tokens, DOM snippets,
    # absolute paths or full platform identifiers.
    return f"{type(exc).__name__}: 飞书通知发送失败"


def _kind_matches_facts(
    kind: NotificationKind,
    *,
    run_status: str,
    guard_states: set[str],
    scheduled_for: datetime | None,
    started_at: datetime | None,
    schedule_timezone: str | None,
) -> bool:
    if kind is NotificationKind.WAITING_APPROVAL:
        return run_status == "WAITING_APPROVAL"
    if kind is NotificationKind.WAITING_AUTH:
        return run_status == "WAITING_AUTH"
    if kind is NotificationKind.AUTH_TIMEOUT:
        return run_status == "NEEDS_HUMAN_AUTH"
    if kind is NotificationKind.PAYMENT_SUBMITTED:
        return run_status == "SUCCEEDED" and bool(guard_states & {"SUBMITTED", "CONFIRMED"})
    if kind is NotificationKind.RUN_COMPLETED:
        return run_status == "SUCCEEDED" and not guard_states & {"SUBMITTED", "CONFIRMED"}
    if kind is NotificationKind.RUN_FAILED:
        return run_status == "FAILED"
    if kind is NotificationKind.RUN_PARTIAL:
        return run_status == "PARTIAL"
    if kind is NotificationKind.RUN_SKIPPED:
        return run_status == "SKIPPED"
    if kind is NotificationKind.UNCERTAIN_FINANCIAL:
        return run_status == "UNCERTAIN_FINANCIAL" or bool(guard_states & {"ARMED", "UNCERTAIN"})
    if kind is NotificationKind.CROSS_DAY_STARTED:
        if scheduled_for is None or started_at is None:
            return False
        try:
            zone = ZoneInfo(schedule_timezone or "Asia/Singapore")
        except ZoneInfoNotFoundError:
            zone = timezone.utc
        return scheduled_for.astimezone(zone).date() < started_at.astimezone(zone).date()
    return False


def _is_zero_or_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        return float(value) == 0
    except (TypeError, ValueError, OverflowError):
        return False
