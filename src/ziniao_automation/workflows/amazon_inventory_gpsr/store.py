"""SQLite candidate snapshot and one-save ledger for inventory declarations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Sequence

from sqlalchemy import or_, select, text

from ...models import InventoryGpsrItem, InventoryGpsrRequirement, Run, RunEvent, SiteRun, Store
from ..errors import WorkflowError, PageRecoveryRequired
from ..types import utc_now
from .config import InventoryItem, SAFETY_ATTESTATION, RESPONSIBLE_PERSON, MANUFACTURER, INVENTORY_MARKETPLACES
from .definition import AmazonInventoryGpsrConfig

PENDING = "PENDING"
ELIGIBLE = "ELIGIBLE"
NO_VIOLATION_ENTRY = "NO_VIOLATION_ENTRY"
NO_ATTESTATION = "NO_ATTESTATION"
ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
MISSING = "MISSING"
FAILED = "FAILED"
SUBMITTING = "SUBMITTING"
CONFIRMED = "CONFIRMED"
UNCERTAIN = "UNCERTAIN"
BLOCKED = "BLOCKED"
ALREADY_LINKED = "ALREADY_LINKED"
MULTIPLE_RECORDS = "MULTIPLE_RECORDS"
NO_RECORDS = "NO_RECORDS"
INVALID_RECORD = "INVALID_RECORD"
NOT_APPLICABLE = "NOT_APPLICABLE"
INTENT_STATES = frozenset({SUBMITTING, UNCERTAIN, CONFIRMED})
SCAN_EVENT = "inventory_gpsr_scan_complete"
WORKFLOW_KEY = "amazon_inventory_gpsr_attestation"
_LEGACY_HTTP_ERROR = re.compile(
    r"InventoryContractError: 德国站库存接口返回 HTTP (403|429|5\d{2})，本次流程已停止；"
    r"请在紫鸟确认页面恢复后再创建新任务"
)


def legacy_inventory_resume_items(session: Any, run: Run) -> tuple[InventoryGpsrItem, ...] | None:
    """Recognize only the old exact HTTP failure and its final failed item.

    None means ineligible. An empty tuple permits a failure before opening the
    next item; nothing in the snapshot needs rewriting in that case.
    """
    if (run.workflow != WORKFLOW_KEY or run.status != "FAILED"
            or not _LEGACY_HTTP_ERROR.fullmatch(run.error or "")
            or not _scan_exists(session, run.id, "DE")):
        return None
    try:
        validate_inventory_run_binding(session, run, require_baseline=True)
    except RuntimeError:
        return None
    if session.scalar(select(InventoryGpsrItem.id).where(
        InventoryGpsrItem.store_id == run.store_id,
        InventoryGpsrItem.marketplace_code == "DE",
        InventoryGpsrItem.state.in_((SUBMITTING, UNCERTAIN)),
    ).limit(1)) is not None:
        return None
    rows = session.scalars(select(InventoryGpsrItem).where(InventoryGpsrItem.run_id == run.id)).all()
    event = session.scalar(select(RunEvent).where(
        RunEvent.run_id == run.id, RunEvent.event_type == "inventory_gpsr_failed",
    ).order_by(RunEvent.id.desc()).limit(1))
    if event is None:
        return () if any(row.state in {PENDING, ELIGIBLE} for row in rows) else None
    details = event.details or {}
    matching = tuple(row for row in rows if row.sku == details.get("sku")
                     and row.asin == details.get("asin") and row.state == FAILED
                     and row.reason == "page_contract_or_identity_error" and row.intent_at is None)
    if details.get("reason_code") != "page_contract_or_identity_error" or len(matching) != 1:
        return None
    return matching


def can_resume_inventory_run(session: Any, run: Run) -> bool:
    """Read-only eligibility for the explicit operator resume button."""
    if run.workflow != WORKFLOW_KEY or (run.result_summary or {}).get("store_removed"):
        return False
    return run.status == "PAUSED" or legacy_inventory_resume_items(session, run) is not None


def _plan_seller_baseline(run: Run) -> str | None:
    """Read only an original, nonempty plan bound to this run/store/DE site."""
    plan = (run.result_summary or {}).get("workflow_plan_v1")
    if (not isinstance(plan, dict) or plan.get("workflow") != WORKFLOW_KEY
            or plan.get("run_id") != run.id or str(plan.get("store_id")) != str(run.store_id)):
        return None
    lines = plan.get("lines")
    if not isinstance(lines, list) or not lines or not all(isinstance(line, dict) for line in lines):
        return None
    sellers = set()
    for line in lines:
        market, snapshot = line.get("marketplace"), line.get("snapshot")
        if not isinstance(market, dict) or not isinstance(snapshot, dict):
            return None
        code = market.get('code')
        if (code not in INVENTORY_MARKETPLACES or market.get('domain') != INVENTORY_MARKETPLACES[code]
                or snapshot.get('marketplace_code') != code or snapshot.get('domain') != market['domain']):
            return None
        seller = snapshot.get('seller_id')
        if not isinstance(seller, str) or not seller.strip():
            return None
        sellers.add(seller)
    return next(iter(sellers)) if len(sellers) == 1 else None


def validate_inventory_run_binding(session: Any, run: Run, *, require_baseline: bool = False) -> Store:
    """Use current enrollment plus the saved attempt/plan identity, never history labels."""
    store = session.get(Store, run.store_id)
    if (store is None or not store.enabled or not store.identity_confirmed
            or not (store.expected_seller_id or "").strip()):
        raise RuntimeError("库存任务的店铺须启用并完成卖家身份确认")
    summary = run.result_summary or {}
    binding = summary.get("inventory_gpsr_binding")
    has_binding = isinstance(binding, dict) and all(
        isinstance(binding.get(key), str) and binding[key].strip()
        for key in ("seller_id", "selector_type", "selector_value")
    ) and binding.get("selector_type") in {"oauth", "id"}
    if binding is not None and not has_binding:
        raise RuntimeError("库存任务原身份基线不完整，原任务未恢复")
    seller = _plan_seller_baseline(run)
    if require_baseline and not has_binding and seller is None:
        raise RuntimeError("旧库存任务缺少可信原卖家身份基线，原任务未恢复")
    if has_binding:
        if (binding.get("seller_id") != store.expected_seller_id
                or binding.get("selector_type") != store.selector_type
                or binding.get("selector_value") != store.selector_value):
            raise RuntimeError("暂停后卖家身份或紫鸟环境已变化，原库存任务未恢复")
    if seller is not None and seller != store.expected_seller_id:
        raise RuntimeError("卖家身份与库存任务原计划不一致，原任务未恢复")
    # Legacy plans never saved selector_type/value. The current legal profile
    # binding is usable, but is not evidence of the original profile. In
    # particular StoreHistory is mutable via stores UPDATE triggers.
    return store


class InventoryRunCancelled(WorkflowError):
    code = "inventory_gpsr_cancelled"


class InventorySubmissionBlocked(WorkflowError):
    code = "inventory_gpsr_submission_blocked"


class InventoryHistoryChanged(InventorySubmissionBlocked):
    """Item-local history conflict, not a seller/marketplace binding failure."""


@dataclass(frozen=True, slots=True)
class ItemRecord:
    id: str
    run_id: str | None
    store_id: int
    marketplace_code: str
    item: InventoryItem
    state: str
    reason: str
    eligible: bool
    intent_at: datetime | None = None
    initial_selected: bool | None = None
    item_id: str = ""


class InventoryGpsrStore:
    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory

    async def validate_binding(self, run: Any) -> None:
        with self.session_factory() as session:
            stored = session.get(Run, run.id)
            if stored is None:
                raise LookupError("库存任务不存在")
            current = validate_inventory_run_binding(session, stored)
            if (current.expected_seller_id != run.store.expected_seller_id
                    or current.selector_type != run.store.selector_type
                    or current.selector_value != run.store.selector_value):
                raise RuntimeError("执行期间卖家身份或紫鸟环境已变化")

    async def pause_run(self, run_id: str, marketplace_code: str, *, http_status: int | None,
                        binding: dict[str, str], reason_code: str = "inventory_page_unavailable") -> None:
        """Persist the entire pause boundary before unwinding the UI session."""
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            run = _require_running(session, run_id)
            if run.workflow != WORKFLOW_KEY:
                raise ValueError("库存暂停仅适用于库存安全证明流程")
            rows = session.scalars(select(InventoryGpsrRequirement).where(
                InventoryGpsrRequirement.run_id == run_id, InventoryGpsrRequirement.marketplace_code == marketplace_code,
            )).all()
            remaining = sum(row.state == PENDING or (run.mode == "auto" and row.state == ELIGIBLE) for row in rows)
            uncertain = sum(row.state in {SUBMITTING, UNCERTAIN} for row in rows)
            cause = PageRecoveryRequired("", http_status=http_status, reason_code=reason_code)
            message = (
                f"{cause.public_reason}，已暂停并保留进度；"
                f"剩余 {remaining} 项要求，提交待核对 {uncertain} 项。页面恢复后手动继续，不重复提交已确认商品"
            )
            site = session.scalar(select(SiteRun).where(
                SiteRun.run_id == run_id, SiteRun.marketplace_code == marketplace_code,
            ))
            if site is None:
                raise ValueError("库存暂停缺少站点记录")
            previous = run.status
            run.result_summary = {**(run.result_summary or {}), "inventory_gpsr_binding": dict(binding)}
            run.status, run.error, run.finished_at = "PAUSED", message, None
            site.status, site.error, site.finished_at = "PAUSED", message, None
            session.add(RunEvent(
                run_id=run_id, site_run_id=site.id, event_type="inventory_gpsr_paused",
                from_status=previous, to_status="PAUSED", message=message,
                details={"reason_code": reason_code, "http_status": http_status,
                         "remaining": remaining, "uncertain": uncertain},
            ))
            session.commit()

    async def snapshot_exists(self, run_id: str, marketplace_code: str) -> bool:
        with self.session_factory() as session:
            return _scan_exists(session, run_id, marketplace_code)

    async def save_snapshot(
        self, *, run_id: str, store_id: int, marketplace_code: str,
        items: Sequence[InventoryItem],
    ) -> None:
        # Validate before opening the transaction: duplicate SKU is a scan error,
        # not permission to silently keep whichever copy happened to load first.
        if len({item.sku for item in items}) != len(items):
            raise ValueError("库存扫描出现重复 SKU，未保存候选清单")
        if any(not item.sku or not item.asin for item in items):
            raise ValueError("库存候选缺少 SKU 或 ASIN")
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if _scan_exists(session, run_id, marketplace_code):
                return
            run = _require_running(session, run_id)
            if run.store_id != store_id or run.workflow != "amazon_inventory_gpsr_attestation":
                raise ValueError("库存快照与任务店铺或流程不匹配")
            current = validate_inventory_run_binding(session, run)
            if 'inventory_gpsr_binding' not in (run.result_summary or {}):
                run.result_summary = {**(run.result_summary or {}), 'inventory_gpsr_binding': {
                    'seller_id': current.expected_seller_id, 'selector_type': current.selector_type,
                    'selector_value': current.selector_value}}
            site_id = session.scalar(select(SiteRun.id).where(
                SiteRun.run_id == run_id, SiteRun.marketplace_code == marketplace_code,
            ))
            if site_id is None:
                raise ValueError("必须先创建库存流程站点结果")
            for item in items:
                candidate = InventoryGpsrItem(
                    run_id=run_id, store_id=store_id, marketplace_code=marketplace_code,
                    sku=item.sku, asin=item.asin, title=item.title[:500],
                    page_number=item.page_number, position=item.position,
                    state=PENDING, reason="", eligible=False,
                )
                session.add(candidate)
                session.flush()
                _ensure_requirements(session, candidate, run)
            # This marker and candidates commit together, including an empty
            # scan. A restart must not expand the original run's candidate set.
            session.add(RunEvent(
                run_id=run_id, site_run_id=site_id, event_type=SCAN_EVENT,
                message=f"全部分页扫描完成，固定本轮 {len(items)} 件商品清单",
                details={"marketplace_code": marketplace_code, "scanned": len(items)},
            ))
            session.commit()

    async def list_items(self, run_id: str, marketplace_code: str | None = None) -> tuple[ItemRecord, ...]:
        with self.session_factory() as session:
            # Also supports pre-migration fixture/import rows; never invent a
            # responsible-person result from a legacy safety-attestation result.
            run = session.get(Run, run_id)
            candidates = session.scalars(select(InventoryGpsrItem).where(InventoryGpsrItem.run_id == run_id)).all()
            for candidate in candidates:
                _ensure_requirements(session, candidate, run)
            session.commit()
            query = select(InventoryGpsrRequirement).join(InventoryGpsrItem, InventoryGpsrRequirement.item_id == InventoryGpsrItem.id).where(InventoryGpsrRequirement.run_id == run_id)
            if marketplace_code:
                query = query.where(InventoryGpsrRequirement.marketplace_code == marketplace_code)
            rows = session.scalars(query.order_by(
                InventoryGpsrItem.page_number.desc(), InventoryGpsrItem.position.desc(),
                InventoryGpsrRequirement.requirement_type.desc(),
            )).all()
            return tuple(_record(row) for row in rows)

    async def get(self, item_id: str) -> ItemRecord:
        with self.session_factory() as session:
            row = _get_requirement(session, item_id)
            if row is None:
                raise LookupError("库存商品记录不存在")
            return _record(row)

    async def history_revisions(self, record: ItemRecord) -> tuple[IntentRevision, ...]:
        with self.session_factory() as session:
            return tuple(_intent_revision(row) for row in session.scalars(
                _history_query(record).limit(None)
            ).all())

    async def historical_intent(self, record: ItemRecord) -> ItemRecord | None:
        with self.session_factory() as session:
            row = session.scalar(_history_query(record))
            return _record(row) if row is not None else None

    async def unsettled_for_store(
        self, store_id: int, marketplace_code: str, *, exclude_run_id: str,
    ) -> tuple[ItemRecord, ...]:
        """A new candidate list cannot hide an older unresolved declaration."""
        with self.session_factory() as session:
            rows = session.scalars(select(InventoryGpsrRequirement).where(
                InventoryGpsrRequirement.store_id == store_id,
                InventoryGpsrRequirement.marketplace_code == marketplace_code,
                InventoryGpsrRequirement.state.in_((SUBMITTING, UNCERTAIN)),
                or_(InventoryGpsrRequirement.run_id != exclude_run_id, InventoryGpsrRequirement.run_id.is_(None)),
            ).order_by(InventoryGpsrRequirement.intent_at, InventoryGpsrRequirement.id)).all()
            return tuple(_record(row) for row in rows)

    async def mark(self, item_id: str, state: str, *, reason: str = "", eligible: bool | None = None,
                   initial_selected: bool | None = None) -> None:
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = _get_requirement(session, item_id)
            if row is None:
                raise LookupError("库存商品记录不存在")
            if state == SUBMITTING:
                raise ValueError("提交意图只能通过 arm() 创建")
            if row.state == CONFIRMED:
                if state != CONFIRMED:
                    raise InventorySubmissionBlocked("已确认的提交记录不允许退回执行步骤")
                return
            if row.state in {SUBMITTING, UNCERTAIN} and state not in {CONFIRMED, UNCERTAIN}:
                raise InventorySubmissionBlocked("已有提交意图，只允许回读")
            if state in {CONFIRMED, UNCERTAIN} and row.intent_at is None:
                raise InventorySubmissionBlocked("缺少持久提交意图")
            row.state = state
            row.reason = reason[:120]
            if eligible is not None:
                row.eligible = eligible
            if initial_selected is not None:
                row.initial_selected = initial_selected
            if state == CONFIRMED:
                row.confirmed_at = utc_now()
            _mirror_legacy(row)
            session.commit()

    async def arm(self, item_id: str, *, rechecked_history: tuple[IntentRevision, ...] | None = None) -> None:
        """One save per attempt; a fresh unprocessed form can justify a new run."""
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = _get_requirement(session, item_id)
            if row is None or row.run_id is None:
                raise InventorySubmissionBlocked("商品记录已不属于可执行任务")
            run = _require_running(session, row.run_id)
            if run.mode != "auto" or run.workflow != "amazon_inventory_gpsr_attestation":
                raise InventorySubmissionBlocked("当前任务模式不允许保存安全声明")
            if run.store_id != row.store_id:
                raise InventorySubmissionBlocked("商品提交记录与任务店铺不匹配")
            config = AmazonInventoryGpsrConfig.model_validate(run.workflow_config or {})
            if row.requirement_type not in config.processing_types:
                raise InventorySubmissionBlocked("要求类型不在本次任务配置中")
            validate_inventory_run_binding(session, run)
            if row.state != ELIGIBLE or not row.eligible:
                raise InventorySubmissionBlocked("商品不处于可提交状态")
            history = tuple(_intent_revision(old) for old in session.scalars(
                _history_query(_record(row)).limit(None)
            ).all())
            if history or rechecked_history:
                if history != rechecked_history:
                    raise InventoryHistoryChanged("页面检查后提交记录已变化，未再次送出")
                if row.initial_selected is not False or any(old.state == CONFIRMED for old in history):
                    raise InventoryHistoryChanged("当前页面未证明历史未决要求尚未处理")
                for old in history:
                    previous = session.get(Run, old.run_id) if old.run_id else None
                    if previous and previous.status in {
                        "QUEUED", "RUNNING", "RECONCILING", "WAITING_AUTH", "WAITING_APPROVAL", "PAUSED",
                    }:
                        raise InventoryHistoryChanged("同项历史任务仍可执行，未开启新的提交")
                site = session.scalar(select(SiteRun).where(
                    SiteRun.run_id == run.id, SiteRun.marketplace_code == row.marketplace_code))
                session.add(RunEvent(
                    run_id=run.id, site_run_id=site.id if site else None,
                    event_type="inventory_gpsr_current_unprocessed_retry",
                    message="当前待批准商品明确未处理，建立新尝试；历史记录及原时间保留",
                    details={"sku": row.sku, "asin": row.asin, "requirement_type": row.requirement_type,
                             "requirement_id": row.id, "initial_selected": False,
                             "basis": "current_approval_required_form_unprocessed",
                             "previous_intents": [{"id": old.id, "state": old.state} for old in history]},
                ))
            row.state = SUBMITTING
            row.reason = "submission_intent_committed"
            row.intent_at = utc_now()
            _mirror_legacy(row)
            session.commit()

    async def record_dispatched(self, item_id: str) -> None:
        with self.session_factory() as session:
            row = _get_requirement(session, item_id)
            if row is None or row.state != SUBMITTING:
                raise InventorySubmissionBlocked("提交状态不匹配")
            row.submitted_at = utc_now()
            _mirror_legacy(row)
            session.commit()

    async def summary(self, run_id: str, marketplace_code: str | None = None) -> dict[str, int]:
        rows = await self.list_items(run_id, marketplace_code)
        counts = {
            "scanned": len({row.item_id for row in rows}),
            "pending": sum(row.state == PENDING for row in rows),
            "eligible": sum(row.eligible for row in rows),
            "confirmed": sum(row.state == CONFIRMED for row in rows),
            "already_submitted": sum(row.state == ALREADY_SUBMITTED for row in rows),
            "no_violation_entry": sum(row.state == NO_VIOLATION_ENTRY for row in rows),
            "no_attestation": sum(row.state == NO_ATTESTATION for row in rows),
            "missing": sum(row.state == MISSING for row in rows),
            "failed": sum(row.state == FAILED for row in rows),
            "blocked": sum(row.state == BLOCKED for row in rows),
            "uncertain": sum(row.state in {SUBMITTING, UNCERTAIN} for row in rows),
        }
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            detailed = run is not None and "processing_types" in (run.workflow_config or {})
        if detailed:
            counts.update(requirements=len(rows),
                safety_confirmed=sum(r.state == CONFIRMED and r.item.requirement_type == SAFETY_ATTESTATION for r in rows),
                manufacturer_confirmed=sum(r.state == CONFIRMED and r.item.requirement_type == MANUFACTURER for r in rows),
                responsible_confirmed=sum(r.state == CONFIRMED and r.item.requirement_type == RESPONSIBLE_PERSON for r in rows),
                already_linked=sum(r.state == ALREADY_LINKED for r in rows),
                multiple_records=sum(r.state == MULTIPLE_RECORDS for r in rows),
                no_records=sum(r.state == NO_RECORDS for r in rows),
                invalid_record=sum(r.state == INVALID_RECORD for r in rows),
                not_applicable=sum(r.state == NOT_APPLICABLE for r in rows))
        return counts


def _scan_exists(session: Any, run_id: str, code: str) -> bool:
    return session.scalar(select(RunEvent.id).join(SiteRun, RunEvent.site_run_id == SiteRun.id).where(
        RunEvent.run_id == run_id, RunEvent.event_type == SCAN_EVENT,
        SiteRun.marketplace_code == code,
    ).limit(1)) is not None


def _require_running(session: Any, run_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None or run.status not in {"RUNNING", "RECONCILING"}:
        raise InventoryRunCancelled("任务已停止，库存流程不再操作页面")
    return run


def _history_query(record: ItemRecord):
    return select(InventoryGpsrRequirement).where(
        InventoryGpsrRequirement.id != record.id,
        InventoryGpsrRequirement.store_id == record.store_id,
        InventoryGpsrRequirement.marketplace_code == record.marketplace_code,
        InventoryGpsrRequirement.sku == record.item.sku,
        InventoryGpsrRequirement.asin == record.item.asin,
        InventoryGpsrRequirement.requirement_type == record.item.requirement_type,
        InventoryGpsrRequirement.state.in_(INTENT_STATES),
    ).order_by(InventoryGpsrRequirement.created_at, InventoryGpsrRequirement.id).limit(1)


@dataclass(frozen=True, slots=True)
class IntentRevision:
    id: str
    run_id: str | None
    state: str
    intent_at: datetime | None
    submitted_at: datetime | None
    confirmed_at: datetime | None


def _intent_revision(row: InventoryGpsrRequirement) -> IntentRevision:
    return IntentRevision(row.id, row.run_id, row.state, row.intent_at, row.submitted_at, row.confirmed_at)


def _record(row: InventoryGpsrRequirement) -> ItemRecord:
    return ItemRecord(
        id=row.id, run_id=row.run_id, store_id=row.store_id,
        marketplace_code=row.marketplace_code,
        item=InventoryItem(sku=row.sku, asin=row.asin, page_number=row.item.page_number,
                           position=row.item.position, title=row.item.title, requirement_type=row.requirement_type),
        state=row.state, reason=row.reason, eligible=row.eligible, intent_at=row.intent_at,
        initial_selected=row.initial_selected, item_id=row.item_id,
    )


def _ensure_requirements(session, item, run):
    types = AmazonInventoryGpsrConfig.model_validate((run.workflow_config or {}) if run else {}).processing_types
    for kind in types:
        if session.scalar(select(InventoryGpsrRequirement.id).where(
            InventoryGpsrRequirement.item_id == item.id, InventoryGpsrRequirement.requirement_type == kind,
        )) is not None:
            continue
        legacy = kind == SAFETY_ATTESTATION
        row = InventoryGpsrRequirement(
            item_id=item.id, item=item, run_id=item.run_id, store_id=item.store_id,
            marketplace_code=item.marketplace_code, sku=item.sku, asin=item.asin,
            requirement_type=kind, state=item.state if legacy else PENDING,
            reason=item.reason if legacy else "", eligible=item.eligible if legacy else False,
            intent_at=item.intent_at if legacy else None, submitted_at=item.submitted_at if legacy else None,
            confirmed_at=item.confirmed_at if legacy else None,
        )
        if legacy:
            row.id = item.id
        session.add(row)
    session.flush()


def _get_requirement(session, record_id):
    row = session.get(InventoryGpsrRequirement, record_id)
    if row is None:
        item = session.get(InventoryGpsrItem, record_id)
        if item is not None:
            _ensure_requirements(session, item, session.get(Run, item.run_id) if item.run_id else None)
            row = session.get(InventoryGpsrRequirement, record_id)
    return row


def _mirror_legacy(row):
    # Preserve the original safety-only history and API projection. It is never
    # a submission barrier for the independent responsible-person requirement.
    if row.requirement_type == SAFETY_ATTESTATION:
        for name in ("state", "reason", "eligible", "intent_at", "submitted_at", "confirmed_at"):
            setattr(row.item, name, getattr(row, name))
