"""Thin runtime facade consumed by FastAPI and the scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from ziniao_automation.db import utc_now as database_utc_now
from ziniao_automation.models import (
    OperationGuard,
    Run,
    RunQueueEntry,
    Store,
    StoreMarketplace,
)
from ziniao_automation.queue import (
    DurableRunQueue,
    PersistentQueueWorker,
    PRIORITY_HUMAN,
    PRIORITY_RECOVERY,
)
from ziniao_automation.ziniao.models import ProfileSelector
from ziniao_automation.ziniao.errors import AuthWaitCancelled, AuthWaitExpired

from .engine import WorkflowEngine
from .amazon_disbursement import AmazonPaymentsPage
from .amazon_disbursement.config import ALLOWED_MARKETPLACES
from .errors import HumanAuthRequired, PreflightRejected, WorkflowNotRegistered
from .types import (
    MarketplaceRef,
    RunMode,
    RunStatus,
    StoreRef,
    WorkflowRun,
)

logger = logging.getLogger(__name__)


_MARKETPLACE_SETUP_ACTIVE_SITE_STATES = {
    "PENDING",
    "CHECKING",
    "WAITING_AUTH",
}
_MARKETPLACE_SETUP_TERMINAL_STATES = {
    "SUCCEEDED",
    "PARTIAL",
    "UNAVAILABLE",
    "NEEDS_REVIEW",
    "FAILED",
    "CANCELLED",
}

_MARKETPLACE_SETUP_DEADLINE_MESSAGE = (
    "自动建档已达到全程 30 分钟硬截止；仍在等待的页面操作已取消，"
    "对应紫鸟店铺窗口已释放。请重新发起自动建档。"
)


class _MarketplaceSetupDeadlineExpired(TimeoutError):
    """The process-wide setup deadline, not a per-page navigation timeout."""

_ASSISTED_LOGIN_WAIT_MESSAGE = (
    "当前站点的自动登录已停止：程序会先等待紫鸟填充，再依次处理邮箱 Continue、"
    "紫鸟托管 Passkey、已填密码登录和已填好的 6 位 OTP；每个页面动作最多自动点击 3 次，"
    "每个站点最多进行 3 轮完整验证，CA、UK、AU 分别计数。当前页面可能已达到次数上限，"
    "或出现未填内容、候选不唯一、CAPTCHA、非托管 Passkey 等不适合自动点击的情况。"
    "请在原紫鸟窗口处理后点击“再次尝试自动登录并继续”；不会切换到普通 Chrome。"
)


@dataclass(slots=True)
class _IdentityProbe:
    id: str
    store_id: int
    task: asyncio.Task[dict[str, Any]]
    ready: asyncio.Event
    created_at: float
    expires_at: float
    state: str = "CHECKING"
    result: dict[str, Any] | None = None
    message: str = "正在通过对应紫鸟店铺读取 Seller Central 身份"


@dataclass(slots=True)
class _MarketplaceSetupProbe:
    """One process-local, same-Ziniao-window account-baseline probe."""

    id: str
    store_id: int
    marketplace_codes: tuple[str, ...]
    seller_id: str
    task: asyncio.Task[dict[str, Any]]
    created_at: float
    expires_at: float
    state: str = "CHECKING"
    result: dict[str, Any] | None = None
    marketplaces: list[dict[str, Any]] | None = None
    message: str = "正在通过对应紫鸟店铺按顺序检测付款账户"
    unified: bool = False
    identity: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StartupRecoveryPlan:
    """Disjoint run groups reconstructed from durable SQLite state."""

    financial: tuple[WorkflowRun, ...] = ()
    # Includes every run protected by an active guard, even when an old or
    # malformed row could not be converted into today's WorkflowRun object.
    # The queue uses this complete set to ensure no guarded item can ever be
    # restored as an ordinary START action.
    financial_guarded_ids: tuple[str, ...] = ()
    financial_failed_ids: tuple[str, ...] = ()
    reconciling_ids: tuple[str, ...] = ()
    queued_ids: tuple[str, ...] = ()
    running_ids: tuple[str, ...] = ()
    waiting_auth_ids: tuple[str, ...] = ()


class DatabaseRunLoader:
    """Build the workflow's immutable input snapshot from SQLite."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | Any,
        *,
        artifact_root: Path | None = None,
        workflow_registry: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.artifact_root = artifact_root
        self.workflow_registry = workflow_registry

    async def __call__(self, run_id: str) -> WorkflowRun:
        with self.session_factory() as session:
            row = session.scalar(
                select(Run)
                .where(Run.id == run_id)
                .options(selectinload(Run.store).selectinload(Store.marketplaces))
            )
            if row is None:
                raise LookupError(f"任务 {run_id} 不存在")
            if self.workflow_registry is not None:
                try:
                    definition = self.workflow_registry.definition(row.workflow)
                except WorkflowNotRegistered as exc:
                    raise ValueError("任务工作流不在当前版本的代码白名单中") from exc
                expected_version = int(definition.config_version)
                if int(row.workflow_config_version) != expected_version:
                    raise ValueError("任务工作流配置版本与当前代码不兼容")
                try:
                    workflow_config = dict(
                        self.workflow_registry.validate_config(
                            row.workflow, dict(row.workflow_config or {})
                        )
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError("任务工作流参数不符合当前版本定义") from exc
                requires_marketplaces = bool(
                    definition.requires_marketplace_targets
                )
            else:
                if row.workflow != "amazon_disbursement":
                    raise ValueError("任务工作流不在当前版本的代码白名单中")
                expected_version = 1
                workflow_config = dict(row.workflow_config or {})
                requires_marketplaces = True
            if (
                row.workflow == "amazon_disbursement"
                and not workflow_config.get("marketplace_codes")
            ):
                legacy_codes = list(
                    (row.result_summary or {}).get("requested_marketplaces") or ()
                )
                if legacy_codes:
                    workflow_config = {"marketplace_codes": legacy_codes}
            store = row.store
            if not store.selector_type or not store.selector_value:
                raise ValueError("店铺缺少明确的紫鸟环境选择器")
            store_ref = StoreRef(
                id=str(store.id),
                name=store.name,
                selector_type=store.selector_type,
                selector_value=store.selector_value,
                expected_seller_id=store.expected_seller_id or "",
                enabled=bool(store.enabled),
                identity_confirmed=bool(store.identity_confirmed),
            )
            requested_codes = set(workflow_config.get("marketplace_codes") or ())
            enabled = tuple(
                MarketplaceRef(
                    id=str(market.id),
                    code=market.code,
                    domain=market.domain,
                    currency=market.currency,
                    enabled=bool(market.enabled),
                )
                for market in sorted(store.marketplaces, key=lambda item: item.code)
                if market.enabled
                and requires_marketplaces
                and (not requested_codes or market.code in requested_codes)
            )
            if requires_marketplaces and not enabled:
                raise ValueError("任务店铺没有启用的 CA/UK/AU 站点")
            return WorkflowRun(
                id=row.id,
                workflow=row.workflow,
                mode=RunMode(row.mode),
                store=store_ref,
                marketplaces=enabled,
                workflow_config=workflow_config,
                workflow_config_version=expected_version,
                requested_by=row.requested_by,
                artifact_dir=self.artifact_root,
            )

    async def recovery_runs(self) -> tuple[WorkflowRun, ...]:
        guarded = self._financial_guard_snapshot()
        loaded: list[WorkflowRun] = []
        for run_id in sorted(guarded):
            try:
                loaded.append(
                    await self._load_financial_recovery_run(
                        run_id, guarded[run_id]
                    )
                )
            except Exception as exc:
                self._mark_financial_recovery_unloadable(run_id, exc)
                logger.exception(
                    "financial_recovery_snapshot_invalid run_id=%s", run_id[:8]
                )
        return tuple(loaded)

    async def load_financial_recovery(self, run_id: str) -> WorkflowRun:
        """Load one guarded run for a queued/manual RECONCILE action."""

        guarded = self._financial_guard_snapshot()
        codes = guarded.get(run_id)
        if codes is None:
            with self.session_factory() as session:
                historical = tuple(
                    dict.fromkeys(
                        str(value).strip().upper()
                        for value in session.scalars(
                            select(OperationGuard.marketplace_code)
                            .where(OperationGuard.run_id == run_id)
                            .order_by(OperationGuard.marketplace_code)
                        )
                        if str(value or "").strip()
                    )
                )
            if not historical:
                raise ValueError("任务没有可回读的资金保护记录")
            codes = historical
        try:
            return await self._load_financial_recovery_run(run_id, codes)
        except Exception as exc:
            self._mark_financial_recovery_unloadable(run_id, exc)
            raise

    async def startup_recovery_plan(self) -> StartupRecoveryPlan:
        """Classify crash leftovers without allowing financial re-execution."""

        guarded = self._financial_guard_snapshot()
        financial_ids = set(guarded)
        with self.session_factory() as session:
            historical_guard_ids = set(
                session.scalars(select(OperationGuard.run_id).distinct())
            )
            ordinary = session.execute(
                select(Run.id, Run.status).where(
                    Run.status.in_(
                        (
                            "QUEUED",
                            "RUNNING",
                            "WAITING_APPROVAL",
                            "WAITING_AUTH",
                            "NEEDS_HUMAN_AUTH",
                            "RECONCILING",
                            "UNCERTAIN_FINANCIAL",
                        )
                    )
                )
            ).all()
        # Sets make the groups disjoint even if malformed legacy rows contain
        # more than one operation guard.  Any historical financial guard is
        # authoritative: the process may have crashed after its final guard
        # became CONFIRMED but before the parent run reached a terminal state.
        queued: list[str] = []
        running: list[str] = []
        waiting_auth: list[str] = []
        reconciling: list[str] = []
        for run_id, status in ordinary:
            if run_id in financial_ids:
                continue
            if run_id in historical_guard_ids:
                reconciling.append(run_id)
                continue
            if status == "QUEUED":
                queued.append(run_id)
            elif status == "RUNNING":
                running.append(run_id)
            elif status == "WAITING_AUTH":
                waiting_auth.append(run_id)
            elif status == "RECONCILING":
                reconciling.append(run_id)
        financial: list[WorkflowRun] = []
        failed: list[str] = []
        for run_id in sorted(financial_ids):
            try:
                financial.append(
                    await self._load_financial_recovery_run(run_id, guarded[run_id])
                )
            except Exception as exc:
                failed.append(run_id)
                self._mark_financial_recovery_unloadable(run_id, exc)
                logger.exception(
                    "financial_recovery_snapshot_invalid run_id=%s", run_id[:8]
                )
        return StartupRecoveryPlan(
            financial=tuple(financial),
            financial_guarded_ids=tuple(sorted(financial_ids)),
            financial_failed_ids=tuple(failed),
            reconciling_ids=tuple(sorted(reconciling)),
            queued_ids=tuple(sorted(queued)),
            running_ids=tuple(sorted(running)),
            waiting_auth_ids=tuple(sorted(waiting_auth)),
        )

    def _financial_guard_snapshot(self) -> dict[str, tuple[str, ...]]:
        """Return active guard site codes without trusting mutable run config."""

        with self.session_factory() as session:
            rows = session.execute(
                select(OperationGuard.run_id, OperationGuard.marketplace_code)
                .where(
                    OperationGuard.state.in_(("ARMED", "SUBMITTED", "UNCERTAIN"))
                )
                .order_by(OperationGuard.run_id, OperationGuard.marketplace_code)
            ).all()
        grouped: dict[str, list[str]] = {}
        for run_id, code in rows:
            normalized = str(code or "").strip().upper()
            bucket = grouped.setdefault(run_id, [])
            if normalized and normalized not in bucket:
                bucket.append(normalized)
        return {run_id: tuple(codes) for run_id, codes in grouped.items()}

    async def _load_financial_recovery_run(
        self, run_id: str, guard_marketplace_codes: Sequence[str]
    ) -> WorkflowRun:
        """Build the minimum safe read-back snapshot for an active guard.

        Recovery deliberately does not validate an old run against the latest
        workflow config model.  The irreversible action already happened (or
        may have happened), so the guard rows are the authoritative targets.
        Normal START/APPROVE paths still use ``__call__`` and its strict current
        version/schema checks.
        """

        codes = tuple(dict.fromkeys(str(code).upper() for code in guard_marketplace_codes))
        if not codes:
            raise ValueError("active financial guard has no marketplace code")
        with self.session_factory() as session:
            row = session.scalar(
                select(Run)
                .where(Run.id == run_id)
                .options(selectinload(Run.store).selectinload(Store.marketplaces))
            )
            if row is None:
                raise LookupError(f"任务 {run_id} 不存在")

            # An active guard is more authoritative than a contradictory run
            # terminal state.  Without this repair, engine.reconcile() treats
            # SUCCEEDED/PARTIAL/FAILED as already settled and returns without
            # reading the platform, leaving ARMED/SUBMITTED stranded forever.
            reconcilable_statuses = {
                "RUNNING",
                "WAITING_APPROVAL",
                "WAITING_AUTH",
                "NEEDS_HUMAN_AUTH",
                "UNCERTAIN_FINANCIAL",
                "RECONCILING",
            }
            if row.status not in reconcilable_statuses:
                previous_status = row.status
                row.status = "UNCERTAIN_FINANCIAL"
                row.error = (
                    "检测到资金保护记录与任务状态不一致，已强制进入资金回读"
                )
                from ziniao_automation.models import RunEvent

                session.add(
                    RunEvent(
                        run_id=run_id,
                        event_type="FINANCIAL_GUARD_STATUS_REPAIRED",
                        from_status=previous_status,
                        to_status="UNCERTAIN_FINANCIAL",
                        message=row.error,
                    )
                )
                session.commit()

            if self.workflow_registry is not None:
                try:
                    definition = self.workflow_registry.definition(row.workflow)
                except WorkflowNotRegistered as exc:
                    raise ValueError("资金任务工作流已不在代码白名单中") from exc
                if not bool(getattr(definition, "requires_financial_lock", False)):
                    raise ValueError("受资金保护的任务未注册为资金安全工作流")
            elif row.workflow != "amazon_disbursement":
                raise ValueError("资金任务工作流已不在代码白名单中")

            # V1 has one financial workflow.  Ignore its legacy/extra config
            # fields and reconstruct only the site target list from guards.
            if row.workflow != "amazon_disbursement":
                raise ValueError("当前版本没有该资金工作流的兼容回读适配器")
            workflow_config = {"marketplace_codes": list(codes)}

            store = row.store
            if store is None or not store.selector_type or not store.selector_value:
                raise ValueError("店铺缺少明确的紫鸟环境选择器")
            store_ref = StoreRef(
                id=str(store.id),
                name=store.name,
                selector_type=store.selector_type,
                selector_value=store.selector_value,
                expected_seller_id=store.expected_seller_id or "",
                enabled=bool(store.enabled),
                identity_confirmed=bool(store.identity_confirmed),
            )
            wanted = set(codes)
            marketplaces = tuple(
                MarketplaceRef(
                    id=str(market.id),
                    code=market.code,
                    domain=market.domain,
                    currency=market.currency,
                    # A later operator edit must not suppress read-back of an
                    # already armed/submitted operation.
                    enabled=True,
                )
                for market in sorted(store.marketplaces, key=lambda item: item.code)
                if market.code in wanted
            )
            return WorkflowRun(
                id=row.id,
                workflow=row.workflow,
                mode=RunMode(row.mode),
                store=store_ref,
                marketplaces=marketplaces,
                workflow_config=workflow_config,
                workflow_config_version=int(row.workflow_config_version or 1),
                requested_by=row.requested_by,
                artifact_dir=self.artifact_root,
            )

    def _mark_financial_recovery_unloadable(
        self, run_id: str, exc: Exception
    ) -> None:
        """Quarantine one malformed guarded run without blocking its peers."""

        message = (
            "启动资金回读准备失败，已保持资金结果不明确；"
            f"请人工核查（{type(exc).__name__}）"
        )
        with self.session_factory() as session:
            row = session.get(Run, run_id)
            if row is None:
                return
            previous = row.status
            row.status = "UNCERTAIN_FINANCIAL"
            row.error = message
            row.finished_at = database_utc_now()
            from ziniao_automation.models import RunEvent, SiteRun

            session.add(
                RunEvent(
                    run_id=run_id,
                    event_type="FINANCIAL_RECOVERY_LOAD_FAILED",
                    from_status=previous,
                    to_status="UNCERTAIN_FINANCIAL",
                    message=message,
                )
            )
            for site in session.scalars(
                select(SiteRun).where(
                    SiteRun.run_id == run_id,
                    SiteRun.status.in_(
                        ("ARMED", "SUBMITTED", "RECONCILING", "WAITING_AUTH")
                    ),
                )
            ):
                site.status = "UNCERTAIN_FINANCIAL"
                site.error = message
            session.commit()

    async def mark_waiting_auth_after_restart(self, run_id: str) -> bool:
        """Persist the lost auth lease without requiring browser configuration.

        This path intentionally does not load MarketplaceRef objects: a store
        may have been disabled or edited while the process was down, but the
        durable run must still leave WAITING_AUTH.  User-triggered continue
        later performs the normal full loader/preflight validation.
        """

        with self.session_factory() as session:
            row = session.get(Run, run_id)
            if row is None or row.status != "WAITING_AUTH":
                return False
            row.status = "NEEDS_HUMAN_AUTH"
            row.error = "程序重启后原人工验证窗口已失效，请重新确认后继续"
            from ziniao_automation.models import RunEvent, SiteRun

            session.add(
                RunEvent(
                    run_id=run_id,
                    event_type="AUTH_LEASE_LOST_ON_RESTART",
                    from_status="WAITING_AUTH",
                    to_status="NEEDS_HUMAN_AUTH",
                    message="程序重启后需人工重新确认",
                )
            )
            for site in session.scalars(
                select(SiteRun).where(SiteRun.run_id == run_id)
            ):
                if site.status == "WAITING_AUTH":
                    site.status = "NEEDS_HUMAN_AUTH"
            session.commit()
            return True


class AutomationService:
    """Web-facing queue with exactly one in-process task per run ID."""

    def __init__(
        self,
        *,
        engine: WorkflowEngine,
        run_loader: Callable[[str], Awaitable[WorkflowRun]],
        ziniao_controller: Any,
        recovery_loader: Callable[[], Awaitable[Sequence[WorkflowRun]]] | None = None,
        profile_sink: Callable[[Sequence[dict[str, Any]]], Awaitable[tuple[int, int]]] | None = None,
        session_factory: sessionmaker[Session] | Any | None = None,
        identity_auth_timeout_seconds: float = 1800.0,
        setup_cleanup_grace_seconds: float = 2.0,
    ) -> None:
        self.engine = engine
        self.run_loader = run_loader
        self.ziniao_controller = ziniao_controller
        self.recovery_loader = recovery_loader
        self.profile_sink = profile_sink
        self.session_factory = session_factory
        registry = getattr(engine, "registry", None)
        priority_resolver = (
            (lambda workflow: int(registry.definition(workflow).business_priority))
            if registry is not None and hasattr(registry, "definition")
            else None
        )
        self._queue = (
            DurableRunQueue(
                session_factory,
                business_priority_resolver=priority_resolver,
            )
            if session_factory is not None
            else None
        )
        self._worker = (
            PersistentQueueWorker(
                self._queue,
                self._execute_queue_entry,
                notifications=getattr(engine, "delivery_service", None),
            )
            if self._queue is not None
            else None
        )
        # Process-local fallback retained only for isolated tests that do not
        # provide a database. Production always uses the durable queue above.
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._guard = asyncio.Lock()
        if identity_auth_timeout_seconds <= 0:
            raise ValueError("identity_auth_timeout_seconds must be positive")
        if setup_cleanup_grace_seconds <= 0:
            raise ValueError("setup_cleanup_grace_seconds must be positive")
        self.identity_auth_timeout_seconds = float(identity_auth_timeout_seconds)
        self.setup_cleanup_grace_seconds = float(setup_cleanup_grace_seconds)
        self._identity_probes: dict[str, _IdentityProbe] = {}
        self._identity_store_probes: dict[int, str] = {}
        self._identity_guard = asyncio.Lock()
        self._marketplace_setup_probes: dict[str, _MarketplaceSetupProbe] = {}
        self._marketplace_setup_store_probes: dict[int, str] = {}
        self._marketplace_setup_guard = asyncio.Lock()

    def set_delivery_service(self, deliveries: Any | None) -> None:
        """Attach explicit notices after composition has built the worker."""

        if self._worker is not None:
            self._worker.notifications = deliveries

    async def start_webdriver_mode(self) -> dict[str, Any]:
        """Switch Ziniao into WebDriver mode, refusing while work is in flight.

        Exposed here because this is the only object that can see both halves of
        the safety question: the controller's live lock state, and the durable
        guards in SQLite.  Killing the browser between ``arm_operation`` and the
        irreversible click would strand a payout nobody can account for.
        """

        from ziniao_automation.ziniao.webdriver_mode import (
            describe_blockers,
            start_webdriver_mode,
        )

        controller = self.ziniao_controller
        snapshot = None
        locks = getattr(controller, "locks", None)
        if locks is not None and hasattr(locks, "snapshot"):
            snapshot = locks.snapshot()

        pending: list[str] = []
        if self.session_factory is not None:
            from ziniao_automation.models import OperationGuard
            from sqlalchemy import select as _select

            with self.session_factory() as session:
                pending = [
                    f"{row.marketplace_code}（{row.state}）"
                    for row in session.scalars(
                        _select(OperationGuard).where(
                            OperationGuard.state.in_(("ARMED", "SUBMITTED"))
                        )
                    )
                ]

        config = getattr(controller, "config", None)
        client_config = getattr(getattr(controller, "client", None), "config", None)
        report = await start_webdriver_mode(
            Path(getattr(config, "client_path", "")),
            host=str(getattr(client_config, "host", "127.0.0.1")),
            port=int(getattr(client_config, "port", 16851)),
            blockers=describe_blockers(snapshot, pending),
        )
        return {
            "ok": report.ok,
            "status": report.status,
            "message": report.message,
            "closed_processes": report.closed_processes,
            "details": report.details,
        }

    async def sync_ziniao(self) -> dict[str, Any]:
        """Read profiles only; the HTTP request owns the single DB transaction.

        Persisting here as well as in ``web.sync_ziniao`` caused every new
        profile to be inserted twice. SQLite then raised a UNIQUE constraint
        error and the browser saw an opaque HTTP 500.
        """
        profiles = await self.ziniao_controller.sync_profiles()
        payload = [
                {
                    "name": item.name,
                    "selector_type": item.selector_type,
                    "selector_value": item.selector_value,
                    "browser_oauth": item.browser_oauth,
                    "browser_id": item.browser_id,
                    "raw": dict(item.raw),
                }
                for item in profiles
            ]
        if self.profile_sink is not None:
            created, updated = await self.profile_sink(payload)
        else:
            # Production FastAPI persists ``profiles`` with the current
            # ZiniaoAccount in one request-scoped transaction.
            created = updated = 0
        return {"created": created, "updated": updated, "profiles": payload}

    async def detect_store_identity(self, store_id: int) -> dict[str, Any]:
        """Start one non-blocking, read-only identity probe.

        HTTP returns as soon as the first read either succeeds or reaches an
        authentication challenge.  In the latter case the background task
        retains the same Ziniao handle and per-store lock; no cookie, token or
        CDP endpoint is persisted.
        """

        if self.session_factory is None:
            raise RuntimeError("店铺身份检测服务尚未配置")
        with self.session_factory() as session:
            store = session.get(Store, store_id)
            if store is None:
                raise LookupError(f"店铺 {store_id} 不存在")
            selector_type = str(store.selector_type)
            selector_value = str(store.selector_value)
        selector = ProfileSelector(selector_type, selector_value)
        async with self._identity_guard:
            current_id = self._identity_store_probes.get(store_id)
            current = self._identity_probes.get(current_id or "")
            if current is not None and not current.task.done():
                # Reattach a refreshed/closed page to the same live probe.
                # This is deliberately not an error and never starts a second
                # Ziniao environment.
                return await self._identity_probe_payload_locked(current)
            if current is not None and current.task.done():
                self._identity_probes.pop(current.id, None)
            probe_id = str(uuid4())
            loop = asyncio.get_running_loop()
            ready = asyncio.Event()
            task = asyncio.create_task(
                self._run_identity_probe(
                    probe_id=probe_id,
                    store_id=store_id,
                    selector=selector,
                    ready=ready,
                ),
                name=f"identity-probe:{probe_id}",
            )
            probe = _IdentityProbe(
                id=probe_id,
                store_id=store_id,
                task=task,
                ready=ready,
                created_at=loop.time(),
                expires_at=loop.time() + self.identity_auth_timeout_seconds,
            )
            self._identity_probes[probe_id] = probe
            self._identity_store_probes[store_id] = probe_id
            task.add_done_callback(
                lambda done, pid=probe_id: asyncio.create_task(
                    self._finish_identity_probe(pid, done)
                )
            )

        # Page navigation is bounded by the adapter. This wait is only for the
        # first result/auth boundary; authentication itself never holds HTTP.
        await ready.wait()
        return await self.get_identity_probe(store_id, probe_id)

    async def _run_identity_probe(
        self,
        *,
        probe_id: str,
        store_id: int,
        selector: ProfileSelector,
        ready: asyncio.Event,
    ) -> dict[str, Any]:
        adapter = AmazonPaymentsPage()
        failures: list[str] = []
        try:
            async with self.ziniao_controller.session(
                selector, store_key=str(store_id)
            ) as handle:
                for code, (domain, currency) in ALLOWED_MARKETPLACES.items():
                    marketplace = MarketplaceRef(
                        id=f"identity-probe-{code.lower()}",
                        code=code,
                        domain=domain,
                        currency=currency,
                    )
                    try:
                        seller_id, source = await adapter.detect_identity(
                            handle.page, marketplace
                        )
                    except HumanAuthRequired:
                        probe = self._identity_probes[probe_id]
                        await self.ziniao_controller.wait_for_auth(
                            handle,
                            self._identity_auth_key(probe_id),
                            timeout_seconds=max(
                                0.001,
                                probe.expires_at - asyncio.get_running_loop().time(),
                            ),
                            on_waiting=lambda: self._publish_identity_auth_wait(
                                probe_id,
                                _ASSISTED_LOGIN_WAIT_MESSAGE,
                            ),
                        )
                        # Continue on the same page/handle and recheck this
                        # marketplace only. A repeated challenge re-arms an
                        # in-place lease instead of opening a new store.
                        while True:
                            try:
                                seller_id, source = await adapter.detect_identity(
                                    handle.page, marketplace
                                )
                                break
                            except HumanAuthRequired:
                                await self.ziniao_controller.wait_for_auth(
                                    handle,
                                    self._identity_auth_key(probe_id),
                                    timeout_seconds=max(
                                        0.001,
                                        probe.expires_at
                                        - asyncio.get_running_loop().time(),
                                    ),
                                    on_waiting=lambda: self._publish_identity_auth_wait(
                                        probe_id,
                                        _ASSISTED_LOGIN_WAIT_MESSAGE,
                                    ),
                                )
                    except PreflightRejected as exc:
                        failures.append(f"{code}: {exc}")
                        continue
                    return {
                        "status": "SUCCEEDED",
                        "probe_id": probe_id,
                        "store_id": store_id,
                        "seller_id": seller_id,
                        "source": source,
                        "marketplace_code": code,
                    }
        except RuntimeError:
            raise
        except Exception as exc:
            # Playwright's public ``Error`` is not a RuntimeError.  A target
            # may disappear briefly while Ziniao finishes creating its first
            # tab, so exposing that raw exception made this read-only probe an
            # opaque HTTP 500.  Do not retry here: one click must cause one
            # environment launch only, and the operator can retry deliberately.
            # Cancellation is intentionally not caught (CancelledError is a
            # BaseException on supported Python versions).
            logger.warning(
                "Store %s identity probe lost its Ziniao browser target (%s)",
                store_id,
                type(exc).__name__,
            )
            raise RuntimeError(
                "对应紫鸟店铺窗口尚未稳定或页面连接已中断；本次已停止，"
                "请确认该店铺窗口正常打开后再手动重试（不会连接普通 Chrome）"
            ) from exc
        detail = "；".join(failures)
        raise RuntimeError(
            "未能从 CA/UK/AU Seller Central 页面读取卖家身份"
            + (f"：{detail}" if detail else "")
        )

    async def _finish_identity_probe(
        self, probe_id: str, task: asyncio.Task[dict[str, Any]]
    ) -> None:
        async with self._identity_guard:
            probe = self._identity_probes.get(probe_id)
            if probe is None:
                return
            try:
                probe.result = task.result()
                if probe.state != "CANCELLED":
                    probe.state = "SUCCEEDED"
                    probe.message = "已读取 Seller Central 卖家身份"
            except asyncio.CancelledError:
                probe.state = "CANCELLED"
                probe.message = "身份检测已取消"
            except AuthWaitCancelled:
                probe.state = "CANCELLED"
                probe.message = "身份检测已取消"
            except AuthWaitExpired:
                probe.state = "EXPIRED"
                probe.message = "人工验证等待已超过30分钟，对应紫鸟店铺窗口已释放"
            except Exception as exc:
                # Only a human-readable message is retained in process memory.
                if probe.state != "CANCELLED":
                    probe.state = "FAILED"
                    probe.message = str(exc) or "身份检测失败"
            probe.ready.set()

    async def _publish_identity_auth_wait(self, probe_id: str, message: str) -> None:
        async with self._identity_guard:
            probe = self._identity_probes.get(probe_id)
            if probe is None:
                raise RuntimeError("身份检测记录已释放")
            probe.state = "WAITING_AUTH"
            probe.message = message
            probe.ready.set()

    async def get_identity_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        async with self._identity_guard:
            probe = self._identity_probes.get(str(probe_id))
            if probe is None or probe.store_id != int(store_id):
                raise LookupError("身份检测记录不存在或不属于该店铺")
            return await self._identity_probe_payload_locked(probe)

    async def _identity_probe_payload_locked(
        self, probe: _IdentityProbe
    ) -> dict[str, Any]:
        """Build a public payload while ``_identity_guard`` is held."""

        remaining = max(0, int(probe.expires_at - asyncio.get_running_loop().time()))
        if probe.state == "SUCCEEDED" and probe.result is not None:
            return dict(probe.result)
        return {
            "status": probe.state,
            "probe_id": probe.id,
            "store_id": probe.store_id,
            "expires_in_seconds": remaining,
            "message": probe.message,
        }

    async def continue_identity_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        async with self._identity_guard:
            probe = self._identity_probes.get(str(probe_id))
            if probe is None or probe.store_id != int(store_id):
                raise LookupError("身份检测记录不存在或不属于该店铺")
            if probe.state != "WAITING_AUTH" or probe.task.done():
                raise RuntimeError(f"当前身份检测状态为 {probe.state}，无需继续")
            probe.state = "CHECKING"
            probe.message = "正在使用原紫鸟店铺窗口尝试自动登录并继续检测"
        signal = getattr(self.ziniao_controller, "continue_auth", None)
        if not callable(signal) or not await signal(self._identity_auth_key(probe_id)):
            async with self._identity_guard:
                if probe.state == "CHECKING":
                    probe.state = "FAILED"
                    probe.message = "原紫鸟验证会话已失效，请重新发起身份检测"
            raise RuntimeError("原紫鸟验证会话已失效，请重新发起身份检测")
        return await self.get_identity_probe(store_id, probe_id)

    async def cancel_identity_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        async with self._identity_guard:
            probe = self._identity_probes.get(str(probe_id))
            if probe is None or probe.store_id != int(store_id):
                raise LookupError("身份检测记录不存在或不属于该店铺")
            if probe.task.done():
                raise RuntimeError(f"当前身份检测状态为 {probe.state}，不能取消")
        signal = getattr(self.ziniao_controller, "cancel_auth", None)
        signalled = bool(
            callable(signal) and await signal(self._identity_auth_key(probe_id))
        )
        if not signalled:
            probe.task.cancel()
        async with self._identity_guard:
            probe.state = "CANCELLED"
            probe.message = "身份检测已取消，正在释放对应紫鸟店铺窗口"
        # The controller cancellation wakes the background lease; join it so a
        # 200 response means stopBrowser/lock cleanup has actually completed.
        # This is local cleanup only and never starts or retries a browser.
        try:
            await asyncio.wait_for(asyncio.shield(probe.task), timeout=15.0)
        except (AuthWaitCancelled, asyncio.CancelledError, RuntimeError):
            pass
        except TimeoutError:
            probe.task.cancel()
            await asyncio.gather(probe.task, return_exceptions=True)
        return await self.get_identity_probe(store_id, probe_id)

    async def shutdown_identity_probes(self) -> None:
        """Cancel and join every live setup/identity probe before shutdown."""

        async with self._identity_guard:
            identity_tasks = tuple(
                probe.task
                for probe in self._identity_probes.values()
                if not probe.task.done()
            )
            for task in identity_tasks:
                task.cancel()
        async with self._marketplace_setup_guard:
            setup_tasks = tuple(
                probe.task
                for probe in self._marketplace_setup_probes.values()
                if not probe.task.done()
            )
            for task in setup_tasks:
                task.cancel()
        tasks = identity_tasks + setup_tasks
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            # Done callbacks update the process-local terminal snapshots.
            await asyncio.sleep(0)

    async def has_active_store_setup(self, store_id: int) -> bool:
        """Return whether any process-local browser probe owns this store."""

        store_id = int(store_id)
        async with self._identity_guard:
            identity_id = self._identity_store_probes.get(store_id)
            identity_probe = self._identity_probes.get(identity_id or "")
            if identity_probe is not None and not identity_probe.task.done():
                return True
        async with self._marketplace_setup_guard:
            setup_id = self._marketplace_setup_store_probes.get(store_id)
            setup_probe = self._marketplace_setup_probes.get(setup_id or "")
            return bool(setup_probe is not None and not setup_probe.task.done())

    @staticmethod
    def _identity_auth_key(probe_id: str) -> str:
        return f"identity-probe:{probe_id}"

    async def _run_setup_probe_before_deadline(
        self,
        *,
        expires_at: float,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Run every setup phase under one absolute, cancellation-safe limit.

        The timeout surrounds lock acquisition, CDP calls, assisted login and
        ``financial_session.__aexit__``.  Cancelling the current task therefore
        interrupts a hung Playwright await and unwinds the same session instead
        of merely changing the public probe status while the browser stays held.
        """

        operation_task = asyncio.create_task(operation())
        loop = asyncio.get_running_loop()
        try:
            done, _ = await asyncio.wait(
                {operation_task},
                timeout=max(0.0, expires_at - loop.time()),
            )
            if operation_task in done:
                return operation_task.result()

            operation_task.cancel()
            cleanup_done, _ = await asyncio.wait(
                {operation_task},
                timeout=self.setup_cleanup_grace_seconds,
            )
            if operation_task in cleanup_done:
                self._consume_setup_operation_result(operation_task)
            else:
                # A second cancellation interrupts a cleanup await that caught
                # the first cancellation. Never await it again on the public
                # probe path: controller cleanup independently owns bounded
                # lock release and best-effort stopBrowser.
                operation_task.cancel()
                operation_task.add_done_callback(
                    self._consume_setup_operation_result
                )
            raise _MarketplaceSetupDeadlineExpired(
                _MARKETPLACE_SETUP_DEADLINE_MESSAGE
            )
        except asyncio.CancelledError:
            operation_task.cancel()
            try:
                cleanup_done, _ = await asyncio.wait(
                    {operation_task},
                    timeout=self.setup_cleanup_grace_seconds,
                )
            except asyncio.CancelledError:
                cleanup_done = set()
            if operation_task in cleanup_done:
                self._consume_setup_operation_result(operation_task)
            else:
                operation_task.cancel()
                operation_task.add_done_callback(
                    self._consume_setup_operation_result
                )
            raise

    @staticmethod
    def _consume_setup_operation_result(task: asyncio.Task[Any]) -> None:
        """Consume a joined or detached setup task after cancellation."""

        try:
            task.exception()
        except BaseException:
            pass

    async def detect_store_setup(
        self,
        store_id: int,
        marketplace_codes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Discover seller identity and account tails in one Ziniao session.

        Unlike the legacy account-only probe, this entry point deliberately
        accepts an unconfigured store.  The live seller discovered from this
        exact browser handle becomes the only identity allowed to receive the
        account observations that follow.  It never calls the final payment
        submit action or creates an operation guard.
        """

        if self.session_factory is None:
            raise RuntimeError("店铺统一自动建档服务尚未配置")
        normalized = tuple(
            dict.fromkeys(
                str(code).strip().upper() for code in (marketplace_codes or ())
            )
        )
        if not normalized:
            raise ValueError("统一建档必须明确选择至少一个站点，禁止自动扩展到其他地区")
        unknown = set(normalized) - set(ALLOWED_MARKETPLACES)
        if unknown:
            raise ValueError(
                "统一建档站点只能是 CA、UK、AU；无效站点："
                + "、".join(sorted(unknown))
            )

        with self.session_factory() as session:
            store = session.get(Store, int(store_id))
            if store is None:
                raise LookupError(f"店铺 {store_id} 不存在")
            selector = ProfileSelector(
                str(store.selector_type), str(store.selector_value)
            )
            store_name = str(store.name)

        async with self._marketplace_setup_guard:
            current_id = self._marketplace_setup_store_probes.get(int(store_id))
            current = self._marketplace_setup_probes.get(current_id or "")
            if current is not None and current.task.done():
                self._finalize_marketplace_setup_probe_locked(current, current.task)
            if current is not None and current.state in {"CHECKING", "WAITING_AUTH"}:
                if current.unified:
                    return self._marketplace_setup_payload_locked(current)
                raise RuntimeError("该店铺正在执行旧版付款账户建档，请完成或取消后再统一建档")
            if current is not None:
                self._marketplace_setup_probes.pop(current.id, None)
                if self._marketplace_setup_store_probes.get(int(store_id)) == current.id:
                    self._marketplace_setup_store_probes.pop(int(store_id), None)
            identity_id = self._identity_store_probes.get(int(store_id))
            identity_probe = self._identity_probes.get(identity_id or "")
            if identity_probe is not None and not identity_probe.task.done():
                raise RuntimeError("该店铺正在单独检测卖家身份，请完成或取消后再统一建档")

            # Validate the *current* database/vault reference before creating
            # a background task or changing any site row.  This is a local,
            # no-network check: a global credentials problem must reach the
            # HTTP caller immediately instead of becoming four launch retries
            # for every store in the browser-side batch queue.
            credential_preflight = getattr(
                self.ziniao_controller, "preflight_credentials", None
            )
            if callable(credential_preflight):
                credential_preflight()

            # Persist explicit site selection only after proving this request
            # will create a new probe. Reattaching to an existing probe must be
            # a strictly read-only operation with no hidden configuration edit.
            with self.session_factory() as session:
                persisted_store = session.get(Store, int(store_id))
                if persisted_store is None:
                    raise LookupError(f"店铺 {store_id} 不存在")
                if (
                    str(persisted_store.selector_type) != selector.type
                    or str(persisted_store.selector_value) != selector.value
                ):
                    raise RuntimeError("建档开始前紫鸟店铺环境已变化，请重新发起")
                # The explicitly requested codes are also the operator's
                # explicit enable decision. Existing unrequested rows remain
                # untouched.
                for code in normalized:
                    domain, currency = ALLOWED_MARKETPLACES[code]
                    row = session.scalar(
                        select(StoreMarketplace).where(
                            StoreMarketplace.store_id == int(store_id),
                            StoreMarketplace.code == code,
                        )
                    )
                    if row is None:
                        row = StoreMarketplace(
                            store_id=int(store_id),
                            code=code,
                            domain=domain,
                            currency=currency,
                            enabled=True,
                        )
                        session.add(row)
                    else:
                        if row.domain.lower() != domain or row.currency.upper() != currency:
                            raise RuntimeError(
                                f"{code} 站点现有域名或币种不是固定白名单值，统一建档已停止"
                            )
                        row.enabled = True
                session.commit()

            probe_id = str(uuid4())
            loop = asyncio.get_running_loop()
            initial_sites = [
                {
                    "code": code,
                    "domain": ALLOWED_MARKETPLACES[code][0],
                    "currency": ALLOWED_MARKETPLACES[code][1],
                    "status": "PENDING",
                    "message": "等待卖家身份检测完成",
                }
                for code in normalized
            ]
            expires_at = loop.time() + self.identity_auth_timeout_seconds
            task = asyncio.create_task(
                self._run_setup_probe_before_deadline(
                    expires_at=expires_at,
                    operation=lambda: self._run_store_setup_probe(
                        probe_id=probe_id,
                        store_id=int(store_id),
                        store_name=store_name,
                        selector=selector,
                        marketplace_codes=normalized,
                    ),
                ),
                name=f"store-setup-probe:{probe_id}",
            )
            probe = _MarketplaceSetupProbe(
                id=probe_id,
                store_id=int(store_id),
                marketplace_codes=normalized,
                seller_id="",
                task=task,
                created_at=loop.time(),
                expires_at=expires_at,
                marketplaces=initial_sites,
                message="正在同一紫鸟店铺窗口中先检测卖家身份",
                unified=True,
                identity={
                    "status": "PENDING",
                    "seller_id": None,
                    "source": None,
                    "marketplace_code": None,
                    "persistence_state": None,
                    "identity_confirmed": None,
                    "store_enabled": None,
                    "message": "等待检测 Seller Central 卖家身份",
                },
            )
            self._marketplace_setup_probes[probe_id] = probe
            self._marketplace_setup_store_probes[int(store_id)] = probe_id
            task.add_done_callback(
                lambda done, pid=probe_id: asyncio.create_task(
                    self._finish_marketplace_setup_probe(pid, done)
                )
            )
            return self._marketplace_setup_payload_locked(probe)

    async def _run_store_setup_probe(
        self,
        *,
        probe_id: str,
        store_id: int,
        store_name: str,
        selector: ProfileSelector,
        marketplace_codes: tuple[str, ...],
    ) -> dict[str, Any]:
        financial_session = getattr(self.ziniao_controller, "financial_session", None)
        if not callable(financial_session):
            raise RuntimeError("紫鸟控制器缺少资金全局锁，已停止统一建档")
        adapter = AmazonPaymentsPage()
        async with financial_session(selector, store_key=str(store_id)) as handle:
            seller_id = ""
            identity_source = ""
            identity_code = ""
            failures: list[str] = []
            for code in marketplace_codes:
                domain, currency = ALLOWED_MARKETPLACES[code]
                marketplace = MarketplaceRef(
                    id=f"store-setup-identity-{code.lower()}",
                    code=code,
                    domain=domain,
                    currency=currency,
                    enabled=True,
                )
                await self._set_store_setup_identity(
                    probe_id,
                    status="CHECKING",
                    marketplace_code=code,
                    message=f"正在从 {code} Seller Central 读取卖家身份",
                )
                while True:
                    try:
                        seller_id, identity_source = await adapter.detect_identity(
                            handle.page, marketplace
                        )
                        handle.page = adapter.resolved_page_for(handle.page)
                        identity_code = code
                        break
                    except HumanAuthRequired:
                        resolved_page = adapter.resolved_page_for(handle.page)
                        if resolved_page is not handle.page:
                            handle.page = resolved_page
                            await self._set_store_setup_identity(
                                probe_id,
                                status="CHECKING",
                                marketplace_code=code,
                                message="已接管同一紫鸟窗口的新业务标签页，正在复核卖家身份",
                            )
                            continue
                        probe = self._marketplace_setup_probes[probe_id]
                        await self.ziniao_controller.wait_for_auth(
                            handle,
                            self._marketplace_setup_auth_key(probe_id),
                            timeout_seconds=max(
                                0.001,
                                probe.expires_at - asyncio.get_running_loop().time(),
                            ),
                            on_waiting=lambda c=code: self._publish_store_setup_identity_auth_wait(
                                probe_id,
                                c,
                                _ASSISTED_LOGIN_WAIT_MESSAGE,
                            ),
                        )
                        await self._set_store_setup_identity(
                            probe_id,
                            status="CHECKING",
                            marketplace_code=code,
                            message="正在复用原紫鸟窗口继续读取卖家身份",
                        )
                    except PreflightRejected as exc:
                        failures.append(f"{code}: {exc}")
                        break
                if seller_id:
                    break
            if not seller_id:
                detail = "；".join(failures)
                raise RuntimeError(
                    "未能从所选 Seller Central 站点读取卖家身份"
                    + (f"：{detail}" if detail else "")
                )

            (
                identity_result,
                existing_identity,
                identity_confirmed,
                store_enabled,
            ) = self._persist_store_setup_identity(
                store_id=store_id,
                seller_id=seller_id,
                selector=selector,
                marketplace_code=identity_code,
            )
            async with self._marketplace_setup_guard:
                probe = self._marketplace_setup_probes[probe_id]
                probe.seller_id = seller_id
            if identity_result == "NEEDS_REVIEW":
                message = (
                    "检测到的卖家身份与现有建档不一致；原身份和付款账户均未覆盖。"
                    "请先人工核对，必要时使用重置建档后重新检测"
                )
                await self._set_store_setup_identity(
                    probe_id,
                    status="NEEDS_REVIEW",
                    seller_id=seller_id,
                    source=identity_source,
                    marketplace_code=identity_code,
                    expected_seller_id=existing_identity,
                    persistence_state=identity_result,
                    identity_confirmed=identity_confirmed,
                    store_enabled=store_enabled,
                    message=message,
                )
                for code in marketplace_codes:
                    await self._set_marketplace_setup_site(
                        probe_id,
                        code,
                        status="NEEDS_REVIEW",
                        message="卖家身份不一致，本次未读取付款账户",
                    )
                return {
                    "status": "NEEDS_REVIEW",
                    "probe_id": probe_id,
                    "store_id": store_id,
                    "seller_id": seller_id,
                    "identity": await self._store_setup_identity_snapshot(probe_id),
                    "marketplaces": await self._marketplace_setup_sites_snapshot(probe_id),
                    "message": message,
                }

            identity_message = (
                "已从当前紫鸟店铺自动读取、绑定并确认卖家身份"
                if identity_result == "CREATED"
                else (
                    "本次检测身份与原建档一致，已自动补全身份确认并启用店铺"
                    if identity_result == "PROMOTED"
                    else "本次检测身份与现有建档一致"
                )
            )
            await self._set_store_setup_identity(
                probe_id,
                status="SUCCEEDED",
                seller_id=seller_id,
                source=identity_source,
                marketplace_code=identity_code,
                persistence_state=identity_result,
                identity_confirmed=identity_confirmed,
                store_enabled=store_enabled,
                message=identity_message,
            )
            store_ref = StoreRef(
                id=str(store_id),
                name=store_name,
                selector_type=selector.type,
                selector_value=selector.value,
                expected_seller_id=seller_id,
                # This setup-only reference is derived from the identity just
                # persisted from this same live handle.  The database binding
                # is already confirmed above before any account observation
                # can be attributed to it.
                enabled=True,
                identity_confirmed=True,
            )
            return await self._run_store_setup_account_sites(
                probe_id=probe_id,
                store_id=store_id,
                selector=selector,
                store_ref=store_ref,
                marketplace_codes=marketplace_codes,
                handle=handle,
                adapter=adapter,
            )

    async def _run_store_setup_account_sites(
        self,
        *,
        probe_id: str,
        store_id: int,
        selector: ProfileSelector,
        store_ref: StoreRef,
        marketplace_codes: tuple[str, ...],
        handle: Any,
        adapter: AmazonPaymentsPage,
    ) -> dict[str, Any]:
        """Confirm each selected site opens as the same seller.  Never clicks.

        This used to press the dashboard's non-final Request-disbursement
        control on every site just to read the payout tail off the details
        page.  That baseline no longer gates anything — Amazon owns the
        destination and this automation cannot change it — so setup now stays
        entirely on the payments dashboard: no clicks, no details page, and no
        way for enrolment to consume a run.
        """

        # Only ``selector`` is genuinely unused here — the payments dashboard is
        # reached through the handle this method is given.  ``store_id`` is still
        # needed by the terminal payload below; deleting it too made every unified
        # setup that got as far as the return raise UnboundLocalError and finish
        # FAILED, so setup could never report success.
        del selector
        for code in marketplace_codes:
            domain, currency = ALLOWED_MARKETPLACES[code]
            marketplace = MarketplaceRef(
                id=f"store-setup-{code.lower()}",
                code=code,
                domain=domain,
                currency=currency,
                enabled=True,
            )
            run = WorkflowRun(
                id=f"store-setup:{probe_id}",
                workflow="amazon_disbursement",
                mode=RunMode.DRY_RUN,
                store=store_ref,
                marketplaces=(marketplace,),
                requested_by="unified-store-setup",
            )
            await self._set_marketplace_setup_site(
                probe_id,
                code,
                status="CHECKING",
                message="正在核验该站点的域名与卖家身份",
            )
            while True:
                try:
                    await adapter.preflight(handle.page, run, marketplace)
                    handle.page = adapter.resolved_page_for(handle.page)
                    break
                except HumanAuthRequired:
                    resolved_page = adapter.resolved_page_for(handle.page)
                    if resolved_page is not handle.page:
                        handle.page = resolved_page
                        continue
                    probe = self._marketplace_setup_probes[probe_id]
                    await self.ziniao_controller.wait_for_auth(
                        handle,
                        self._marketplace_setup_auth_key(probe_id),
                        timeout_seconds=max(
                            0.001,
                            probe.expires_at - asyncio.get_running_loop().time(),
                        ),
                        on_waiting=lambda c=code: self._publish_marketplace_setup_auth_wait(
                            probe_id,
                            c,
                            _ASSISTED_LOGIN_WAIT_MESSAGE,
                        ),
                    )
                    await self._set_marketplace_setup_site(
                        probe_id,
                        code,
                        status="CHECKING",
                        message="正在复用原紫鸟窗口继续核验",
                    )
                except PreflightRejected as exc:
                    await self._set_marketplace_setup_site(
                        probe_id, code, status="FAILED", message=str(exc)
                    )
                    break
            site = await self._marketplace_setup_site_snapshot(probe_id, code)
            if site.get("status") != "CHECKING":
                continue
            await self._set_marketplace_setup_site(
                probe_id,
                code,
                status="SUCCEEDED",
                message="域名与卖家身份核验通过",
            )

        sites = await self._marketplace_setup_sites_snapshot(probe_id)
        successes = sum(item.get("status") == "SUCCEEDED" for item in sites)
        if successes == len(marketplace_codes):
            status, message = "SUCCEEDED", "卖家身份及所选站点均已核验通过"
        elif successes:
            status, message = "PARTIAL", "卖家身份已建档；部分站点核验未通过"
        else:
            status, message = "FAILED", "卖家身份已建档，但所选站点均未通过核验"
        return {
            "status": status,
            "probe_id": probe_id,
            "store_id": store_id,
            "seller_id": store_ref.expected_seller_id,
            "identity": await self._store_setup_identity_snapshot(probe_id),
            "marketplaces": sites,
            "message": message,
        }

    async def detect_marketplace_setup(
        self,
        store_id: int,
        marketplace_codes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Start one setup-only account-baseline probe for a Ziniao store.

        The probe may use Amazon's non-final dashboard transition because the
        masked account tail is not present on the dashboard.  It runs under
        the controller's global funds lock, never calls ``submit_once`` and
        never creates an operation guard.  Only values read by the server from
        a verified details page are persisted.
        """

        if self.session_factory is None:
            raise RuntimeError("付款账户自动建档服务尚未配置")
        normalized = tuple(
            dict.fromkeys(str(code).strip().upper() for code in (marketplace_codes or ()))
        )
        if not normalized:
            raise ValueError("自动建档必须明确选择至少一个站点，禁止自动扩展到其他地区")
        unknown = set(normalized) - set(ALLOWED_MARKETPLACES)
        if unknown:
            raise ValueError(
                "自动建档站点只能是 CA、UK、AU；无效站点："
                + "、".join(sorted(unknown))
            )
        with self.session_factory() as session:
            store = session.get(Store, int(store_id))
            if store is None:
                raise LookupError(f"店铺 {store_id} 不存在")
            seller_id = (store.expected_seller_id or "").strip()
            if not seller_id or not store.identity_confirmed:
                raise RuntimeError("请先检测并人工确认该店铺的预期卖家身份")
            selector = ProfileSelector(str(store.selector_type), str(store.selector_value))
            store_ref = StoreRef(
                id=str(store.id),
                name=store.name,
                selector_type=selector.type,
                selector_value=selector.value,
                expected_seller_id=seller_id,
                # Setup is allowed before the operator enables the store. The
                # identity confirmation above is the setup safety boundary.
                enabled=True,
                identity_confirmed=True,
            )

        async with self._marketplace_setup_guard:
            current_id = self._marketplace_setup_store_probes.get(int(store_id))
            current = self._marketplace_setup_probes.get(current_id or "")
            if current is not None and current.task.done():
                # ``Task.add_done_callback`` is deliberately asynchronous.  A
                # second HTTP request can therefore observe ``task.done()`` a
                # tick before that callback acquires this guard.  Finalize the
                # old snapshot synchronously instead of deleting it while its
                # sites still say PENDING/CHECKING.
                self._finalize_marketplace_setup_probe_locked(current, current.task)
            if current is not None and current.state in {
                "CHECKING",
                "WAITING_AUTH",
            }:
                if current.unified:
                    raise RuntimeError(
                        "该店铺正在执行统一建档，请完成或取消后再使用旧版付款账户建档"
                    )
                return self._marketplace_setup_payload_locked(current)
            if current is not None:
                self._marketplace_setup_probes.pop(current.id, None)
                if self._marketplace_setup_store_probes.get(int(store_id)) == current.id:
                    self._marketplace_setup_store_probes.pop(int(store_id), None)
            identity_id = self._identity_store_probes.get(int(store_id))
            identity_probe = self._identity_probes.get(identity_id or "")
            if identity_probe is not None and not identity_probe.task.done():
                raise RuntimeError("该店铺正在检测卖家身份，请完成或取消后再自动建档")

            probe_id = str(uuid4())
            loop = asyncio.get_running_loop()
            initial_sites = [
                {
                    "code": code,
                    "domain": ALLOWED_MARKETPLACES[code][0],
                    "currency": ALLOWED_MARKETPLACES[code][1],
                    "status": "PENDING",
                    "message": "排队等待检测",
                }
                for code in normalized
            ]
            expires_at = loop.time() + self.identity_auth_timeout_seconds
            task = asyncio.create_task(
                self._run_setup_probe_before_deadline(
                    expires_at=expires_at,
                    operation=lambda: self._run_marketplace_setup_probe(
                        probe_id=probe_id,
                        store_id=int(store_id),
                        selector=selector,
                        store_ref=store_ref,
                        marketplace_codes=normalized,
                    ),
                ),
                name=f"marketplace-setup-probe:{probe_id}",
            )
            probe = _MarketplaceSetupProbe(
                id=probe_id,
                store_id=int(store_id),
                marketplace_codes=normalized,
                seller_id=seller_id,
                task=task,
                created_at=loop.time(),
                expires_at=expires_at,
                marketplaces=initial_sites,
            )
            self._marketplace_setup_probes[probe_id] = probe
            self._marketplace_setup_store_probes[int(store_id)] = probe_id
            task.add_done_callback(
                lambda done, pid=probe_id: asyncio.create_task(
                    self._finish_marketplace_setup_probe(pid, done)
                )
            )
            return self._marketplace_setup_payload_locked(probe)

    async def _run_marketplace_setup_probe(
        self,
        *,
        probe_id: str,
        store_id: int,
        selector: ProfileSelector,
        store_ref: StoreRef,
        marketplace_codes: tuple[str, ...],
    ) -> dict[str, Any]:
        """Confirm each selected site opens as the expected seller.  No clicks.

        Formerly this pressed the dashboard's non-final control on every site
        to read a payout tail.  Nothing gates on that tail any more, so the
        probe stays on the dashboard.
        """

        financial_session = getattr(self.ziniao_controller, "financial_session", None)
        if not callable(financial_session):
            raise RuntimeError("紫鸟控制器缺少资金全局锁，已停止自动建档")
        adapter = AmazonPaymentsPage()
        async with financial_session(selector, store_key=str(store_id)) as handle:
            for code in marketplace_codes:
                domain, currency = ALLOWED_MARKETPLACES[code]
                marketplace = MarketplaceRef(
                    id=f"marketplace-setup-{code.lower()}",
                    code=code,
                    domain=domain,
                    currency=currency,
                    enabled=True,
                )
                run = WorkflowRun(
                    id=f"marketplace-setup:{probe_id}",
                    workflow="amazon_disbursement",
                    mode=RunMode.DRY_RUN,
                    store=store_ref,
                    marketplaces=(marketplace,),
                    requested_by="account-setup",
                )
                await self._set_marketplace_setup_site(
                    probe_id, code, status="CHECKING", message="正在核验域名与卖家身份"
                )
                while True:
                    try:
                        await adapter.preflight(handle.page, run, marketplace)
                        handle.page = adapter.resolved_page_for(handle.page)
                        break
                    except HumanAuthRequired:
                        resolved_page = adapter.resolved_page_for(handle.page)
                        if resolved_page is not handle.page:
                            handle.page = resolved_page
                            continue
                        probe = self._marketplace_setup_probes[probe_id]
                        await self.ziniao_controller.wait_for_auth(
                            handle,
                            self._marketplace_setup_auth_key(probe_id),
                            timeout_seconds=max(
                                0.001,
                                probe.expires_at - asyncio.get_running_loop().time(),
                            ),
                            on_waiting=lambda c=code: self._publish_marketplace_setup_auth_wait(
                                probe_id,
                                c,
                                _ASSISTED_LOGIN_WAIT_MESSAGE,
                            ),
                        )
                        await self._set_marketplace_setup_site(
                            probe_id,
                            code,
                            status="CHECKING",
                            message="正在复用原紫鸟窗口继续核验",
                        )
                    except PreflightRejected as exc:
                        await self._set_marketplace_setup_site(
                            probe_id, code, status="FAILED", message=str(exc)
                        )
                        break
                site = await self._marketplace_setup_site_snapshot(probe_id, code)
                if site.get("status") != "CHECKING":
                    continue
                await self._set_marketplace_setup_site(
                    probe_id,
                    code,
                    status="SUCCEEDED",
                    message="域名与卖家身份核验通过",
                )

        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes[probe_id]
            successes = sum(
                item.get("status") == "SUCCEEDED" for item in (probe.marketplaces or ())
            )
            if successes == len(probe.marketplace_codes):
                status = "SUCCEEDED"
                message = "所选站点均已核验通过"
            elif successes:
                status = "PARTIAL"
                message = "部分站点核验未通过；只读检查仍可使用"
            else:
                status = "FAILED"
                message = "所选站点均未通过核验；只读检查仍可使用"
            return {
                "status": status,
                "probe_id": probe_id,
                "store_id": store_id,
                "seller_id": probe.seller_id,
                "marketplaces": [dict(item) for item in (probe.marketplaces or ())],
                "message": message,
            }

    def _persist_store_setup_identity(
        self,
        *,
        store_id: int,
        seller_id: str,
        selector: ProfileSelector,
        marketplace_code: str,
    ) -> tuple[str, str, bool, bool]:
        """Bind an identity proven by the unified live Ziniao probe.

        The caller invokes this method while it still owns the one
        ``financial_session`` handle used by the unified setup probe.  The
        page adapter has already required the exact allow-listed Seller
        Central host.  Requiring that marketplace proof here as well, plus an
        exact selector re-check against SQLite, prevents a browser result from
        being attributed to a different Ziniao environment.
        """

        normalized = " ".join(str(seller_id or "").split())
        if not normalized or len(normalized) > 120:
            raise RuntimeError("卖家身份检测结果为空或长度异常，结果未保存")
        proof_code = str(marketplace_code or "").strip().upper()
        if proof_code not in ALLOWED_MARKETPLACES:
            raise RuntimeError("卖家身份检测结果没有来自固定域名白名单，结果未保存")
        identity_key = normalized.casefold()
        with self.session_factory() as session:
            store = session.get(Store, int(store_id))
            if (
                store is None
                or str(store.selector_type) != selector.type
                or str(store.selector_value) != selector.value
            ):
                raise RuntimeError("检测期间紫鸟店铺环境已变化，身份结果未保存")
            existing = " ".join(str(store.expected_seller_id or "").split())
            if existing:
                if existing.casefold() != identity_key:
                    return (
                        "NEEDS_REVIEW",
                        existing,
                        bool(store.identity_confirmed),
                        bool(store.enabled),
                    )
                # A record created by an older setup version can contain the
                # correct seller ID while still waiting for a manual checkbox.
                # This new authoritative live proof completes that binding.
                # Once already confirmed, preserve the administrator's enabled
                # switch instead of silently turning a deliberately disabled
                # store back on. Account baselines remain untouched.
                if not store.identity_confirmed:
                    store.identity_confirmed = True
                    store.enabled = True
                    session.commit()
                    return "PROMOTED", existing, True, True
                return "MATCHED", existing, True, bool(store.enabled)

            store.expected_seller_id = normalized
            store.identity_confirmed = True
            store.enabled = True
            session.commit()
            return "CREATED", normalized, True, True

    async def _set_store_setup_identity(
        self, probe_id: str, **changes: Any
    ) -> None:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes.get(probe_id)
            if probe is None or not probe.unified or probe.identity is None:
                raise RuntimeError("店铺统一建档身份记录已释放")
            probe.identity.update(changes)

    async def _store_setup_identity_snapshot(
        self, probe_id: str
    ) -> dict[str, Any]:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes[probe_id]
            return dict(probe.identity or {})

    async def _marketplace_setup_sites_snapshot(
        self, probe_id: str
    ) -> list[dict[str, Any]]:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes[probe_id]
            return [dict(item) for item in (probe.marketplaces or ())]

    async def _publish_store_setup_identity_auth_wait(
        self, probe_id: str, code: str, message: str
    ) -> None:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes.get(probe_id)
            if probe is None or not probe.unified or probe.identity is None:
                raise RuntimeError("店铺统一建档身份记录已释放")
            probe.state = "WAITING_AUTH"
            probe.message = message
            probe.identity.update(
                status="WAITING_AUTH",
                marketplace_code=code,
                message=message,
            )

    async def _set_marketplace_setup_site(
        self, probe_id: str, code: str, **changes: Any
    ) -> None:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes.get(probe_id)
            if probe is None:
                raise RuntimeError("付款账户自动建档记录已释放")
            for site in probe.marketplaces or ():
                if site.get("code") == code:
                    site.update(changes)
                    return
            raise RuntimeError(f"付款账户自动建档不包含站点 {code}")

    async def _marketplace_setup_site_snapshot(
        self, probe_id: str, code: str
    ) -> dict[str, Any]:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes[probe_id]
            return dict(
                next(item for item in (probe.marketplaces or ()) if item.get("code") == code)
            )

    async def _publish_marketplace_setup_auth_wait(
        self, probe_id: str, code: str, message: str
    ) -> None:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes.get(probe_id)
            if probe is None:
                raise RuntimeError("付款账户自动建档记录已释放")
            probe.state = "WAITING_AUTH"
            probe.message = message
            for site in probe.marketplaces or ():
                if site.get("code") == code:
                    site.update(status="WAITING_AUTH", message=message)
                    break

    async def _finish_marketplace_setup_probe(
        self, probe_id: str, task: asyncio.Task[dict[str, Any]]
    ) -> None:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes.get(probe_id)
            if probe is None:
                return
            self._finalize_marketplace_setup_probe_locked(probe, task)

    def _finalize_marketplace_setup_probe_locked(
        self,
        probe: _MarketplaceSetupProbe,
        task: asyncio.Task[dict[str, Any]],
    ) -> None:
        """Atomically publish a task and every site as a terminal snapshot.

        A task becomes done before an asynchronous done callback gets a turn.
        HTTP polling must therefore also be able to call this idempotently
        while ``_marketplace_setup_guard`` is held.  In particular, a closed
        CDP target or a normal no-payment result must never leave an old
        CHECKING/PENDING badge in the public payload.
        """

        if not task.done():
            return
        # Another caller may have already consumed this same task.  Keeping
        # terminal values unchanged makes callback/poller/cancel joins safe.
        if probe.state in _MARKETPLACE_SETUP_TERMINAL_STATES and not any(
            str(site.get("status") or "").upper()
            in _MARKETPLACE_SETUP_ACTIVE_SITE_STATES
            for site in (probe.marketplaces or ())
        ):
            return

        terminal_site_status = "FAILED"
        terminal_site_message = "付款账户自动建档失败"
        try:
            result = task.result()
            if not isinstance(result, dict):
                raise RuntimeError("付款账户自动建档任务返回了无效结果")
            result_status = str(result.get("status") or "FAILED").upper()
            if result_status not in _MARKETPLACE_SETUP_TERMINAL_STATES:
                result_status = "FAILED"
            probe.state = result_status
            probe.message = str(result.get("message") or "自动建档已结束")
            # Copy the result so a caller cannot mutate the retained snapshot.
            probe.result = dict(result)
            terminal_site_status = (
                "UNAVAILABLE" if result_status == "UNAVAILABLE" else "FAILED"
            )
            terminal_site_message = probe.message
        except (asyncio.CancelledError, AuthWaitCancelled):
            probe.result = None
            probe.state = "CANCELLED"
            probe.message = "付款账户自动建档已取消"
            terminal_site_status = "CANCELLED"
            terminal_site_message = probe.message
        except AuthWaitExpired:
            probe.result = None
            probe.state = "FAILED"
            probe.message = "人工验证等待已超时，对应紫鸟店铺窗口已释放"
            terminal_site_message = probe.message
        except Exception as exc:
            probe.result = None
            probe.state = "FAILED"
            probe.message = self._marketplace_setup_failure_message(exc)
            terminal_site_message = probe.message

        for site in probe.marketplaces or ():
            site_status = str(site.get("status") or "").upper()
            if site_status in _MARKETPLACE_SETUP_ACTIVE_SITE_STATES:
                site.update(
                    status=terminal_site_status,
                    message=terminal_site_message,
                )

        if probe.identity is not None and str(
            probe.identity.get("status") or ""
        ).upper() in _MARKETPLACE_SETUP_ACTIVE_SITE_STATES:
            probe.identity.update(
                status=terminal_site_status,
                message=terminal_site_message,
            )

        # Terminal payloads always use the normalized in-memory site list;
        # this also repairs a result assembled before context-manager cleanup.
        if probe.result is not None:
            probe.result.update(
                status=probe.state,
                message=probe.message,
                marketplaces=[dict(item) for item in (probe.marketplaces or ())],
            )
            if probe.identity is not None:
                probe.result["identity"] = dict(probe.identity)

    @staticmethod
    def _marketplace_setup_failure_message(exc: BaseException) -> str:
        """Return a useful operator message without leaking Playwright jargon."""

        detail = str(exc).strip()
        normalized = detail.casefold()
        if (
            "target page, context or browser has been closed" in normalized
            or "target closed" in normalized
            or "browser has been closed" in normalized
        ):
            return "对应紫鸟店铺页面连接已中断；本次检测已结束，请重新发起检测"
        return detail or "付款账户自动建档失败"

    async def get_marketplace_setup_probe(
        self, store_id: int, probe_id: str
    ) -> dict[str, Any]:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes.get(str(probe_id))
            if probe is None or probe.store_id != int(store_id):
                raise LookupError("付款账户自动建档记录不存在或不属于该店铺")
            if probe.task.done():
                self._finalize_marketplace_setup_probe_locked(probe, probe.task)
            return self._marketplace_setup_payload_locked(probe)

    async def get_store_setup_probe(
        self, store_id: int, probe_id: str
    ) -> dict[str, Any]:
        await self._require_unified_store_setup_probe(store_id, probe_id)
        return await self.get_marketplace_setup_probe(store_id, probe_id)

    async def get_active_store_setup_probe(
        self, store_id: int
    ) -> dict[str, Any] | None:
        """The unified setup probe still holding this store, if any.

        The browser is the wrong place to keep the only copy of ``probe_id``:
        reloading the page, reopening the editor or closing the tab all lose it,
        while this registry stays authoritative for the whole window.  Losing it
        left the operator facing a reset that always 409s and a cancel control
        that is never rendered, with nothing on screen naming a way out.  The
        console asks this on every editor open so the running probe — and its
        cancel button — are always found again.
        """

        async with self._marketplace_setup_guard:
            probe_id = self._marketplace_setup_store_probes.get(int(store_id))
            probe = self._marketplace_setup_probes.get(probe_id or "")
            if probe is None or not probe.unified:
                return None
            if probe.task.done():
                # Same settle-then-report as the read-by-id path above: keep the
                # terminal snapshot consistent, but report no live probe, since
                # a finished task no longer blocks reset.
                self._finalize_marketplace_setup_probe_locked(probe, probe.task)
                return None
            return self._marketplace_setup_payload_locked(probe)

    def _marketplace_setup_payload_locked(
        self, probe: _MarketplaceSetupProbe
    ) -> dict[str, Any]:
        if probe.result is not None and probe.state in _MARKETPLACE_SETUP_TERMINAL_STATES:
            return dict(probe.result)
        remaining = max(0, int(probe.expires_at - asyncio.get_running_loop().time()))
        payload = {
            "status": probe.state,
            "probe_id": probe.id,
            "store_id": probe.store_id,
            "seller_id": probe.seller_id,
            "expires_in_seconds": remaining,
            "marketplaces": [dict(item) for item in (probe.marketplaces or ())],
            "message": probe.message,
        }
        if probe.identity is not None:
            payload["identity"] = dict(probe.identity)
        return payload

    async def continue_marketplace_setup_probe(
        self, store_id: int, probe_id: str
    ) -> dict[str, Any]:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes.get(str(probe_id))
            if probe is None or probe.store_id != int(store_id):
                raise LookupError("付款账户自动建档记录不存在或不属于该店铺")
            if probe.task.done():
                self._finalize_marketplace_setup_probe_locked(probe, probe.task)
                return self._marketplace_setup_payload_locked(probe)
            if probe.state == "CHECKING":
                return self._marketplace_setup_payload_locked(probe)
            if probe.state != "WAITING_AUTH":
                raise RuntimeError(f"当前自动建档状态为 {probe.state}，无需继续")
            probe.state = "CHECKING"
            probe.message = "正在使用原紫鸟店铺窗口尝试自动登录并继续检测"
        signal = getattr(self.ziniao_controller, "continue_auth", None)
        if not callable(signal) or not await signal(
            self._marketplace_setup_auth_key(probe_id)
        ):
            async with self._marketplace_setup_guard:
                message = "原紫鸟验证会话已失效，请重新发起自动建档"
                probe.state = "FAILED"
                probe.message = message
                for site in probe.marketplaces or ():
                    if str(site.get("status") or "").upper() in (
                        _MARKETPLACE_SETUP_ACTIVE_SITE_STATES
                    ):
                        site.update(status="FAILED", message=message)
                task = probe.task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return await self.get_marketplace_setup_probe(store_id, probe_id)
        return await self.get_marketplace_setup_probe(store_id, probe_id)

    async def continue_store_setup_probe(
        self, store_id: int, probe_id: str
    ) -> dict[str, Any]:
        await self._require_unified_store_setup_probe(store_id, probe_id)
        return await self.continue_marketplace_setup_probe(store_id, probe_id)

    async def cancel_marketplace_setup_probe(
        self, store_id: int, probe_id: str
    ) -> dict[str, Any]:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes.get(str(probe_id))
            if probe is None or probe.store_id != int(store_id):
                raise LookupError("付款账户自动建档记录不存在或不属于该店铺")
            if probe.task.done():
                self._finalize_marketplace_setup_probe_locked(probe, probe.task)
                return self._marketplace_setup_payload_locked(probe)
        signal = getattr(self.ziniao_controller, "cancel_auth", None)
        signalled = bool(
            callable(signal)
            and await signal(self._marketplace_setup_auth_key(probe_id))
        )
        if not signalled:
            probe.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(probe.task), timeout=15.0)
        except (AuthWaitCancelled, asyncio.CancelledError, RuntimeError):
            pass
        except TimeoutError:
            probe.task.cancel()
            await asyncio.gather(probe.task, return_exceptions=True)
        await asyncio.sleep(0)
        return await self.get_marketplace_setup_probe(store_id, probe_id)

    async def cancel_store_setup_probe(
        self, store_id: int, probe_id: str
    ) -> dict[str, Any]:
        await self._require_unified_store_setup_probe(store_id, probe_id)
        return await self.cancel_marketplace_setup_probe(store_id, probe_id)

    async def _require_unified_store_setup_probe(
        self, store_id: int, probe_id: str
    ) -> None:
        async with self._marketplace_setup_guard:
            probe = self._marketplace_setup_probes.get(str(probe_id))
            if (
                probe is None
                or probe.store_id != int(store_id)
                or not probe.unified
            ):
                raise LookupError("店铺统一建档记录不存在或不属于该店铺")

    @staticmethod
    def _marketplace_setup_auth_key(probe_id: str) -> str:
        return f"marketplace-setup-probe:{probe_id}"

    async def enqueue_run(self, run_id: str) -> None:
        await self._enqueue(run_id, action="START")

    async def enqueue_runs(self, run_ids: Sequence[str]) -> None:
        """Enqueue one same-occurrence batch and wake the worker once.

        The scheduler creates one independent run per store, but a batch is a
        single ordering unit.  ``DurableRunQueue.enqueue_many`` writes all
        entries in one transaction with a shared immutable enqueue timestamp;
        starting/waking the worker only after that commit prevents it from
        claiming child 1 while child 2 is still being persisted.
        """

        normalized = list(dict.fromkeys(str(value) for value in run_ids))
        if not normalized:
            return
        if self._queue is None or self._worker is None:
            # Compatibility path for isolated in-memory tests.
            for run_id in normalized:
                await self._spawn(run_id, lambda run: self.engine.start(run))
            return
        self._queue.enqueue_many(normalized, action="START")
        await self._worker.start()
        self._worker.wake()

    async def approve_run(
        self,
        run_id: str,
        approval_id: str | None = None,
        *,
        actor: str = "admin",
    ) -> None:
        if self._queue is not None:
            if not approval_id:
                raise RuntimeError("没有可处理的审批")
            self._queue.approve_and_enqueue(run_id, approval_id, actor=actor)
            assert self._worker is not None
            await self._worker.start()
            self._worker.wake()
            return
        # Compatibility for isolated tests without a database queue.
        await self._spawn(run_id, lambda value: self.engine.approve(value, actor=actor))

    async def continue_auth(self, run_id: str) -> None:
        # A live background task holds page/store/funds locks inside
        # controller.wait_for_auth; signal it directly.  After a process restart
        # there is no lease, so enqueue the engine's guarded recovery path.
        signal = getattr(self.ziniao_controller, "continue_auth", None)
        if callable(signal) and await signal(run_id):
            return
        if self._queue is not None:
            # After restart the original browser lease is gone. Convert the
            # human acknowledgement into durable QUEUED before the worker
            # attempts the full guarded check again.
            status = await self.engine.repository.get_run_status(run_id)
            if status in (RunStatus.WAITING_AUTH, RunStatus.NEEDS_HUMAN_AUTH):
                if await self.engine.repository.list_operations(run_id):
                    changed = await self.engine.repository.set_run_status(
                        run_id,
                        RunStatus.UNCERTAIN_FINANCIAL,
                        allowed_from=(status,),
                    )
                    if not changed:
                        raise RuntimeError("任务状态已变化，请刷新后重试")
                    await self._enqueue(run_id, action="RECONCILE", priority=PRIORITY_RECOVERY)
                    return
                changed = await self.engine.repository.set_run_status(
                    run_id,
                    RunStatus.QUEUED,
                    allowed_from=(status,),
                )
                if not changed:
                    raise RuntimeError("任务状态已变化，请刷新后重试")
            await self._enqueue(
                run_id, action="START", priority=PRIORITY_HUMAN
            )
        else:
            await self._spawn(run_id, lambda run: self.engine.continue_auth(run))

    async def cancel_run(self, run_id: str) -> None:
        if self.session_factory is not None:
            with self.session_factory() as session:
                active_guard = session.scalar(
                    select(OperationGuard.id)
                    .where(
                        OperationGuard.run_id == run_id,
                        OperationGuard.state.in_(
                            ("ARMED", "SUBMITTED", "UNCERTAIN")
                        ),
                    )
                        .limit(1)
                    )
                # A durable RECONCILE item is itself a financial safety marker.
                # It may survive a restart after all guard rows have reached a
                # terminal state, so checking only active guard states above is
                # insufficient. Keep both reads in this managed session before
                # any cancellation signal or ``cancel_ready`` call; otherwise
                # cancellation could erase the only recovery action and strand
                # the run in a reconcile state.
                recovery_entry = session.scalar(
                    select(RunQueueEntry.action)
                    .where(
                        RunQueueEntry.run_id == run_id,
                        RunQueueEntry.state.in_(("READY", "CLAIMED")),
                        RunQueueEntry.action == "RECONCILE",
                    )
                    .limit(1)
                )
            if active_guard is not None:
                raise RuntimeError(
                    "任务已进入资金保护阶段，只能执行付款结果回读，不能取消"
                )
            if recovery_entry is not None:
                raise RuntimeError(
                    "任务已进入资金回读队列，只能先完成资金结果回读，不能取消"
                )
        signal = getattr(self.ziniao_controller, "cancel_auth", None)
        if callable(signal) and await signal(run_id):
            return
        if self._queue is not None and self._queue.cancel_ready(run_id):
            run = await self.run_loader(run_id)
            cancelled = await self.engine.cancel(run)
            await self._raise_if_cancel_was_unsafe(run_id, cancelled)
            if self._worker is not None:
                self._worker.wake()
            return
        run = await self.run_loader(run_id)
        cancelled = await self.engine.cancel(run)
        await self._raise_if_cancel_was_unsafe(run_id, cancelled)

    async def _raise_if_cancel_was_unsafe(
        self, run_id: str, cancelled: Any
    ) -> None:
        """Reject a cancellation that did not acquire a safe state boundary.

        ``CLAIMED`` only means the durable worker owns the queue row; it does
        not stop the already-running coroutine.  Writing CANCELLED while that
        coroutine is between its last page check and ``arm_operation`` would
        therefore be cosmetic rather than cancellation.  The engine now
        returns ``False`` at that boundary and this service surfaces a 409
        instead of claiming that the funds task stopped.

        ``is False`` intentionally preserves compatibility with tiny injected
        test doubles whose legacy ``cancel`` method returns ``None``.
        """

        if cancelled is not False:
            return
        status = await self.engine.repository.get_run_status(run_id)
        if status in {
            RunStatus.SUCCEEDED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.SKIPPED,
        }:
            return
        if await self.engine.repository.list_operations(run_id):
            raise RuntimeError(
                "任务已进入资金保护阶段，只能执行付款结果回读，不能取消"
            )
        if status in {RunStatus.RUNNING, RunStatus.RECONCILING}:
            raise RuntimeError(
                "任务正在执行，当前尚未到可安全取消的等待点；本次未写入取消状态"
            )
        raise RuntimeError("任务状态已变化，当前不能取消，请刷新后重试")

    async def reconcile_run(self, run_id: str) -> None:
        if self._queue is not None:
            changed = self._queue.replace_ready_action(
                run_id, action="RECONCILE", priority=PRIORITY_RECOVERY
            )
            if not changed:
                await self._enqueue(
                    run_id, action="RECONCILE", priority=PRIORITY_RECOVERY
                )
            elif self._worker is not None:
                await self._worker.start()
                self._worker.wake()
        else:
            await self._spawn(run_id, lambda run: self.engine.reconcile(run))

    async def _enqueue(
        self,
        run_id: str,
        *,
        action: str,
        priority: int | None = None,
    ) -> None:
        if self._queue is None or self._worker is None:
            actions = {
                "START": lambda run: self.engine.start(run),
                "APPROVE": lambda run: self.engine.approve(run),
                "CONTINUE_AUTH": lambda run: self.engine.continue_auth(run),
                "RECONCILE": lambda run: self.engine.reconcile(run),
            }
            await self._spawn(run_id, actions[action])
            return
        self._queue.enqueue(run_id, action=action, priority=priority)
        await self._worker.start()
        self._worker.wake()

    async def _execute_queue_entry(self, entry: Any) -> None:
        durable_status: str | None = None
        has_active_guard = False
        has_any_guard = False
        if self.session_factory is not None:
            with self.session_factory() as session:
                durable_status = session.scalar(
                    select(Run.status).where(Run.id == entry.run_id)
                )
                guard_states = tuple(
                    session.scalars(
                        select(OperationGuard.state).where(
                            OperationGuard.run_id == entry.run_id
                        )
                    )
                )
                has_any_guard = bool(guard_states)
                has_active_guard = any(
                    state in {"ARMED", "SUBMITTED", "UNCERTAIN"}
                    for state in guard_states
                )
        # The guard, not a stale queue action, decides what is safe. An
        # ARMED/SUBMITTED/UNCERTAIN run can only enter the read-back path.
        effective_action = "RECONCILE" if has_active_guard else entry.action
        try:
            recovery_loader = getattr(
                self.run_loader, "load_financial_recovery", None
            )
            if (
                (has_active_guard or (entry.action == "RECONCILE" and has_any_guard))
                and callable(recovery_loader)
            ):
                run = await recovery_loader(entry.run_id)
            else:
                run = await self.run_loader(entry.run_id)
        except (LookupError, TypeError, ValueError, WorkflowNotRegistered) as exc:
            self._mark_unloadable_queue_run(entry.run_id, exc)
            return
        # A user can cancel between claim and actual execution. Never ask the
        # engine to revive a terminal run.
        if self.session_factory is not None:
            with self.session_factory() as session:
                durable_status = session.scalar(
                    select(Run.status).where(Run.id == entry.run_id)
                )
            if durable_status in {
                "SUCCEEDED",
                "PARTIAL",
                "FAILED",
                "CANCELLED",
                "SKIPPED",
                "NEEDS_HUMAN_AUTH",
                "UNCERTAIN_FINANCIAL",
                "RECONCILING",
            }:
                if effective_action != "RECONCILE":
                    return
        if effective_action == "START":
            await self.engine.start(run)
        elif effective_action == "APPROVE":
            execute = getattr(self.engine, "execute_approved", None)
            if callable(execute):
                await execute(run)
            else:
                await self.engine.approve(run)
        elif effective_action == "CONTINUE_AUTH":
            await self.engine.continue_auth(run)
        elif effective_action == "RECONCILE":
            await self.engine.reconcile(run)
        else:  # database check constraint is defence in depth
            raise RuntimeError(f"unknown queue action: {entry.action}")

    def _mark_unloadable_queue_run(self, run_id: str, exc: Exception) -> None:
        """Move a rejected immutable snapshot to an auditable terminal state."""

        if self.session_factory is None:
            return
        with self.session_factory() as session:
            row = session.get(Run, run_id)
            if row is None:
                return
            previous = row.status
            # A guarded recovery loader already wrote UNCERTAIN_FINANCIAL; it
            # must never be weakened to an ordinary FAILED/SKIPPED state.
            guarded = session.scalar(
                select(OperationGuard.id)
                .where(
                    OperationGuard.run_id == run_id,
                    OperationGuard.state.in_(("ARMED", "SUBMITTED", "UNCERTAIN")),
                )
                .limit(1)
            )
            if guarded is not None:
                target = "UNCERTAIN_FINANCIAL"
                message = "资金任务快照无法加载，已保持资金结果不明确，请人工核查"
            else:
                target = "SKIPPED" if row.trigger == "schedule" else "FAILED"
                message = f"任务配置无法加载：{type(exc).__name__}"
            row.status = target
            row.error = message
            row.finished_at = database_utc_now()
            from ziniao_automation.models import RunEvent

            session.add(
                RunEvent(
                    run_id=run_id,
                    event_type="QUEUE_SNAPSHOT_REJECTED",
                    from_status=previous,
                    to_status=target,
                    message=message,
                )
            )
            session.commit()

    async def recover_startup(self) -> None:
        startup_plan_loader = getattr(self.run_loader, "startup_recovery_plan", None)
        if callable(startup_plan_loader):
            plan = await startup_plan_loader()
        elif self.recovery_loader is not None:
            plan = StartupRecoveryPlan(financial=tuple(await self.recovery_loader()))
        else:
            return

        if self._queue is not None:
            financial_ids = {run.id for run in plan.financial}
            guarded_ids = set(plan.financial_guarded_ids) or financial_ids
            # Every guarded run is restored as RECONCILE first.  Invalid
            # legacy snapshots are then cancelled below, so none can fall
            # through to an ordinary START action while healthy peers recover.
            reconcile_ids = guarded_ids | set(plan.reconciling_ids)
            self._queue.restore_claimed(reconcile_ids)
            for run in plan.financial:
                self._queue.ensure_financial_recovery(run.id)
            for run_id in plan.reconciling_ids:
                self._queue.ensure_financial_recovery(run_id)
            for run_id in plan.financial_failed_ids:
                self._queue.cancel_ready(run_id)
            deliveries = getattr(self._worker, "notifications", None)
            notify_uncertain = getattr(
                deliveries, "notify_uncertain_financial", None
            )
            if callable(notify_uncertain):
                for run_id in plan.financial_failed_ids:
                    try:
                        await notify_uncertain(run_id)
                    except Exception:
                        logger.exception(
                            "financial_recovery_failure_notice_failed run_id=%s",
                            run_id[:8],
                        )
        else:
            # Isolated non-database consumers retain the legacy serial path.
            for run in plan.financial:
                try:
                    await self.engine.reconcile(run)
                except Exception:
                    logger.exception("Startup reconciliation failed for run %s", run.id)

        # A plain RUNNING record means the process died during a read-only,
        # pre-submit step.  CAS it back to QUEUED before spawning so the engine
        # can claim QUEUED -> RUNNING normally.  A failed CAS means another
        # actor already changed it, so it is not enqueued.
        recovered_ids: set[str] = (
            set(plan.financial_guarded_ids)
            or {run.id for run in plan.financial}
        )
        recovered_ids.update(plan.reconciling_ids)
        for run_id in plan.running_ids:
            if run_id in recovered_ids:
                continue
            changed = await self.engine.repository.set_run_status(
                run_id,
                RunStatus.QUEUED,
                allowed_from=(RunStatus.RUNNING,),
                error=None,
            )
            if changed:
                recovered_ids.add(run_id)
                if self._queue is not None:
                    self._queue.enqueue(run_id, action="START")
                else:
                    await self.enqueue_run(run_id)

        # QUEUED is already durable work awaiting the process-local queue.
        # _spawn also prevents a duplicate task per run ID.
        for run_id in plan.queued_ids:
            if run_id not in recovered_ids:
                recovered_ids.add(run_id)
                if self._queue is not None:
                    self._queue.enqueue(run_id, action="START")
                else:
                    await self.enqueue_run(run_id)

        # The old browser/auth lease disappeared with the process.  Preserve a
        # human-visible state rather than silently rerunning.  The existing
        # continue-auth endpoint accepts NEEDS_HUMAN_AUTH and restarts all
        # checks from QUEUED after the user explicitly confirms.
        for run_id in plan.waiting_auth_ids:
            if run_id in recovered_ids:
                continue
            marker = getattr(self.run_loader, "mark_waiting_auth_after_restart", None)
            if callable(marker):
                changed = await marker(run_id)
            else:
                run = await self.run_loader(run_id)
                changed = await self.engine.expire_auth(run)
            if changed:
                recovered_ids.add(run_id)

        if self._worker is not None:
            await self._worker.start()
            self._worker.wake()

    async def wait_idle(self) -> None:
        if self._worker is not None:
            await self._worker.wait_idle()
            return
        while True:
            async with self._guard:
                tasks = tuple(self._tasks.values())
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown_worker(self) -> None:
        if self._worker is not None:
            await self._worker.close()

    async def _spawn(
        self,
        run_id: str,
        action: Callable[[WorkflowRun], Awaitable[Any]],
    ) -> None:
        async with self._guard:
            current = self._tasks.get(run_id)
            if current is not None and not current.done():
                return

            async def runner() -> None:
                try:
                    run = await self.run_loader(run_id)
                    await action(run)
                except Exception:
                    logger.exception("Automation run failed: %s", run_id)
                finally:
                    async with self._guard:
                        if self._tasks.get(run_id) is asyncio.current_task():
                            self._tasks.pop(run_id, None)

            self._tasks[run_id] = asyncio.create_task(
                runner(), name=f"workflow-{run_id}"
            )
