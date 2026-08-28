"""Build and deliver safe Feishu notices from explicit SQLite facts.

This module is the persistence boundary for notifications.  It deliberately
does not inspect ``result_summary``, ``SiteRun.details``, event ``details``,
guard metadata or approval plan JSON.  The objects it returns therefore cannot
accidentally transport a browser/session payload to Feishu.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import re
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
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
from .dto import NotificationKind, SafeRunNotice, SafeSiteNotice

logger = logging.getLogger(__name__)

_ALLOWED_KINDS = frozenset(NotificationKind)

# A run of this workflow moves no money, so every payout phrase in this module
# would be a false statement about it.
FEEDBACK_WORKFLOW_KEY = "amazon_feedback_removal"


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
        card: PAYMENT_CONFIRMED requires at least one CONFIRMED operation guard.
        Conversely, RUN_COMPLETED is rejected when such a guard exists, so a
        real payment confirmation can never be downgraded to a neutral card.
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
                    RunEvent.event_type == "site_skipped",
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

        confirmed_guards = [row for row in guards if row.state == "CONFIRMED"]
        if kind is NotificationKind.PAYMENT_CONFIRMED and not confirmed_guards:
            return None
        if kind is NotificationKind.RUN_COMPLETED and confirmed_guards:
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

        summary = _summary_for(
            kind,
            workflow=run_row.workflow,
            feedback_counts=feedback_counts,
            run_mode=run_row.mode,
            run_error=run_row.error,
            site_payables=[row.payable_amount for row in site_rows],
            site_errors=[row.error for row in site_rows if row.error],
            site_skip_reasons=[
                reason
                for reason in (skip_reasons.get(row.id, "") for row in site_rows)
                if reason
            ],
            event_message=(latest_event.message if latest_event else None),
        )
        sites: list[SafeSiteNotice] = []
        for row in site_rows:
            guard = guard_by_site.get(row.id)
            # The destination Amazon showed for this payout.  A site with no
            # guard moved no money and therefore has no destination to report.
            account = (guard.payout_account_tail if guard else "") or ""
            earlier_tail = previous_tail.get(row.id, "")
            is_feedback = run_row.workflow == FEEDBACK_WORKFLOW_KEY
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
            title=_title_for(kind, run_row.store_name, run_row.workflow),
            summary=summary,
            run_short_id=run_row.id[:8],
            store_name=run_row.store_name,
            workflow=run_row.workflow or "",
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
            run_id, kind, site_run_id=site_run_id
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

    async def notify_waiting_approval(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.WAITING_APPROVAL)

    async def notify_waiting_auth(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.WAITING_AUTH)

    async def notify_auth_timeout(self, run_id: str) -> bool:
        return await self.deliver(run_id, NotificationKind.AUTH_TIMEOUT)

    async def notify_payment_confirmed(
        self, run_id: str, *, site_run_id: str | None = None
    ) -> bool:
        return await self.deliver(
            run_id,
            NotificationKind.PAYMENT_CONFIRMED,
            site_run_id=site_run_id,
        )

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
                if row.status in ("SENT", "PENDING"):
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


def _without_exception_prefix(value: str | None) -> str:
    """Drop a leading exception class name from an operator-facing sentence."""

    return _EXCEPTION_PREFIX.sub("", " ".join(str(value or "").split()))


def _feedback_summary(
    kind: NotificationKind,
    counts: dict[str, int],
    *,
    run_mode: str,
    run_error: str | None,
    site_errors: list[str],
    event_message: str | None,
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
    if str(run_mode).lower() == "dry_run":
        return (
            f"只读检查已完成，未提交任何请求审核。"
            f"待提交 {pending} 条，待人工 {needs_human} 条，此前已请求 {already} 条。"
        )
    tail = f"待人工 {needs_human} 条，未确认 {uncertain} 条，此前已请求 {already} 条。"
    if submitted:
        return f"已提交 {submitted} 条请求审核；亚马逊是否采纳由其决定。{tail}"
    return f"本次没有提交任何请求审核。{tail}"


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
) -> str:
    if feedback_counts is not None or workflow == FEEDBACK_WORKFLOW_KEY:
        return _feedback_summary(
            kind,
            feedback_counts or {},
            run_mode=run_mode,
            run_error=run_error,
            site_errors=site_errors,
            event_message=event_message,
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
        return "任务已完成，但没有已确认的提现记录；这不代表提现成功。"
    return {
        NotificationKind.WAITING_APPROVAL: "金额清单已生成，等待本地管理员审核。",
        NotificationKind.WAITING_AUTH: "自动登录尝试已停止，对应紫鸟店铺窗口正在等待人工处理。",
        NotificationKind.AUTH_TIMEOUT: "登录验证在等待期限内未完成。",
        NotificationKind.PAYMENT_CONFIRMED: "已从亚马逊付款记录回读确认本次结果。",
        NotificationKind.RUN_FAILED: "任务在资金提交前失败。",
        NotificationKind.RUN_PARTIAL: "部分站点已处理，另有站点执行失败。",
        # The read-back runs at most three times over about a minute, while
        # Amazon usually needs hours — often until the next day — before a
        # disbursement appears in the statements page.  "结果不明确" therefore
        # read like a failure when the truthful statement is that the request
        # went out and the platform has not published its result yet.
        NotificationKind.UNCERTAIN_FINANCIAL: (
            "已提交提现请求，结果等待审核；亚马逊通常数小时甚至次日才显示。"
        ),
        NotificationKind.CROSS_DAY_STARTED: "该任务排队至次日后开始执行。",
    }[kind]


def _title_for(
    kind: NotificationKind, store_name: str, workflow: str = ""
) -> str:
    if workflow == FEEDBACK_WORKFLOW_KEY:
        label = {
            NotificationKind.WAITING_AUTH: "等待登录验证",
            NotificationKind.AUTH_TIMEOUT: "登录验证超时",
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
        NotificationKind.AUTH_TIMEOUT: "登录验证超时",
        NotificationKind.PAYMENT_CONFIRMED: "提现已确认",
        NotificationKind.RUN_COMPLETED: "任务检查已完成（非提现确认）",
        NotificationKind.RUN_FAILED: "任务失败",
        NotificationKind.RUN_PARTIAL: "任务部分失败",
        # Not "已跳过": that reads as "the automation declined to work".  Every
        # site here was checked; none of them had anything to send.
        NotificationKind.RUN_SKIPPED: "本次无站点可提现",
        # Not "待审核": that is WAITING_APPROVAL above, and it means the local
        # operator must act.  Here the request is already with Amazon.
        NotificationKind.UNCERTAIN_FINANCIAL: "已发出，平台尚未显示",
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
    if kind is NotificationKind.PAYMENT_CONFIRMED:
        return "CONFIRMED" in guard_states
    if kind is NotificationKind.RUN_COMPLETED:
        return run_status == "SUCCEEDED" and "CONFIRMED" not in guard_states
    if kind is NotificationKind.RUN_FAILED:
        return run_status == "FAILED"
    if kind is NotificationKind.RUN_PARTIAL:
        return run_status == "PARTIAL"
    if kind is NotificationKind.RUN_SKIPPED:
        return run_status == "SKIPPED"
    if kind is NotificationKind.UNCERTAIN_FINANCIAL:
        return run_status == "UNCERTAIN_FINANCIAL" or "UNCERTAIN" in guard_states
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
