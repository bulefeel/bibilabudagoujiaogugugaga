"""Snapshot, inspect, save once, and reconcile inventory GPSR declarations."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Sequence

from ..contracts import WorkflowRepository
from ..errors import DomContractError, HumanAuthRequired, PageRecoveryRequired, PreflightRejected, WorkflowError
from ..types import (
    EvidenceRef, MarketplaceRef, MarketplaceSnapshot, OperationRecord, PlanLine, RunMode,
    RunStatus, SiteStatus, WorkflowPlan, WorkflowReport, WorkflowRun,
)
from .page import AmazonInventoryGpsrPage, InventoryContractError, InventoryMarketplaceError, ItemLoadError
from .marketplace_menu import ERRORS as MENU_ERRORS
from .progress import ReadProgress
from .config import InventoryGpsrPolicy
from .config import SUBMISSION_RECEIPT, RESPONSIBLE_PERSON_RECEIPT, INVENTORY_MARKETPLACES, RESPONSIBLE_PERSON, SAFETY_ATTESTATION, PARTY_TYPES, REQUIREMENT_LABELS
from .store import (
    ALREADY_SUBMITTED, BLOCKED, CONFIRMED, ELIGIBLE, FAILED, MISSING,
    NO_ATTESTATION, NO_VIOLATION_ENTRY, PENDING, SUBMITTING, UNCERTAIN,
    InventoryGpsrStore, InventoryRunCancelled, InventorySubmissionBlocked, InventoryHistoryChanged, ItemRecord,
    ALREADY_LINKED, MULTIPLE_RECORDS, NO_RECORDS, INVALID_RECORD, NOT_APPLICABLE,
)

logger = logging.getLogger(__name__)


class InventoryResultUncertain(WorkflowError):
    code = "inventory_gpsr_result_uncertain"


class AmazonInventoryGpsrWorkflow:
    name = "amazon_inventory_gpsr_attestation"

    def __init__(
        self, *, repository: WorkflowRepository, item_store: InventoryGpsrStore,
        page_adapter: AmazonInventoryGpsrPage | None = None,
    ) -> None:
        self.repository = repository
        self.item_store = item_store
        self.page_adapter = page_adapter or AmazonInventoryGpsrPage()
        if hasattr(self.page_adapter, "cancel_check"):
            self.page_adapter.cancel_check = self._check_running
        if hasattr(self.page_adapter, "on_http_event"):
            self.page_adapter.on_http_event = self._http_event
        if hasattr(self.page_adapter, "on_item_event"):
            self.page_adapter.on_item_event = self._item_event
        if hasattr(self.page_adapter, "on_marketplace_event"):
            self.page_adapter.on_marketplace_event = self._marketplace_event
        if hasattr(self.page_adapter, "on_scan_event"):
            self.page_adapter.on_scan_event = self._scan_event

    async def _scan_event(self, run, marketplace, action, details):
        message = ("库存数量变化，等待后在原标签重建本轮候选清单" if action == "restart"
                   else "库存数量持续变化，已暂停并保留其他站点清单")
        await self.repository.append_event(
            run.id, "inventory_gpsr_scan_" + action, message, marketplace_code=marketplace.code,
            details={k: details[k] for k in ("expected_total", "actual_total", "page", "size", "restart") if k in details},
        )

    async def _marketplace_event(self, run, marketplace, action, details):
        messages = {'opened': '已展开商城菜单，等待当前账号国家列表',
                    'reopened': '商城菜单意外收起，等待后仅重新展开一次',
                    'clicked': '已点击目标商城，等待切换结果，不重复点击',
                    'confirmed': '目标商城与卖家身份已核对',
                    'failed': '商城切换菜单未完成，已记录实际失败状态'}
        await self.repository.append_event(
            run.id, 'inventory_marketplace_' + action, messages[action], marketplace_code=marketplace.code,
            details={k: details[k] for k in ('state', 'target_count', 'item_count', 'reopens') if k in details},
        )

    async def _item_event(self, run: WorkflowRun, item: Any, action: str,
                          details: dict[str, Any]) -> None:
        messages = {
            "sidebar_closed": "已收起商品侧栏，准备重新打开目标商品",
            "entry_clicked": "已完成当前商品入口点击，等待新的侧栏内容",
            "panel_loaded": "当前商品的新侧栏内容已加载并核对",
            "read_failed": "当前商品读取未完成，已记录失败阶段",
            "retry": "等待后重试当前商品，先检查侧栏及入口状态",
            "exhausted": "当前商品重试已用完，记录实际失败原因并继续后续商品",
            "person_selection_failed": "关联信息勾选或按钮启用未完成，尚未点击送出",
            "person_selected": "关联信息已勾选且送出按钮已启用，准备记录提交意图",
            "person_dispatched": "关联信息送出点击已派发，等待当前商品回执",
            "history_all_inventory": "待批准列表未找到历史商品，切换全部商品只读核对；不是新增候选扫描",
            "manufacturer_skipped": "当前入口是制造商信息，属于不同于负责人的要求；本任务未启用制造商关联，已跳过",
        }
        fields = {"stage", "retry", "http_status", "reason", "panel_ready",
                  "panel_loading", "panel_bound", "fresh_open", "no_action",
                  "first_layer", "known_other", "person_form", "manufacturer_form",
                  "typed_receipt", "close_count", "back_count"}
        stages = {"entry": "商品入口", "close_sidebar": "关闭侧栏", "return_sidebar": "侧栏返回",
                  "first_layer": "首层加载", "violation_entry": "违规入口",
                  "violation_content": "违规详情加载", "attestation_entry": "安全证明入口",
                  "attestation_form": "声明表单加载", "person_selection": "关联信息勾选",
                  "person_ready": "关联信息送出前检查", "person_submitted": "关联信息送出"}
        message = messages[action]
        if action in {"read_failed", "exhausted"}:
            message += "（" + stages.get(details.get("stage"), "商品检查") + "）"
        await self.repository.append_event(
            run.id, "inventory_gpsr_ui_" + action, message, marketplace_code=self._event_marketplace(run),
            details={"sku": item.sku, "asin": item.asin, "requirement_type": item.requirement_type,
                     **{k: v for k, v in details.items() if k in fields}},
        )

    async def _http_event(self, run: WorkflowRun, item: Any, action: str, retry: int) -> None:
        messages = {
            "dom_verified": "观察到 HTTP 403，但当前页面内容已稳定且身份核对通过，继续网页流程",
            "retry": "当前商品侧栏未恢复，等待后通过网页重新打开侧栏",
            "exhausted": "当前商品网页重试已用完，记录读取失败并继续后续商品",
            "csrf_detected": "网页返回跨站请求校验失败，且实际请求缺少校验头；已识别页面请求环境异常",
            "csrf_reload": "保留清单与进度，等待后仅刷新当前紫鸟标签一次；不重复保存",
            "csrf_restored": "原标签已恢复，域名、卖家和待批准筛选核对通过，重新定位当前商品",
            "csrf_recovery_failed": "页面恢复未完成，保留当前及剩余商品进度",
            "csrf_recovery_exhausted": "本轮页面恢复预算已用完，暂停并保留剩余商品，避免连续无效点击",
        }
        await self.repository.append_event(
            run.id, "inventory_gpsr_http_" + action, messages[action],
            marketplace_code=self._event_marketplace(run),
            details={"http_status": 403, "retry": retry,
                     **({"sku": item.sku, "asin": item.asin} if item else {})},
        )

    def begin_attempt(self, run_id: str) -> None:
        begin = getattr(self.page_adapter, "begin_attempt", None)
        if callable(begin):
            begin(run_id)

    def _event_marketplace(self, run):
        market = getattr(self.page_adapter, '_active_marketplace', None)
        return market.code if market is not None else run.marketplaces[0].code

    async def preflight(self, run: WorkflowRun, page: Any) -> None:
        if run.mode not in {RunMode.DRY_RUN, RunMode.AUTO}:
            raise PreflightRejected("库存安全证明流程仅支持空跑和自动模式")
        if not run.marketplaces or any(
            INVENTORY_MARKETPLACES.get(market.code) != market.domain
            for market in run.marketplaces
        ):
            raise PreflightRejected("库存合规流程仅允许已登记的欧盟站点准确域名")
        if not run.store.enabled or not run.store.identity_confirmed or not run.store.expected_seller_id:
            raise PreflightRejected("店铺尚未完成卖家身份绑定")
        await self._check_running(run)
        await self.item_store.validate_binding(run)

    async def plan(self, run: WorkflowRun, page: Any) -> WorkflowPlan:
        lines: list[PlanLine] = []
        for marketplace in run.marketplaces:
            await self.repository.set_site_status(run.id, marketplace.code, SiteStatus.PREFLIGHT)
            phase = "plan"
            try:
                await self._check_running(run)
                page = await self.page_adapter.open_inventory(page, run, marketplace)
                if not await self.item_store.snapshot_exists(run.id, marketplace.code):
                    await self._check_running(run)
                    items = await self.page_adapter.scan_candidates(page, run, marketplace)
                    await self._check_running(run)
                    await self.item_store.save_snapshot(
                        run_id=run.id, store_id=int(run.store.id),
                        marketplace_code=marketplace.code, items=items,
                    )
                page = self._resolved(page)
                # Only resume this attempt's consumed intents, never scan other runs.
                await self._recover_intents(run, page, marketplace)
                rows = await self.item_store.list_items(run.id, marketplace.code)
                if run.mode is RunMode.DRY_RUN:
                    await self._process_snapshot(run, page, marketplace, submit=False)
                    # Engine handles empty plans using site outcomes. Returning a
                    # line in DRY_RUN would make partial reads unconditional success.
                elif any(row.state in {PENDING, ELIGIBLE} for row in rows):
                    await self.repository.set_site_status(run.id, marketplace.code, SiteStatus.PLANNED)
                    lines.append(PlanLine(marketplace, _snapshot(run, marketplace)))
                else:
                    await self._finish_site(run, marketplace)
            except PageRecoveryRequired as exc:
                await self._pause_site(run, marketplace, exc)
                raise
            except (HumanAuthRequired, InventoryRunCancelled):
                raise
            except Exception as exc:
                await self._record_site_failure(run, marketplace, exc, phase=phase)
                # A non-financial run has no OperationGuard. Let the engine's
                # site-outcome path retain PARTIAL when earlier items succeeded.
                if not (await self.item_store.summary(run.id, marketplace.code))["confirmed"]:
                    raise
                # Preserve partial outcomes, but never submit on a later site
                # after an identity/contract error or an unresolved intent.
                return WorkflowPlan(self.name, run.id, run.store.id, ())
            finally:
                await self._summary_event(run, marketplace)
        return WorkflowPlan(self.name, run.id, run.store.id, tuple(lines))

    async def execute(self, run: WorkflowRun, page: Any, plan: WorkflowPlan) -> Sequence[OperationRecord]:
        if run.mode is not RunMode.AUTO:
            return ()
        for marketplace in run.marketplaces:
            try:
                if not await self.item_store.snapshot_exists(run.id, marketplace.code):
                    raise DomContractError("缺少完整候选快照，未执行库存提交")
                page = await self.page_adapter.open_inventory(self._resolved(page), run, marketplace)
                await self._recover_intents(run, self._resolved(page), marketplace)
                await self._process_snapshot(run, self._resolved(page), marketplace, submit=True)
            except PageRecoveryRequired as exc:
                await self._pause_site(run, marketplace, exc)
                raise
            except (HumanAuthRequired, InventoryRunCancelled):
                raise
            except Exception as exc:
                await self._record_site_failure(run, marketplace, exc, phase="execute")
                if not (await self.item_store.summary(run.id, marketplace.code))["confirmed"]:
                    raise
                return ()
            finally:
                await self._summary_event(run, marketplace)
        return ()

    async def _process_snapshot(
        self, run: WorkflowRun, page: Any, marketplace: MarketplaceRef, *, submit: bool,
    ) -> None:
        progress = ReadProgress(getattr(self.page_adapter, 'timing', InventoryGpsrPolicy()))
        residual: list[ItemRecord] = []
        rows = await self.item_store.list_items(run.id, marketplace.code)
        # Store returns descending page and position. Submissions may remove rows;
        # processing from the tail keeps untouched earlier rows at stable indices.
        for record in rows:
            if record.state not in {PENDING, ELIGIBLE}:
                continue
            if not submit and record.state == ELIGIBLE:
                # A previous dry-run leg already reached the form. Auth resume
                # only examines the remaining snapshot, avoiding repeated visits.
                continue
            await self._check_running(run)
            await self._check_read_progress(run, marketplace, progress)
            try:
                if not await self.page_adapter.locate_item(self._resolved(page), record.item):
                    residual.append(record)
                    continue
                await self._inspect_and_act(run, self._resolved(page), marketplace, record, submit=submit)
                progress.completed()
            except ItemLoadError as exc:
                reason = exc.reason_code
                await self._mark_item(run, marketplace, record, FAILED, reason)
                await self._capture_evidence(run, self._resolved(page), marketplace, record, "load-failure")
                progress.failed()
                await self._check_read_progress(run, marketplace, progress)
            except (HumanAuthRequired, InventoryRunCancelled, InventoryResultUncertain, PageRecoveryRequired):
                raise
            except Exception:
                current = await self.item_store.get(record.id)
                if current.state in {PENDING, ELIGIBLE}:
                    await self._mark_item(run, marketplace, record, FAILED, "page_contract_or_identity_error")
                raise

        # A disappearing row is not proof of a saved declaration. Only unresolved
        # original candidates get an exact SKU search, once, after the main pass.
        for record in residual:
            await self._check_running(run)
            await self._check_read_progress(run, marketplace, progress)
            try:
                if not await self.page_adapter.locate_item(self._resolved(page), record.item, search=True):
                    await self._mark_item(run, marketplace, record, MISSING, "not_in_inventory_snapshot_anymore")
                    progress.completed()
                    continue
                await self._inspect_and_act(run, self._resolved(page), marketplace, record, submit=submit)
                progress.completed()
            except ItemLoadError as exc:
                reason = exc.reason_code
                await self._mark_item(run, marketplace, record, FAILED, reason)
                await self._capture_evidence(run, self._resolved(page), marketplace, record, "load-failure")
                progress.failed()
                await self._check_read_progress(run, marketplace, progress)
            except (HumanAuthRequired, InventoryRunCancelled, InventoryResultUncertain, PageRecoveryRequired):
                raise
            except Exception:
                current = await self.item_store.get(record.id)
                if current.state in {PENDING, ELIGIBLE}:
                    await self._mark_item(run, marketplace, record, FAILED, "page_contract_or_identity_error")
                raise
        await self._finish_site(run, marketplace)

    async def _check_read_progress(self, run, marketplace, progress):
        try:
            progress.check()
        except PageRecoveryRequired:
            await self.repository.append_event(
                run.id, "inventory_gpsr_no_progress",
                "连续读取失败或长时间无有效进展，停止继续点击并保留剩余清单",
                marketplace_code=marketplace.code, details=progress.diagnostic(),
            )
            raise

    async def _inspect_and_act(
        self, run: WorkflowRun, page: Any, marketplace: MarketplaceRef,
        record: ItemRecord, *, submit: bool,
    ) -> None:
        await self._check_running(run)
        # Snapshot before inspecting; arm() detects any intervening submission.
        history = await self.item_store.history_revisions(record)
        inspection = await self.page_adapter.inspect_item(page, run, marketplace, record.item)
        page = inspection.page or self._resolved(page)
        disposition = {
            "already_submitted": ALREADY_SUBMITTED,
            "no_violation_entry": NO_VIOLATION_ENTRY,
            "no_attestation": NO_ATTESTATION,
            "eligible": ELIGIBLE,
            "already_linked": ALREADY_LINKED,
            "multiple_records": MULTIPLE_RECORDS,
            "no_records": NO_RECORDS,
            "invalid_record": INVALID_RECORD,
            "not_applicable": NOT_APPLICABLE,
        }.get(inspection.status)
        if disposition is None:
            raise DomContractError("库存侧栏返回未知状态，未提交")
        if history and disposition == ELIGIBLE:
            if any(h.state == CONFIRMED for h in history):
                retained = getattr(self.page_adapter, "confirmed_attestation_retained", None)
                if (record.item.requirement_type == SAFETY_ATTESTATION and callable(retained)
                        and await retained(page, run, marketplace, record.item)):
                    await self._mark_item(run, marketplace, record, ALREADY_SUBMITTED,
                                          "historically_confirmed_declaration_retained", initial_selected=True)
                    return
                await self._mark_item(run, marketplace, record, BLOCKED, "historical_confirmed_conflicts_with_page")
                return
            if getattr(inspection, "initial_selected", None) is not False:
                await self._mark_item(run, marketplace, record, BLOCKED, "current_unprocessed_not_proven")
                return
        await self._mark_item(
            run, marketplace, record, disposition,
            inspection.reason or inspection.status, eligible=disposition == ELIGIBLE,
            initial_selected=getattr(inspection, 'initial_selected', None),
        )
        if disposition == ELIGIBLE:
            await self._capture_evidence(run, page, marketplace, record, "form")
        if disposition != ELIGIBLE or not submit:
            return
        await self._check_running(run)
        prepare = getattr(self.page_adapter, "prepare_submission", None)
        if callable(prepare):
            await prepare(page, run, marketplace, record.item)
            page = self._resolved(page)
        await self._check_running(run)
        # arm() rechecks RUNNING/AUTO and historical intents in the write
        # transaction, closing the cancellation race between read and commit.
        try:
            await self.item_store.arm(record.id, rechecked_history=history)
        except InventoryHistoryChanged:
            await self._mark_item(run, marketplace, record, BLOCKED, "submission_history_changed")
            return
        try:
            # Cancellation may arrive immediately after the intent transaction.
            # This attempt stays consumed. A new run must inspect a new candidate.
            await self._check_running(run)
            await self.page_adapter.submit_once(page, run, marketplace, record.item)
            await self.item_store.record_dispatched(record.id)
            # A dispatched write is never repeated. Even after cancellation,
            # finish only its bounded, read-only receipt drain before releasing.
            confirmed = await self.page_adapter.confirm_submission(self._resolved(page), run, marketplace, record.item)
        except PageRecoveryRequired:
            await self._mark_item(run, marketplace, record, UNCERTAIN, "page_unavailable_after_intent")
            await self._check_running(run)
            raise
        except HumanAuthRequired:
            # Engine's auth resume calls plan(), which reconciles the intent.
            raise
        except InventoryRunCancelled:
            raise
        except Exception as exc:
            await self._mark_item(run, marketplace, record, UNCERTAIN, "submission_result_unreadable")
            logger.warning("Inventory submission failure: %s", _safe_exception_frames(exc))
            await self._check_running(run)
            raise InventoryResultUncertain("商品提交结果尚未确认，已停止后续提交") from exc
        if not confirmed:
            await self._mark_item(run, marketplace, record, UNCERTAIN, "confirmation_timeout")
            await self._check_running(run)
            raise InventoryResultUncertain("未读到对应商品的审核中回执，已停止后续提交")
        await self._mark_item(run, marketplace, record, CONFIRMED, "submission_confirmed")
        await self._check_running(run)
        await self._capture_evidence(run, self._resolved(page), marketplace, record, "receipt")

    async def _recover_intents(self, run: WorkflowRun, page: Any, marketplace: MarketplaceRef) -> None:
        for record in await self.item_store.list_items(run.id, marketplace.code):
            if record.state not in {SUBMITTING, UNCERTAIN}:
                continue
            await self._check_running(run)
            try:
                confirmed = await self.page_adapter.reconcile_item(self._resolved(page), run, marketplace, record.item)
            except (HumanAuthRequired, InventoryRunCancelled, PageRecoveryRequired):
                raise
            except Exception as exc:
                await self._mark_item(run, marketplace, record, UNCERTAIN, "reconciliation_unreadable")
                reason = await self._reconciliation_failure(run, marketplace, record, exc)
                raise InventoryResultUncertain(f"历史提交只回读，当前仍未确认：{reason}") from None
            if not confirmed:
                await self._mark_item(run, marketplace, record, UNCERTAIN, "reconciliation_not_confirmed")
                raise InventoryResultUncertain("历史提交仍待核对，本轮未进行新的提交")
            await self._mark_item(run, marketplace, record, CONFIRMED, "reconciliation_confirmed")

    async def _reconciliation_failure(
        self, run: WorkflowRun, marketplace: MarketplaceRef,
        record: ItemRecord, exc: Exception, *, historical_state: str | None = None,
    ) -> str:
        """Keep the diagnostic category without serializing remote error text."""
        details, reason = _safe_reconciliation_failure(exc)
        stage, stage_name = _safe_reconciliation_stage(exc)
        source_state = historical_state or record.state
        details.update(stage=stage, historical_state=(
            source_state if source_state in {SUBMITTING, UNCERTAIN, CONFIRMED} else "UNKNOWN"
        ))
        reason = f"{stage_name}：{reason}"
        await self.repository.append_event(
            run.id, "inventory_gpsr_reconciliation_failed",
            f"{record.item.sku} / {record.item.asin}：历史提交回读失败：{reason}；未再次保存",
            marketplace_code=marketplace.code,
            details={"sku": record.item.sku, "asin": record.item.asin, **details},
        )
        # exc_info/format_exception include URLs, source text and exception
        # payloads. Retain only bounded code locations, never locals or values.
        logger.warning("Inventory reconciliation failure: %s", _safe_exception_frames(exc))
        return reason

    async def reconcile(
        self, run: WorkflowRun, page: Any, operations: Sequence[OperationRecord] | None = None,
    ) -> Sequence[OperationRecord]:
        if page is None:
            raise InventoryResultUncertain("库存提交回读需要对应紫鸟页面")
        for marketplace in run.marketplaces:
            try:
                await self._check_running(run)
                page = await self.page_adapter.open_inventory(page, run, marketplace)
                await self._recover_intents(run, page, marketplace)
            finally:
                await self._summary_event(run, marketplace)
        return ()

    async def _mark_item(
        self, run: WorkflowRun, marketplace: MarketplaceRef, record: ItemRecord,
        state: str, reason: str, *, eligible: bool | None = None, initial_selected: bool | None = None,
    ) -> None:
        await self.item_store.mark(record.id, state, reason=reason, eligible=eligible, initial_selected=initial_selected)
        descriptions = {
            ELIGIBLE: "已检查到安全声明表单", CONFIRMED: "安全证明已提交，平台显示审核中",
            ALREADY_SUBMITTED: "平台显示此前已提交，本次跳过",
            NO_VIOLATION_ENTRY: "没有解决商品信息违规问题入口，跳过",
            NO_ATTESTATION: "当前违规类型没有安全证明入口，跳过",
            MISSING: "原清单商品已不在待批准列表，未判为提交成功",
            FAILED: "提交前读取失败", UNCERTAIN: "提交结果不明确，需要核对",
            BLOCKED: "历史提交与当前页面待核对，未再次保存",
            ALREADY_LINKED: "进入页面时已有关联，本次不送出",
            MULTIPLE_RECORDS: "多条负责人记录，跳过并待确认",
            NO_RECORDS: "没有关联记录，跳过",
            INVALID_RECORD: "关联记录失效或不可操作，跳过",
            NOT_APPLICABLE: "当前商品无本项处理要求，跳过",
        }
        if record.item.requirement_type in PARTY_TYPES:
            descriptions[ELIGIBLE] = REQUIREMENT_LABELS[record.item.requirement_type] + "存在未选中的可操作记录，可关联"
            descriptions[CONFIRMED] = REQUIREMENT_LABELS[record.item.requirement_type] + "已提交，平台显示审核中"
        if state == BLOCKED:
            descriptions[BLOCKED] = {
                "current_unprocessed_not_proven": "当前表单尚未明确证明未处理，本项跳过；其他商品继续",
                "submission_history_changed": "提交记录已变化或同项任务仍在执行，本项跳过；其他商品继续",
                "historical_confirmed_conflicts_with_page": "历史已确认与当前未处理状态冲突，本项待核对；其他商品继续",
            }.get(reason, descriptions[BLOCKED])
        if reason == "historically_confirmed_requirement_absent":
            descriptions[NOT_APPLICABLE] = "历史已确认；完整检查后当前已无本项要求，本轮未再次提交"
        if state == FAILED:
            descriptions[FAILED] = {
                "manufacturer_selection_failed": "制造商勾选或按钮启用未完成，尚未送出；继续后续商品",
                "responsible_person_selection_failed": "负责人勾选或按钮启用未完成，尚未送出；继续后续商品",
                "item_click_timeout": "网页点击未完成，已跳过当前商品",
                "item_sidebar_not_reset": "侧栏未能收起或返回，已跳过当前商品",
                "item_panel_load_timeout": "侧栏未加载出当前商品的稳定内容，已跳过",
                "item_search_load_timeout": "SKU 搜索结果未完成更新，未提交该商品，继续剩余清单",
                "item_http_403_retries_exhausted": "HTTP 403 后网页重试仍未取得商品内容，已跳过",
                "page_contract_or_identity_error": "页面结构或商品身份核验异常，已停止后续提交；详见站点中断诊断",
            }.get(reason, descriptions[FAILED])
        await self.repository.append_event(
            run.id, f"inventory_gpsr_{state.lower()}",
            f"{record.item.sku} / {record.item.asin}：{descriptions.get(state, state)}",
            marketplace_code=marketplace.code,
            details={"sku": record.item.sku, "asin": record.item.asin, "state": state, "reason_code": reason,
                     "requirement_type": record.item.requirement_type},
        )

    async def _record_site_failure(
        self, run: WorkflowRun, marketplace: MarketplaceRef, exc: Exception, *, phase: str,
    ) -> None:
        """Preserve why a partial run stopped, without exception text or DOM data."""
        stage, stage_label = _safe_reconciliation_stage(exc, default=(phase,
            {"plan": "候选扫描", "history_reconciliation": "历史提交核对", "execute": "商品执行"}.get(phase, "库存执行")))
        details, reason = _safe_reconciliation_failure(exc)
        if isinstance(exc, InventoryResultUncertain):
            details = {"reason_code": "submission_result_uncertain", "exception_type": "InventoryResultUncertain"}
            reason = "商品提交结果尚未确认，仅允许回读"
        elif isinstance(exc, InventorySubmissionBlocked):
            stage, stage_label = "history_reconciliation", "历史提交核对"
            details = {"reason_code": "historical_submission_blocked", "exception_type": "InventorySubmissionBlocked"}
            reason = "历史提交与当前页面尚未核对一致"
        elif details["reason_code"] == "reconciliation_error":
            details = {**details, "reason_code": "inventory_processing_error"}
            reason = "执行异常，需核查本地诊断"
        codes = [m.code for m in run.marketplaces]
        remaining = codes[codes.index(marketplace.code) + 1:]
        message = f"{marketplace.code} · {stage_label}：{reason}；已停止后续提交"
        if remaining:
            message += f"，后续站点 {'、'.join(remaining)} 未执行"
        await self.repository.set_site_status(run.id, marketplace.code, SiteStatus.FAILED, error=message)
        await self.repository.append_event(
            run.id, "inventory_gpsr_stopped", message, marketplace_code=marketplace.code,
            details={**details, "phase": phase, "stage": stage,
                     "remaining_marketplaces": remaining, "exception_frames": _safe_exception_frames(exc)},
        )

    async def _summary_event(self, run: WorkflowRun, marketplace: MarketplaceRef) -> None:
        counts = await self._counts(run, marketplace.code)
        await self.repository.append_event(
            run.id, "inventory_gpsr_summary", _summary_text(counts),
            marketplace_code=marketplace.code, details=counts,
        )

    async def _capture_evidence(
        self, run: WorkflowRun, page: Any, marketplace: MarketplaceRef,
        record: ItemRecord, label: str,
    ) -> None:
        """Evidence is best effort and never opens or refreshes another page."""
        if run.artifact_dir is None:
            return
        await self._check_running(run)
        timeout = self.page_adapter.timing.evidence_timeout_seconds
        try:
            directory = Path(run.artifact_dir) / run.id
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{marketplace.code}-inventory-{record.id}-{label}.png"
            await asyncio.wait_for(
                page.screenshot(path=str(target), full_page=False, timeout=int(timeout * 1000)),
                timeout=timeout,
            )
            await self.repository.save_evidence(run.id, marketplace.code, EvidenceRef(
                kind="screenshot", file_path=str(target),
                sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                metadata={"marketplace": marketplace.code, "label": f"inventory_{label}"},
            ))
        except Exception:
            fallback_saved = await self._capture_text_evidence(run, marketplace, record, label)
            await self.repository.append_event(
                run.id, "inventory_gpsr_evidence_unavailable",
                "当前页面截图未取得，已保留白名单文字证据" if fallback_saved else
                "当前页面截图及文字证据未取得，商品处理状态仍以页面回执和记录为准",
                marketplace_code=marketplace.code,
                details={"stage": label, "reason_code": "screenshot_unavailable", "text_evidence_saved": fallback_saved},
            )

    async def _capture_text_evidence(
        self, run: WorkflowRun, marketplace: MarketplaceRef, record: ItemRecord, label: str,
    ) -> bool:
        """Persist validated scalar facts, never HTML, cookies or exception text.

        Receipt wording is the normalized contract matched by the adapter, not
        a fresh scrape. No browser operation is attempted after screenshot failure.
        """
        try:
            current = await self.item_store.get(record.id)
            if label not in {"form", "receipt", "load-failure"}:
                return False
            payload = {
                "schema_version": 1,
                "evidence_source": "validated_workflow_state",
                "marketplace": marketplace.code,
                "sku": current.item.sku,
                "asin": current.item.asin,
                "phase": label,
                "status": current.state,
                "receipt": (RESPONSIBLE_PERSON_RECEIPT if current.item.requirement_type in PARTY_TYPES
                            else SUBMISSION_RECEIPT) if label == "receipt" and current.state == CONFIRMED else None,
            }
            directory = Path(run.artifact_dir) / run.id
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{marketplace.code}-inventory-{record.id}-{label}.json"
            content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            target.write_bytes(content)
            await self.repository.save_evidence(run.id, marketplace.code, EvidenceRef(
                kind="json", file_path=str(target), sha256=hashlib.sha256(content).hexdigest(),
                metadata={"marketplace": marketplace.code, "label": f"inventory_{label}", "source": "validated_workflow_state"},
            ))
            return True
        except Exception:
            # Evidence is auxiliary: an unavailable disk or database must not
            # erase a confirmed receipt or cause another save attempt.
            return False

    async def _finish_site(self, run: WorkflowRun, marketplace: MarketplaceRef) -> None:
        await self._check_running(run)
        counts = await self._counts(run, marketplace.code)
        if counts["failed"] or counts["uncertain"] or counts["blocked"] or counts["pending"]:
            status = SiteStatus.FAILED
        elif run.mode is RunMode.DRY_RUN:
            status = SiteStatus.DRY_RUN_COMPLETE
        elif counts["confirmed"]:
            status = SiteStatus.CONFIRMED
        else:
            status = SiteStatus.SKIPPED
        await self.repository.set_site_status(
            run.id, marketplace.code, status,
            error="部分商品尚未检查、读取失败或历史提交待核对" if status is SiteStatus.FAILED else None,
        )

    async def _pause_site(
        self, run: WorkflowRun, marketplace: MarketplaceRef, exc: PageRecoveryRequired,
    ) -> None:
        await self.item_store.pause_run(
            run.id, marketplace.code, http_status=exc.http_status, reason_code=exc.reason_code,
            binding={"seller_id": run.store.expected_seller_id, "selector_type": run.store.selector_type,
                     "selector_value": run.store.selector_value},
        )

    async def _check_running(self, run: WorkflowRun) -> None:
        status = await self.repository.get_run_status(run.id)
        if status not in {RunStatus.RUNNING, RunStatus.RECONCILING}:
            raise InventoryRunCancelled("任务已停止，不再操作库存页面")

    def _resolved(self, page: Any) -> Any:
        resolver = getattr(self.page_adapter, "resolved_page_for", None)
        return resolver(page) if callable(resolver) else page

    async def report(self, run: WorkflowRun, status: RunStatus) -> WorkflowReport:
        try:
            counts = await self._counts(run)
            return WorkflowReport(
                run_id=run.id, status=status,
                title="库存待批准商品合规检查" if run.mode is RunMode.DRY_RUN else "库存待批准商品合规处理结果",
                summary=_summary_text(counts),
                fields={"workflow": self.name, "store_name": run.store.name, "mode": run.mode.value, **counts},
            )
        finally:
            if status in {
                RunStatus.SUCCEEDED, RunStatus.PARTIAL, RunStatus.FAILED,
                RunStatus.CANCELLED, RunStatus.NEEDS_HUMAN_AUTH,
                RunStatus.UNCERTAIN_FINANCIAL, RunStatus.SKIPPED,
                RunStatus.PAUSED,
            }:
                detach = getattr(self.page_adapter, "detach_response_observers", None)
                if callable(detach):
                    detach()

    async def _counts(self, run: WorkflowRun, marketplace_code: str | None = None) -> dict[str, int]:
        return await self.item_store.summary(run.id, marketplace_code)


def _summary_text(counts: dict[str, int]) -> str:
    if 'requirements' in counts:
        return (f"扫描 {counts['scanned']} 件商品，检查范围 {counts['requirements']} 项要求；"
                f"安全证明已提交、审核中 {counts['safety_confirmed']} 项，负责人已提交、审核中 {counts['responsible_confirmed']} 项，制造商已提交、审核中 {counts['manufacturer_confirmed']} 项；"
                f"已有关联 {counts['already_linked']} 项，多记录待确认 {counts['multiple_records']} 项，"
                f"无记录 {counts['no_records']} 项，失效记录 {counts['invalid_record']} 项，"
                f"非本项要求 {counts['not_applicable'] + counts['no_attestation']} 项；"
                f"可处理 {counts['eligible']} 项，尚未检查 {counts['pending']} 项，"
                f"失败 {counts['failed']} 项，历史待核对 {counts['blocked']} 项，结果不明 {counts['uncertain']} 项")
    return (
        f"扫描 {counts['scanned']} 件，尚未检查 {counts['pending']} 件，已核验可处理 {counts['eligible']} 件，"
        f"提交确认 {counts['confirmed']} 件，此前审核中 {counts['already_submitted']} 件；"
        f"无违规入口 {counts['no_violation_entry']} 件，无安全证明 {counts['no_attestation']} 件，"
        f"移出列表 {counts['missing']} 件，读取失败 {counts['failed']} 件，"
        f"历史待核对 {counts['blocked']} 件，结果不明 {counts['uncertain']} 件"
    )


_SAFE_EXCEPTION_TYPES = frozenset({
    "InventoryContractError", "InventoryMarketplaceError", "DomContractError", "ItemLoadError", "TimeoutError",
    "ItemClickError", "ItemPanelLoadError", "ItemPanelResetError", "InventoryItemHttpError",
    "ItemSearchLoadError", "InventoryScanChanged",
    "TargetClosedError", "Error", "RuntimeError", "ValueError", "TypeError",
    "AttributeError", "AssertionError", "KeyError", "ConnectionError", "OSError",
    "InventoryResultUncertain", "InventorySubmissionBlocked", "PersonSelectionError", "ManufacturerSelectionError",
})


def _exception_chain(exc: Exception) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 8 and all(current is not item for item in chain):
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return chain


def _safe_exception_type(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if name in _SAFE_EXCEPTION_TYPES else "Exception"


def _safe_reconciliation_failure(exc: Exception) -> tuple[dict[str, Any], str]:
    chain = _exception_chain(exc)
    for cause in chain:
        if isinstance(cause, InventoryMarketplaceError) and cause.reason in MENU_ERRORS:
            return {"reason_code": "marketplace_menu_" + cause.reason,
                    "exception_type": "InventoryMarketplaceError"}, MENU_ERRORS[cause.reason]
    for cause in chain:
        if isinstance(cause, PageRecoveryRequired):
            return {"reason_code": cause.reason_code, "exception_type": "PageRecoveryRequired",
                    "http_status": cause.http_status}, f"{cause.public_reason}，等待手动继续"
    # Only the bounded, fixed inventory error prefix yields an HTTP status.
    # Never return the rest of an exception, which may contain a signed URL.
    for cause in chain:
        if isinstance(cause, InventoryContractError):
            match = re.match(r"德国站库存接口返回 HTTP ([45]\d{2})，", str(cause))
            if match:
                status = int(match.group(1))
                return {"reason_code": "inventory_http_error", "exception_type": _safe_exception_type(cause),
                        "http_status": status}, f"库存接口返回 HTTP {status}"
    for cause in chain:
        message = str(cause).lower()
        if type(cause).__name__ == "TargetClosedError" or "target page, context or browser has been closed" in message:
            return {"reason_code": "target_closed", "exception_type": _safe_exception_type(cause)}, "页面或紫鸟连接已关闭"
    for cause in chain:
        if isinstance(cause, (TimeoutError, ItemLoadError)) or type(cause).__name__ == "TimeoutError":
            return {"reason_code": "read_timeout", "exception_type": _safe_exception_type(cause)}, "页面加载或读取超时"
    for cause in chain:
        if isinstance(cause, DomContractError):
            return {"reason_code": "page_contract_error", "exception_type": _safe_exception_type(cause)}, "页面结构或商品身份未通过核验"
    return {"reason_code": "reconciliation_error", "exception_type": _safe_exception_type(exc)}, "回读执行异常，需核查本地诊断"


def _safe_exception_frames(exc: Exception) -> list[dict[str, Any]]:
    result = []
    for cause in _exception_chain(exc):
        frames = []
        tb = cause.__traceback__
        while tb is not None and len(frames) < 8:
            code = tb.tb_frame.f_code
            frames.append(f"{Path(code.co_filename).name}:{tb.tb_lineno}:{code.co_name}")
            tb = tb.tb_next
        result.append({"exception_type": _safe_exception_type(cause), "frames": frames})
    return result


def _safe_reconciliation_stage(
    exc: Exception, *, default: tuple[str, str] = ("history_reconciliation", "历史提交回读"),
) -> tuple[str, str]:
    """Derive a fixed stage label, without persisting dynamic stack contents."""
    stages = {
        "_choose_marketplace": ("marketplace_switch", "商城菜单切换"),
        "_switch_marketplace": ("marketplace_switch", "商城菜单切换"),
        "locate_item": ("search_or_location", "商品定位或 SKU 搜索"),
        "_read_search_state": ("search_or_location", "商品定位或 SKU 搜索"),
        "search_result": ("search_or_location", "商品定位或 SKU 搜索"),
        "_panel_snapshot": ("sidebar_read", "商品侧栏回读"),
        "_wait_panel": ("sidebar_read", "商品侧栏回读"),
        "_inspect_loaded_panel": ("sidebar_read", "商品侧栏回读"),
        "_read_list": ("inventory_list", "库存列表读取"),
        "_wait_list": ("inventory_list", "库存列表读取"),
        "_identity": ("identity_check", "卖家身份核验"),
        "_validate_form": ("attestation_form", "安全证明表单核验"),
        "_inspect_person": ("responsible_person_form", "关联信息表单核验"),
        "prepare_submission": ("prepare_submission", "提交前选择核验"),
        "submit_once": ("submission", "提交派发"),
        "_submit_person_once": ("submission", "关联信息提交派发"),
        "confirm_submission": ("submission_receipt", "提交回执确认"),
    }
    for cause in reversed(_exception_chain(exc)):
        names = []
        tb = cause.__traceback__
        while tb is not None:
            names.append(tb.tb_frame.f_code.co_name)
            tb = tb.tb_next
        for name in reversed(names):
            if name in stages:
                return stages[name]
    return default


def _snapshot(run: WorkflowRun, marketplace: MarketplaceRef) -> MarketplaceSnapshot:
    return MarketplaceSnapshot(
        marketplace_code=marketplace.code, domain=marketplace.domain,
        seller_id=run.store.expected_seller_id, payment_account="", currency=marketplace.currency,
        payable_amount=Decimal(0), delayed_amount=Decimal(0),
        settlement_key=f"inventory-gpsr:{run.id}", can_submit=True,
        contract_version="inventory-gpsr-v1", page_fingerprint="inventory-approval-required-v1",
    )
