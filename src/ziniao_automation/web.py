"""FastAPI routes for the loopback-only operations console."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import inspect
import asyncio
from functools import wraps
import logging
from pathlib import Path
import re
import socket
from typing import Any, AsyncIterator, Protocol

from ziniao_automation.setup_repository import SetupRepository, SetupConflict
from ziniao_automation.schemas import StoreSetupInput, StoreSetupBatchPreview

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import LocalAccess, LocalOperator, is_loopback_client, is_same_origin_request
from .config import Settings
from .db import backup_sqlite_database, create_sqlite_engine, database_revision, init_database, make_session_factory
from .models import (
    ApprovalRequest,
    Evidence,
    FeedbackReview,
    InventoryGpsrItem,
    InventoryGpsrRequirement,
    OperationGuard,
    Run,
    RunQueueEntry,
    SiteRun,
    Store,
    StoreHistory,
    SystemSetting,
    ZiniaoAccount,
)
from .presentation_time import format_display_datetime, to_display_datetime
from .notifications.database import read_inventory_gpsr_counts
from .repositories import (
    ACTIVE_RUN_STATUSES,
    ConflictError,
    DashboardRepository,
    NotFoundError,
    ScheduleRepository,
    StoreRepository,
    WorkflowRepository,
)
from .schedule_batch import BatchScheduleService, BatchScheduleValidationError
from .scheduler import ScheduleProjectionReport
from .schemas import (
    BatchScheduleCreateInput,
    BatchSchedulePreviewInput,
    FeedbackDecisionInput,
    FeishuSettingsInput,
    MarketplaceSetupInput,
    RunCreate,
    WebdriverStart,
    RunView,
    ScheduleCreate,
    ScheduleDeleteView,
    ScheduleMutationView,
    SchedulePatch,
    ScheduleView,
    StoreCreate,
    StorePatch,
    StoreSetupResetView,
    StoreSetupBatchResetInput,
    StoreView,
    ZiniaoSettingsInput,
)
from .version import __version__, build_commit
from .workflows import WorkflowRegistry
from .workflows.amazon_disbursement import build_amazon_disbursement_definition
from .workflows.amazon_feedback import build_amazon_feedback_definition
from .workflows.amazon_feedback.classifier import resolve_codex_endpoint
from .workflows.amazon_feedback.config import (
    AMAZON_REVIEW_CRITERIA,
    CATEGORY_LABELS,
    REASON_CATALOG,
    is_known_reason,
    reason_label,
)
from .workflows.amazon_feedback.review_store import (
    NEEDS_HUMAN as FEEDBACK_NEEDS_HUMAN,
    PENDING as FEEDBACK_PENDING,
    TERMINAL_STATES as FEEDBACK_TERMINAL_STATES,
)
from .workflows.amazon_inventory_gpsr.store import can_resume_inventory_run
from .workflows.errors import WorkflowNotRegistered
from .workflows.engine import run_status_from_facts
from .workflows.types import UNSETTLED_GUARD_STATES
from .ziniao.credentials import (
    DEFAULT_FEISHU_TARGET,
    DEFAULT_ZINIAO_TARGET,
    CredentialStoreError,
    credential_exists,
    credential_matches,
    read_generic_credential,
    write_generic_credential,
)
from .ziniao.errors import ZiniaoCredentialError

logger = logging.getLogger(__name__)

_ZINIAO_CREDENTIAL_ERROR_CODE = "ZINIAO_CREDENTIALS_INVALID"


def _release_before_automation(db: Session) -> None:
    """End the request's transaction before delegating to the run service.

    The automation service writes through its own connection with
    ``BEGIN IMMEDIATE``.  If the request session still holds SQLite's write
    lock, that second writer blocks; because the blocking call is synchronous
    sqlite on the event loop, the request can never reach its own commit and
    both sides fail after ``busy_timeout`` with "database is locked".  Ending
    the request transaction first makes the deadlock unrepresentable, and is
    safe here because everything read so far is either finished or re-read by
    the service under its own guards.
    """

    db.commit()


SESSION_COOKIE = "ziniao_local_session"
CSRF_COOKIE = "ziniao_csrf"


class AutomationService(Protocol):
    async def sync_ziniao(self) -> dict[str, Any]: ...
    async def detect_store_identity(self, store_id: int) -> dict[str, Any]: ...
    async def get_identity_probe(self, store_id: int, probe_id: str) -> dict[str, Any]: ...
    async def continue_identity_probe(self, store_id: int, probe_id: str) -> dict[str, Any]: ...
    async def cancel_identity_probe(self, store_id: int, probe_id: str) -> dict[str, Any]: ...
    async def detect_marketplace_setup(self, store_id: int, marketplace_codes: list[str] | None = None) -> dict[str, Any]: ...
    async def get_marketplace_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]: ...
    async def continue_marketplace_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]: ...
    async def cancel_marketplace_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]: ...
    async def detect_store_setup(self, store_id: int, marketplace_codes: list[str] | None = None) -> dict[str, Any]: ...
    async def get_store_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]: ...
    async def continue_store_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]: ...
    async def cancel_store_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]: ...
    async def has_active_store_setup(self, store_id: int) -> bool: ...
    async def enqueue_run(self, run_id: str) -> None: ...
    async def approve_run(self, run_id: str, approval_id: str | None = None, *, actor: str = "admin") -> None: ...
    async def continue_auth(self, run_id: str) -> None: ...
    async def resume_inventory_run(self, run_id: str) -> None: ...
    async def cancel_run(self, run_id: str) -> None: ...


def _interval_label(minutes: Any) -> str:
    """Render a schedule period the way an operator would say it.

    Whole hours read as hours because that is how the 24-hour cap is discussed;
    anything else keeps its minutes rather than being rounded into a lie.
    """

    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return "—"
    if total < 1:
        return "—"
    hours, remainder = divmod(total, 60)
    if hours and not remainder:
        return f"每 {hours} 小时"
    if hours:
        return f"每 {hours} 小时 {remainder} 分"
    return f"每 {remainder} 分钟"


class UnconfiguredAutomationService:
    async def sync_ziniao(self) -> dict[str, Any]:
        raise RuntimeError("紫鸟运行服务尚未配置")

    async def detect_store_identity(self, store_id: int) -> dict[str, Any]:
        del store_id
        raise RuntimeError("店铺身份检测服务尚未配置")

    async def get_identity_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        del store_id, probe_id
        raise RuntimeError("店铺身份检测服务尚未配置")

    async def continue_identity_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        del store_id, probe_id
        raise RuntimeError("店铺身份检测服务尚未配置")

    async def cancel_identity_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        del store_id, probe_id
        raise RuntimeError("店铺身份检测服务尚未配置")

    async def detect_marketplace_setup(self, store_id: int, marketplace_codes: list[str] | None = None) -> dict[str, Any]:
        del store_id, marketplace_codes
        raise RuntimeError("付款账户自动建档服务尚未配置")

    async def get_marketplace_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        del store_id, probe_id
        raise RuntimeError("付款账户自动建档服务尚未配置")

    async def continue_marketplace_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        del store_id, probe_id
        raise RuntimeError("付款账户自动建档服务尚未配置")

    async def cancel_marketplace_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        del store_id, probe_id
        raise RuntimeError("付款账户自动建档服务尚未配置")

    async def detect_store_setup(self, store_id: int, marketplace_codes: list[str] | None = None) -> dict[str, Any]:
        del store_id, marketplace_codes
        raise RuntimeError("店铺统一自动建档服务尚未配置")

    async def get_store_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        del store_id, probe_id
        raise RuntimeError("店铺统一自动建档服务尚未配置")

    async def continue_store_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        del store_id, probe_id
        raise RuntimeError("店铺统一自动建档服务尚未配置")

    async def cancel_store_setup_probe(self, store_id: int, probe_id: str) -> dict[str, Any]:
        del store_id, probe_id
        raise RuntimeError("店铺统一自动建档服务尚未配置")

    async def has_active_store_setup(self, store_id: int) -> bool:
        del store_id
        return False

    async def get_active_store_setup_probe(self, store_id: int) -> dict[str, Any] | None:
        del store_id
        return None

    async def enqueue_run(self, run_id: str) -> None:
        raise RuntimeError("自动化运行服务尚未配置")

    async def approve_run(self, run_id: str, approval_id: str | None = None, *, actor: str = "admin") -> None:
        raise RuntimeError("自动化运行服务尚未配置")

    async def continue_auth(self, run_id: str) -> None:
        raise RuntimeError("自动化运行服务尚未配置")

    async def resume_inventory_run(self, run_id: str) -> None:
        raise RuntimeError("自动化运行服务尚未配置")

    async def cancel_run(self, run_id: str) -> None:
        raise RuntimeError("自动化运行服务尚未配置")


def _resolved_classifier_endpoint() -> dict[str, Any] | None:
    """Non-secret description of where reason judgement is sent, or None."""

    try:
        endpoint = resolve_codex_endpoint()
    except Exception:  # noqa: BLE001 - diagnostics must render regardless
        return None
    return endpoint.describe() if endpoint is not None else None


STATUS_LABELS = {
    "QUEUED": "排队中", "RUNNING": "执行中", "WAITING_APPROVAL": "待审核",
    "WAITING_AUTH": "等待人工验证", "RECONCILING": "资金收口中",
    "PAUSED": "已暂停，等待页面恢复",
    "SUCCEEDED": "已完成", "PARTIAL": "部分完成", "FAILED": "失败", "CANCELLED": "已取消",
    "SKIPPED": "已跳过", "NEEDS_HUMAN_AUTH": "需要人工验证",
    # Site-level states that had no label and rendered as raw English.
    "PREFLIGHT": "读取中", "PLANNED": "已列入计划", "DRY_RUN_COMPLETE": "只读检查完成",
    "UNCERTAIN_FINANCIAL": "资金结果待人工核对", "PENDING": "待处理",
    "APPROVED": "已批准", "CONFIRMED": "已确认", "ARMED": "已锁定",
    "SUBMITTED": "已提交", "UNCERTAIN": "待人工核对", "INVALIDATED": "已失效",
    "EXPIRED": "已过期",
}

# Feedback entries need their own words.  Sharing STATUS_LABELS would render
# UNCERTAIN as "待人工核对", which is about a payout nobody can vouch for.
FEEDBACK_STATE_LABELS = {
    "PENDING": "待提交",
    "NEEDS_HUMAN": "待人工",
    # Amazon withdraws the action once a review has been requested, so its
    # absence means the request already exists — not that we chose to skip.
    "ALREADY_REQUESTED": "此前已请求",
    # Amazon struck it out itself; nobody had to ask.
    "AMAZON_REMOVED": "亚马逊已剔除",
    "SUBMITTED": "已提交",
    "UNCERTAIN": "未确认",
    "FAILED": "失败",
}

INVENTORY_STATE_LABELS = {
    "PENDING": "尚未检查",
    "ELIGIBLE": "已核验可处理",
    "NO_VIOLATION_ENTRY": "无违规入口",
    "NO_ATTESTATION": "无安全证明",
    "ALREADY_SUBMITTED": "此前审核中",
    "MISSING": "清单商品未找到",
    "FAILED": "处理失败",
    "SUBMITTING": "已有提交意图，仅回读",
    "CONFIRMED": "本轮提交确认（审核中）",
    "UNCERTAIN": "提交结果不明，仅回读",
    "BLOCKED": "历史记录待核对",
}

QUEUE_STATE_LABELS = {
    "READY": "排队中",
    "CLAIMED": "执行中",
    "DONE": "队列动作已完成",
    "CANCELLED": "队列动作已取消",
}

QUEUE_ACTION_LABELS = {
    "START": "开始任务",
    "APPROVE": "批准后继续",
    "CONTINUE_AUTH": "验证后继续",
    "RECONCILE": "资金收口",
}


def _attach_queue_projection(db: Session, runs: list[Run]) -> None:
    """Attach read-only queue details used by RunView and the HTML console.

    These are transient attributes: SQLite remains the source of truth and no
    display-only value is written back to ``runs``.  READY positions use the
    exact durable-worker FIFO order, including higher-priority recovery and
    administrator actions ahead of scheduled work.
    """

    if not runs:
        return

    run_ids = [run.id for run in runs]
    ready_entries = list(
        db.scalars(
            select(RunQueueEntry)
            .where(RunQueueEntry.state == "READY")
            .order_by(
                RunQueueEntry.priority.asc(),
                func.coalesce(
                    RunQueueEntry.scheduled_for_at, RunQueueEntry.enqueued_at
                ).asc(),
                RunQueueEntry.business_priority.asc(),
                RunQueueEntry.enqueued_at.asc(),
                RunQueueEntry.batch_scope_id.asc(),
                RunQueueEntry.target_order.asc(),
                RunQueueEntry.id.asc(),
            )
        )
    )
    ready_positions = {entry.id: index for index, entry in enumerate(ready_entries, 1)}

    active_entries = list(
        db.scalars(
            select(RunQueueEntry)
            .where(
                RunQueueEntry.run_id.in_(run_ids),
                RunQueueEntry.state.in_(("READY", "CLAIMED")),
            )
            .order_by(RunQueueEntry.id.desc())
        )
    )
    selected = {entry.run_id: entry for entry in active_entries}

    missing_ids = [run_id for run_id in run_ids if run_id not in selected]
    if missing_ids:
        historical_entries = list(
            db.scalars(
                select(RunQueueEntry)
                .where(RunQueueEntry.run_id.in_(missing_ids))
                .order_by(RunQueueEntry.id.desc())
            )
        )
        for entry in historical_entries:
            selected.setdefault(entry.run_id, entry)

    for run in runs:
        entry = selected.get(run.id)
        run.queue_state = entry.state if entry else None
        run.queue_position = (
            ready_positions.get(entry.id) if entry and entry.state == "READY" else None
        )
        run.queue_action = entry.action if entry else None
        # Scheduled batch entries reuse ``enqueued_at`` as an immutable group
        # ordering anchor.  ``created_at`` remains the real insertion time the
        # operator expects to see in run history.
        run.queued_at = entry.created_at if entry else None


def create_app(
    settings: Settings | None = None,
    *,
    automation_service: AutomationService | None = None,
    runtime_factory: Any | None = None,
    workflow_registry: WorkflowRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = create_sqlite_engine(settings)
    sessions = make_session_factory(engine)
    package_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(package_dir / "templates"))
    templates.env.filters["status_label"] = lambda value: STATUS_LABELS.get(str(value), str(value))
    templates.env.filters["feedback_state_label"] = lambda value: FEEDBACK_STATE_LABELS.get(str(value), str(value))
    templates.env.filters["queue_state_label"] = lambda value: QUEUE_STATE_LABELS.get(str(value), str(value))
    templates.env.filters["queue_action_label"] = lambda value: QUEUE_ACTION_LABELS.get(str(value), str(value))
    templates.env.filters["money"] = lambda value: "—" if value is None else f"{value:,.2f}"
    templates.env.filters["interval_label"] = _interval_label
    # Datetimes stay UTC in SQLite and APIs.  Jinja alone converts them to the
    # operator timezone so changing presentation never changes queue ordering
    # or financial audit timestamps.
    templates.env.filters["local_datetime"] = format_display_datetime
    templates.env.filters["local_time"] = to_display_datetime
    templates.env.globals["now"] = lambda: to_display_datetime(datetime.now(timezone.utc))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.create_schema_on_start:
            init_database(engine, backup_dir=settings.backup_dir)
        SetupRepository(sessions).recover()
        runtime = None
        try:
            try:
                if runtime_factory is not None:
                    runtime = runtime_factory(settings, sessions)
                    app.state.runtime = runtime
                    app.state.automation_service = runtime.automation_service
                    app.state.schedule_manager = runtime.schedule_manager
                    runtime_registry = getattr(runtime, "workflow_registry", None)
                    if runtime_registry is not None:
                        app.state.workflow_registry = runtime_registry
                    await runtime.start()
            except BaseException:
                # ⚠️ 启动失败时 uvicorn 只会打一句「Application startup failed.
                # Exiting.」,而 runner.last_error 取的是最后一条 ERROR ——
                # 于是启动器和 Start.bat 对**每一种**启动失败都显示那一句,真正的
                # 原因(端口被占、库锁、凭据引用失效……)一个字都看不到
                # (2026-09-26 自审)。这里先把它记下来,logger.exception 会经
                # RedactionFilter 产出 exception_type/message,脱敏照旧。
                logger.exception("service_startup_failed")
                raise
            yield
        finally:
            if runtime is not None:
                await runtime.close()
            engine.dispose()

    app = FastAPI(
        title="紫鸟多店铺自动化管理器",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.automation_service = automation_service or UnconfiguredAutomationService()
    app.state.schedule_manager = None
    app.state.workflow_registry = workflow_registry or WorkflowRegistry(
        (build_amazon_disbursement_definition(),)
    )
    app.state.templates = templates
    local_access = LocalAccess(session_hours=settings.session_hours)
    app.state.local_access = local_access
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"] + (["testserver"] if settings.testing else []),
    )
    app.mount("/static", StaticFiles(directory=str(package_dir / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        peer = request.client.host if request.client else None
        if not is_loopback_client(peer, testing=settings.testing):
            response = JSONResponse({"detail": "管理后台仅允许本机访问"}, status_code=403)
        elif not is_same_origin_request(
            request.headers.get("origin"), request.headers.get("sec-fetch-site"), str(request.url)
        ):
            response = JSONResponse({"detail": "请从本机管理后台页面操作"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_session(request: Request) -> AsyncIterator[Session]:
        factory: sessionmaker[Session] = request.app.state.sessions
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def current_operator() -> LocalOperator:
        # All requests already passed the peer/Host/origin middleware.
        return LocalOperator()

    def verified_operator(
        request: Request,
        x_csrf_token: str | None = Header(None),
    ) -> LocalOperator:
        if not local_access.validate_csrf(request.cookies.get(SESSION_COOKIE), x_csrf_token):
            raise HTTPException(status_code=403, detail="页面校验已失效，请刷新后重试")
        return LocalOperator()

    def render(
        request: Request,
        template: str,
        context: dict[str, Any],
        *,
        db: Session,
        page_title: str,
    ) -> Response:
        common = {
            "request": request,
            "page_title": page_title,
            "active_path": request.url.path,
            **context,
        }
        response = templates.TemplateResponse(request=request, name=template, context=common)
        token, csrf = local_access.page_tokens(request.cookies.get(SESSION_COOKIE))
        _set_session_cookies(response, token, csrf, settings)
        if "ziniao_session" in request.cookies:
            response.delete_cookie("ziniao_session", path="/")
        return response

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def conflict_handler(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(OperationalError)
    async def database_busy_handler(_: Request, exc: OperationalError) -> JSONResponse:
        """Turn SQLite contention into an actionable answer, not a bare 500.

        A run that is still holding the browser can keep the write reservation
        long enough to exhaust ``busy_timeout``.  The request simply did not
        happen — nothing was approved, cancelled or submitted — so the operator
        needs to be told to retry rather than being shown "请求失败 (500)" and
        left guessing whether the money moved.
        """

        message = str(getattr(exc, "orig", exc) or exc).splitlines()[0]
        # ``exc_info`` is what makes RedactionFilter attach the safe
        # file:line:function frames; without it this warning cannot say which
        # writer lost the race.
        logger.warning(
            "Request rejected while the database was busy: %s",
            message[:200],
            exc_info=exc,
            extra={"event": "database_busy"},
        )
        if "locked" in message.lower() or "busy" in message.lower():
            return JSONResponse(
                {"detail": "数据库正忙（有任务正在写入），本次请求未执行，请稍后重试"},
                status_code=503,
            )
        raise exc

    @app.get("/setup", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    def legacy_login_page() -> Response:
        """Old bookmarks go straight to the console; no credential forms remain."""
        return RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, db: Session = Depends(get_session)) -> Response:
        data = DashboardRepository(db).summary()
        runs = WorkflowRepository(db).list_runs(limit=8)
        return render(request, "dashboard.html", {"summary": data, "runs": runs}, db=db, page_title="今日总览")

    @app.get("/stores", response_class=HTMLResponse)
    def stores_page(request: Request, db: Session = Depends(get_session)) -> Response:
        from .config import PROJECT_ROOT
        from .workflows.store_setup.policy import OnboardingPolicy
        # Tests isolate SQLite in a temporary root but use the shipped policy.
        policy_root = PROJECT_ROOT if settings.testing else settings.project_root
        setup_policy = OnboardingPolicy.load(policy_root / "onboarding-policy.toml")
        stores = StoreRepository(db).list()
        # 筛选项只列账册里真有的平台;亚马逊固定排第一,因为三条流程只认它。
        platforms = {store.platform.label: store.platform.is_amazon for store in stores}
        platform_labels = ["亚马逊", *sorted(label for label in platforms if label != "亚马逊")]
        platform_counts = {
            "amazon": sum(store.platform.is_amazon for store in stores),
            "other": sum(store.platform.known and not store.platform.is_amazon for store in stores),
            "unknown": sum(not store.platform.known for store in stores),
        }
        return render(
            request,
            "stores.html",
            {"stores": stores, "platform_labels": platform_labels, "platform_counts": platform_counts,
             "setup_tracking_policy": setup_policy.model_dump(include={
                 "batch_poll_interval_ms", "batch_poll_request_timeout_ms", "batch_poll_error_retries",
                 "batch_poll_finalization_grace_ms", "total_deadline_seconds"})},
            db=db,
            page_title="店铺账册",
        )

    @app.get("/schedules", response_class=HTMLResponse)
    def schedules_page(request: Request, db: Session = Depends(get_session)) -> Response:
        registry = getattr(request.app.state, "workflow_registry", None)
        metadata = list(registry.public_metadata()) if registry is not None else []
        return render(
            request,
            "schedules.html",
            {
                "schedules": ScheduleRepository(db).list(),
                # Ineligible stores stay visible so preview can explain what
                # must be fixed instead of silently hiding an account.
                "stores": StoreRepository(db).list(),
                "workflow_labels": {
                    item["key"]: item["display_name"] for item in metadata
                },
                "workflow_definitions": {
                    item["key"]: item for item in metadata
                },
            },
            db=db,
            page_title="任务排期",
        )

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, db: Session = Depends(get_session)) -> Response:
        approvals = WorkflowRepository(db).list_pending_approvals()
        return render(request, "approvals.html", {"approvals": approvals}, db=db, page_title="资金审核台")

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback_page(
        request: Request,
        store: int | None = None,
        site: str | None = None,
        state: str | None = None,
        db: Session = Depends(get_session),
    ) -> Response:
        """Every feedback the automation has ever considered, per store and site.

        This is the ledger behind "never retry": an entry appears here exactly
        once and keeps whatever outcome it reached.  One run covers CA/UK/AU,
        so the marketplace has to be filterable — otherwise three sites' worth
        of reviews arrive as one undifferentiated list.
        """

        stmt = select(FeedbackReview).order_by(FeedbackReview.created_at.desc())
        if store is not None:
            stmt = stmt.where(FeedbackReview.store_id == store)
        if site:
            stmt = stmt.where(FeedbackReview.marketplace_code == site.upper())
        if state:
            stmt = stmt.where(FeedbackReview.state == state.upper())
        reviews = list(db.scalars(stmt.limit(500)))

        store_names = {
            int(row.id): str(row.name) for row in db.scalars(select(StoreHistory))
        }
        # Counts per store+site over the WHOLE ledger, not the filtered view —
        # the point of the summary is to show where the reviews actually are.
        breakdown: dict[tuple[int, str], int] = {}
        for store_id, code in db.execute(
            select(FeedbackReview.store_id, FeedbackReview.marketplace_code)
        ):
            key = (int(store_id), str(code))
            breakdown[key] = breakdown.get(key, 0) + 1

        return render(
            request,
            "feedback.html",
            {
                "reviews": reviews,
                "store_names": store_names,
                "breakdown": sorted(
                    (
                        {
                            "store_id": key[0],
                            "store_name": store_names.get(key[0], str(key[0])),
                            "code": key[1],
                            "count": value,
                        }
                        for key, value in breakdown.items()
                    ),
                    key=lambda item: (item["store_name"], item["code"]),
                ),
                "selected": {
                    "store": store,
                    "site": (site or "").upper(),
                    "state": (state or "").upper(),
                },
                "state_labels": FEEDBACK_STATE_LABELS,
                "reason_catalog": REASON_CATALOG,
                "category_labels": CATEGORY_LABELS,
                "review_criteria": AMAZON_REVIEW_CRITERIA,
            },
            db=db,
            page_title="反馈处理台",
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, db: Session = Depends(get_session)) -> Response:
        runs = WorkflowRepository(db).list_runs(limit=200)
        _attach_queue_projection(db, runs)
        registry = getattr(request.app.state, "workflow_registry", None)
        metadata = list(registry.public_metadata()) if registry is not None else []
        return render(
            request,
            "runs.html",
            {
                "runs": runs,
                "workflow_labels": {
                    item["key"]: item["display_name"] for item in metadata
                },
            },
            db=db,
            page_title="运行流水",
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: str, request: Request, db: Session = Depends(get_session)) -> Response:
        run = WorkflowRepository(db).get_run(run_id, full=True)
        _attach_queue_projection(db, [run])
        guards = list(db.scalars(select(OperationGuard).where(OperationGuard.run_id == run_id)))
        # Which reviews this run actually touched, with the buyer's own words —
        # a count cannot be checked against the seller account.
        feedback_reviews = (
            list(
                db.scalars(
                    select(FeedbackReview)
                    .where(FeedbackReview.run_id == run_id)
                    .order_by(FeedbackReview.rating, FeedbackReview.created_at)
                )
            )
            if run.workflow == "amazon_feedback_removal"
            else []
        )
        registry = getattr(request.app.state, "workflow_registry", None)
        definitions = {
            item["key"]: item
            for item in (registry.public_metadata() if registry is not None else ())
        }
        return render(
            request,
            "run_detail.html",
            {
                "run": run,
                "run_is_active": run.status in ACTIVE_RUN_STATUSES,
                "guards": guards,
                "workflow_meta": definitions.get(run.workflow),
                "feedback_reviews": feedback_reviews,
                "inventory_counts": (
                    read_inventory_gpsr_counts(db, run_id)
                    if run.workflow == "amazon_inventory_gpsr_attestation"
                    else None
                ),
                "can_resume_inventory": can_resume_inventory_run(db, run),
                "inventory_requirements": list(db.scalars(select(InventoryGpsrRequirement)
                    .join(InventoryGpsrItem, InventoryGpsrRequirement.item_id == InventoryGpsrItem.id)
                    .where(InventoryGpsrRequirement.run_id == run_id)
                    .order_by(InventoryGpsrRequirement.marketplace_code, InventoryGpsrItem.page_number.desc(),
                              InventoryGpsrItem.position.desc(), InventoryGpsrRequirement.requirement_type.desc())))
                    if run.workflow == "amazon_inventory_gpsr_attestation" else [],
                "inventory_items": (
                    list(db.scalars(
                        select(InventoryGpsrItem)
                        .where(InventoryGpsrItem.run_id == run_id)
                        .order_by(
                            InventoryGpsrItem.page_number.desc(),
                            InventoryGpsrItem.position.desc(),
                        )
                    ))
                    if run.workflow == "amazon_inventory_gpsr_attestation"
                    else []
                ),
                "inventory_state_labels": INVENTORY_STATE_LABELS,
                "reason_catalog": REASON_CATALOG,
                "category_labels": CATEGORY_LABELS,
            },
            db=db,
            page_title=f"任务 {run.id[:8]}",
        )

    @app.get("/diagnostics", response_class=HTMLResponse)
    def diagnostics_page(request: Request, db: Session = Depends(get_session)) -> Response:
        account = db.scalar(select(ZiniaoAccount).limit(1))
        ziniao_ref = str(account.credential_ref or "").strip() if account else ""
        feishu_setting = db.get(SystemSetting, "feishu")
        feishu_metadata = (
            dict(feishu_setting.value)
            if feishu_setting is not None and feishu_setting.value
            else {}
        )
        feishu_ref = str(feishu_metadata.get("credential_ref", "")).strip()
        diagnostics = {
            "app_version": __version__,
            "build_commit": build_commit(settings.project_root),
            "migration_revision": database_revision(engine),
            "executable": str(settings.ziniao_executable),
            "executable_exists": settings.ziniao_executable.exists(),
            "control_port": settings.ziniao_port,
            "control_reachable": _port_reachable(settings.ziniao_host, settings.ziniao_port),
            "database": str(settings.database_url).split("///", 1)[-1],
            "ziniao_credential_registered": bool(ziniao_ref),
            "ziniao_credential_readable": bool(
                ziniao_ref and credential_exists(ziniao_ref)
            ),
            "feishu_enabled": bool(feishu_metadata.get("enabled", False)),
            "feishu_credential_registered": bool(feishu_ref),
            "feishu_credential_readable": bool(
                feishu_ref and credential_exists(feishu_ref)
            ),
            # The feedback classifier borrows the operator's Codex endpoint.
            # Its absence is not an error: without it every entry simply waits
            # for a human.
            "ai_endpoint": _resolved_classifier_endpoint(),
            "host": f"{settings.host}:{settings.port}",
            # Non-secret fields only, so the forms can be pre-filled without a
            # round trip.  Passwords and App Secret are never sent to the page —
            # not even masked, since a mask still leaks the length.
            "ziniao_company": (account.company or "") if account else "",
            "ziniao_username": (account.username or "") if account else "",
            "feishu_app_id": str(feishu_metadata.get("app_id", "") or ""),
            "feishu_chat_id": str(feishu_metadata.get("chat_id", "") or ""),
        }
        return render(request, "diagnostics.html", {"diagnostics": diagnostics}, db=db, page_title="系统诊断")

    @app.get("/evidence/{evidence_id}")
    def evidence_file(
        evidence_id: str,
        _: LocalOperator = Depends(current_operator),
        db: Session = Depends(get_session),
    ) -> FileResponse:
        record = db.get(Evidence, evidence_id)
        if record is None:
            raise HTTPException(404, "证据文件不存在")
        root = settings.evidence_dir.resolve()
        path = Path(record.file_path)
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(404, "证据文件不存在")
        return FileResponse(path, filename=path.name)

    api = APIRouter(prefix="/api", dependencies=[Depends(current_operator)])
    catalog_lock = asyncio.Lock()

    def serialized_store_catalog(handler):
        # Serialize starting/resuming probes with sync deletion. Running probes
        # are still checked explicitly; ordinary workflow claims use SQLite's
        # IMMEDIATE write transaction as their cross-process boundary.
        @wraps(handler)
        async def guarded(*args, **kwargs):
            async with catalog_lock:
                return await handler(*args, **kwargs)
        return guarded


    def active_workflow_registry(request: Request) -> WorkflowRegistry:
        registry = getattr(request.app.state, "workflow_registry", None)
        if not isinstance(registry, WorkflowRegistry):
            raise HTTPException(503, "工作流注册表尚未初始化")
        return registry

    async def refresh_schedule_projection(
        manager: Any, schedule_id: int
    ) -> ScheduleProjectionReport | None:
        """Return the scheduler report from one-row or full-refresh adapters."""

        refresh_one = getattr(manager, "refresh_schedule", None)
        if callable(refresh_one):
            return await refresh_one(schedule_id)
        refresh_all = getattr(manager, "refresh", None)
        if callable(refresh_all):
            return await refresh_all()
        raise RuntimeError("当前定时器不支持刷新")

    async def schedule_mutation_projection(
        request: Request,
        *,
        schedule_id: int,
        expects_projection: bool,
        durable_action: str,
    ) -> dict[str, Any]:
        """Describe projection lag without turning a committed write into 5xx.

        A create request is not idempotent, and neither patch nor delete needs
        to be replayed once SQLite has committed.  Projection trouble must
        therefore remain a successful durable response with an explicit
        do-not-repeat warning.  A later process restart rebuilds APScheduler
        from SQLite.
        """

        manager = request.app.state.schedule_manager
        if manager is None:
            return {
                "scheduler_refreshed": False,
                "scheduler_failed_schedule_ids": [schedule_id],
                "warning": (
                    f"{durable_action}，但当前进程没有定时器，尚未应用到运行计划；"
                    "请不要重复提交本次操作。重启后台会按数据库记录自动重新加载。"
                ),
            }

        try:
            projection = await refresh_schedule_projection(manager, schedule_id)
            # Compatibility with small embedded/test adapters that predate the
            # structured report. Production ScheduleManager always returns it.
            if projection is None:
                return {"scheduler_refreshed": True}

            reported_projected = getattr(
                projection, "projected_schedule_ids", None
            )
            reported_failures = getattr(projection, "failed_schedule_ids", None)
            if reported_projected is None or reported_failures is None:
                raise RuntimeError("定时器刷新结果格式无效")
            projected_schedule_ids = frozenset(
                int(item) for item in reported_projected
            )
            failed_schedule_ids = frozenset(
                int(item) for item in reported_failures
            )
            target_is_projected = schedule_id in projected_schedule_ids
            target_failed = (
                schedule_id in failed_schedule_ids
                or target_is_projected != expects_projection
            )
            if not target_failed:
                # A malformed unrelated legacy row is isolated by refresh().
                # It must not make this successfully projected mutation look
                # unsuccessful to the operator.
                return {"scheduler_refreshed": True}

            logger.error(
                "schedule_mutation_projection_incomplete schedule_id=%s "
                "expects_projection=%s projected=%s reported_failed=%s",
                schedule_id,
                expects_projection,
                target_is_projected,
                schedule_id in failed_schedule_ids,
            )
        except Exception:
            # SQLite is already authoritative. Raising here would invite an
            # unsafe duplicate create/replay while not undoing the first write.
            logger.exception(
                "schedule_mutation_refresh_failed schedule_id=%s", schedule_id
            )

        return {
            "scheduler_refreshed": False,
            "scheduler_failed_schedule_ids": [schedule_id],
            "warning": (
                f"{durable_action}，但定时器尚未应用这次变更；"
                "请不要重复提交本次操作。重启后台会按数据库记录自动重新加载。"
            ),
        }

    def normalise_workflow_input(
        request: Request,
        *,
        workflow: str,
        mode: str,
        workflow_config: dict[str, Any] | None,
        marketplace_codes: list[str] | None = None,
    ) -> tuple[dict[str, Any], int, list[str]]:
        """Validate one schedule/run input through the code-only registry.

        ``marketplace_codes`` remains an HTTP compatibility field for the
        existing console and older clients.  The versioned workflow config is
        authoritative for every newly persisted row.
        """

        registry = active_workflow_registry(request)
        try:
            definition = registry.definition(workflow)
        except WorkflowNotRegistered as exc:
            raise ValueError("工作流不在当前版本的代码白名单中") from exc
        supported = {item.value for item in definition.supported_modes}
        if mode not in supported:
            raise ValueError(f"工作流不支持运行模式：{mode}")

        config = dict(workflow_config or {})

        def normalized_codes(values: Any) -> list[str]:
            """Compare legacy and versioned inputs using registry semantics.

            Older clients may send lower-case or repeated marketplace codes,
            while the registered workflow normalizes them to upper-case unique
            values.  Normalize both sides before the compatibility comparison
            so two equivalent requests are not rejected merely because their
            spelling differs.
            """

            return list(
                dict.fromkeys(
                    str(code).strip().upper()
                    for code in (values or [])
                    if str(code).strip()
                )
            )

        legacy_codes = normalized_codes(marketplace_codes)
        if definition.requires_marketplace_targets:
            configured = normalized_codes(config.get("marketplace_codes"))
            if configured and legacy_codes and configured != legacy_codes:
                raise ValueError("站点参数前后不一致，请刷新页面后重新选择")
            if not configured and legacy_codes:
                config["marketplace_codes"] = legacy_codes
        try:
            normalized = registry.validate_config(workflow, config)
        except (ValidationError, TypeError, ValueError) as exc:
            raise ValueError("工作流参数不符合当前版本的固定定义") from exc
        codes = [str(code).upper() for code in normalized.get("marketplace_codes", [])]
        if definition.requires_marketplace_targets and not codes:
            raise ValueError("请至少选择一个站点")
        return normalized, int(definition.config_version), codes

    @api.get("/workflows")
    def api_workflows(request: Request) -> list[dict[str, Any]]:
        """Return only the registry's fixed, non-sensitive form metadata."""

        return list(active_workflow_registry(request).public_metadata())

    @api.post("/settings/feishu", status_code=200)
    async def save_feishu_settings(
        payload: FeishuSettingsInput,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Store the Feishu app the same way ``configure feishu`` does.

        The secret goes to Windows Credential Manager and never to SQLite; only
        the App ID, Chat ID and the credential's name are persisted here.  The
        response deliberately carries no secret back — not even a masked one,
        because a masked value still tells an attacker its length.
        """

        reference = DEFAULT_FEISHU_TARGET
        try:
            write_generic_credential(
                reference,
                {
                    "app_id": payload.app_id,
                    "app_secret": payload.app_secret,
                    "chat_id": payload.chat_id,
                },
                username=payload.app_id,
            )
        except (CredentialStoreError, ValueError, OSError) as exc:
            raise HTTPException(502, f"写入 Windows 凭据管理器失败：{exc}") from exc
        # Read it straight back.  Security software has been observed silently
        # dropping credential writes, and a write that did not stick would only
        # surface much later as an unexplained notification failure.
        expected = {
            "app_id": payload.app_id,
            "app_secret": payload.app_secret,
            "chat_id": payload.chat_id,
        }
        if not credential_matches(reference, expected):
            raise HTTPException(
                502,
                "凭据写入后无法回读，请检查安全软件是否拦截了 Windows 凭据管理器。",
            )
        row = db.get(SystemSetting, "feishu")
        metadata = {
            "credential_ref": reference,
            "app_id": payload.app_id,
            "chat_id": payload.chat_id,
            "enabled": True,
        }
        if row is None:
            db.add(SystemSetting(key="feishu", value=metadata))
        else:
            row.value = metadata
        db.commit()
        return {"status": "saved", "app_id": payload.app_id, "chat_id": payload.chat_id}

    @api.post("/settings/feishu/test", status_code=200)
    async def test_feishu_settings(
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Send one real card, so a wrong Chat ID is found now rather than later.

        Configuration that is only validated the next time a payout runs is
        configuration nobody trusts.
        """

        row = db.get(SystemSetting, "feishu")
        metadata = dict(row.value) if row is not None and row.value else {}
        reference = str(metadata.get("credential_ref") or "").strip()
        if not reference:
            raise HTTPException(409, "尚未保存飞书配置")
        from .notifications.dto import NotificationKind, SafeRunNotice
        from .notifications.feishu import FeishuNotifier

        notifier = FeishuNotifier(lambda: read_generic_credential(reference))
        try:
            await notifier.send(
                SafeRunNotice(
                    kind=NotificationKind.RUN_COMPLETED,
                    title="紫鸟提现 · 配置测试",
                    summary="这是一条测试消息，看到它说明飞书通知已经配好了。",
                    run_short_id="TESTONLY",
                    store_name="配置测试",
                    next_action="无需操作。",
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator verbatim
            raise HTTPException(502, f"发送失败：{_safe_probe_error(exc)}") from exc
        return {"status": "sent"}

    @api.post("/settings/ziniao", status_code=200)
    async def save_ziniao_settings(
        payload: ZiniaoSettingsInput,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Store the Ziniao login exactly as ``configure ziniao`` does."""

        reference = DEFAULT_ZINIAO_TARGET
        try:
            write_generic_credential(
                reference,
                {
                    "company": payload.company,
                    "username": payload.username,
                    "password": payload.password,
                },
                username=payload.username,
            )
        except (CredentialStoreError, ValueError, OSError) as exc:
            raise HTTPException(502, f"写入 Windows 凭据管理器失败：{exc}") from exc
        expected = {
            "company": payload.company,
            "username": payload.username,
            "password": payload.password,
        }
        if not credential_matches(reference, expected):
            raise HTTPException(
                502,
                "凭据写入后无法回读，请检查安全软件是否拦截了 Windows 凭据管理器。",
            )
        account = db.scalar(select(ZiniaoAccount).limit(1))
        if account is None:
            db.add(
                ZiniaoAccount(
                    display_name="紫鸟主账号",
                    company=payload.company,
                    username=payload.username,
                    credential_ref=reference,
                )
            )
        else:
            account.company = payload.company
            account.username = payload.username
            account.credential_ref = reference
            account.enabled = True
        db.commit()
        return {"status": "saved", "company": payload.company, "username": payload.username}

    @api.post("/ziniao/webdriver/start", status_code=200)
    async def start_webdriver_mode(
        payload: WebdriverStart,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Put Ziniao into WebDriver mode, which force-closes its open windows.

        Refused while a run holds the browser or an ARMED guard (click not yet
        returned) is outstanding — see ``webdriver_mode.describe_blockers``.  The service
        answers 409 in that case and kills nothing.

        ``force`` 越过那些理由。它是**显式的第二次点击**，界面只在收到 409 之后
        才把那颗按钮露出来 —— 前两类阻止是本进程内的锁，关掉浏览器清不掉，
        卡住时除了重启服务没有别的出路。越过的内容会写进 WARNING 日志。
        """

        starter = getattr(
            request.app.state.automation_service, "start_webdriver_mode", None
        )
        if not callable(starter):
            raise HTTPException(503, "紫鸟运行服务尚未配置")
        _release_before_automation(db)
        try:
            result = await starter(force=payload.force)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if not isinstance(result, dict):
            raise HTTPException(502, "紫鸟模式切换返回了无效结构")
        if not result.get("ok"):
            status = 409 if result.get("status") == "busy" else 502
            raise HTTPException(status, str(result.get("message") or "切换失败"))
        return result

    @api.post("/ziniao/sync")
    @serialized_store_catalog
    async def sync_ziniao(
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        _release_before_automation(db)
        try:
            result = await request.app.state.automation_service.sync_ziniao()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if not isinstance(result, dict):
            raise HTTPException(502, "紫鸟同步服务返回了无效结构")
        if result.get("complete") is True and "profiles" not in result:
            raise HTTPException(502, "完整同步结果缺少店铺列表，本次未同步或删除")
        profiles = result.get("profiles", [])
        if not isinstance(profiles, list):
            raise HTTPException(502, "紫鸟返回了无效的店铺列表，本次未同步或删除")
        complete = result.get("complete") is True
        selectors: set[str] = set()
        for profile in profiles:
            if not isinstance(profile, dict):
                raise HTTPException(502, "紫鸟返回了无效的店铺记录")
            value = str(profile.get("selector_value") or "").strip()
            if profile.get("selector_type") not in {"oauth", "id"} or not value or value in selectors:
                raise HTTPException(502, "紫鸟店铺标识缺失或重复，本次未同步或删除")
            selectors.add(value)
        if not profiles and not complete:
            return {**result, "created": int(result.get("created", 0)), "updated": int(result.get("updated", 0)), "deleted": 0}
        account = db.scalar(select(ZiniaoAccount).order_by(ZiniaoAccount.id).limit(1))
        account_id = account.id if account is not None else None
        missing = StoreRepository(db).missing_profile_ids(profiles, account_id=account_id) if complete and account_id else []
        _release_before_automation(db)
        active_probe = getattr(request.app.state.automation_service, "has_active_store_setup", None)
        for store_id in missing:
            if not callable(active_probe):
                raise HTTPException(409, "当前服务未提供建档状态检查，本次同步删除未执行")
            if await active_probe(store_id):
                raise HTTPException(409, "待删除店铺正在建档或等待验证；请完成或取消建档后重新同步。本次未删除店铺")
        if missing:
            database = request.app.state.engine.url.database
            if database and database != ":memory:":
                await asyncio.to_thread(
                    backup_sqlite_database, Path(database), settings.backup_dir,
                    from_revision="before-store-sync", to_revision="store-sync",
                )
        try:
            # The worker uses this same SQLite transaction mode for claiming.
            # Either it claims first (deletion reports busy), or sync cancels
            # queued work first (there is nothing left for the worker to claim).
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            account = db.get(ZiniaoAccount, account_id) if account_id else None
            if account is None:
                account = ZiniaoAccount(display_name="紫鸟主账号")
                db.add(account)
                db.flush()
            repo = StoreRepository(db)
            removal = repo.delete_missing_profiles(profiles, account_id=account.id) if complete else {"deleted": 0, "deleted_schedule_ids": [], "cancelled_runs": 0}
            created, updated = repo.upsert_profiles(profiles, account_id=account.id)
            account.last_synced_at = datetime.now(timezone.utc)
            account.sync_error = None
            db.commit()
        except ConflictError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "紫鸟店铺标识存在冲突；本次同步和删除均已回滚") from exc
        response = {**result, "created": created, "updated": updated, **removal}
        if removal["deleted_schedule_ids"]:
            manager = request.app.state.schedule_manager
            try:
                refresh = getattr(manager, "refresh", None)
                if callable(refresh):
                    await refresh()
                elif manager is not None:
                    for schedule_id in removal["deleted_schedule_ids"]:
                        await refresh_schedule_projection(manager, schedule_id)
                response["scheduler_refreshed"] = manager is not None
            except Exception:
                # Deletion is committed, and stale callbacks recheck SQLite.
                # Do not turn success into a retry of a destructive operation.
                response["scheduler_refreshed"] = False
                response["warning"] = "店铺及排期已删除；定时器列表暂未刷新，旧回调不会再创建这些店铺的任务"
        return response

    @api.get("/stores", response_model=list[StoreView])
    def api_stores(db: Session = Depends(get_session)) -> list[Any]:
        return StoreRepository(db).list()

    @api.post("/stores/{store_id}/detect-identity")
    @serialized_store_catalog
    async def api_detect_store_identity(
        store_id: int,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        detector = getattr(request.app.state.automation_service, "detect_store_identity", None)
        if not callable(detector):
            raise HTTPException(503, "店铺身份检测服务尚未配置")
        try:
            result = await detector(store_id)
            return JSONResponse(
                result,
                status_code=(
                    202
                    if result.get("status") in {"WAITING_AUTH", "CHECKING"}
                    else 200
                ),
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            # Keep unexpected browser-automation failures actionable instead
            # of returning an empty 500 page.  The runtime intentionally does
            # not auto-retry identity detection, so this response cannot cause
            # burst launches/navigation.
            raise HTTPException(
                503,
                "紫鸟店铺页面连接意外中断；本次检测已停止，请稍后手动重试",
            ) from exc

    async def _identity_probe_action(
        request: Request,
        store_id: int,
        probe_id: str,
        method_name: str,
    ) -> JSONResponse:
        action = getattr(request.app.state.automation_service, method_name, None)
        if not callable(action):
            raise HTTPException(503, "店铺身份检测服务尚未配置")
        try:
            result = await action(store_id, probe_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        waiting = result.get("status") in {"WAITING_AUTH", "CHECKING"}
        return JSONResponse(result, status_code=202 if waiting else 200)

    @api.get("/stores/{store_id}/identity-probes/{probe_id}")
    async def api_get_identity_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _identity_probe_action(request, store_id, probe_id, "get_identity_probe")

    @api.post("/stores/{store_id}/identity-probes/{probe_id}/continue")
    @serialized_store_catalog
    async def api_continue_identity_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _identity_probe_action(
            request, store_id, probe_id, "continue_identity_probe"
        )

    @api.post("/stores/{store_id}/identity-probes/{probe_id}/cancel")
    async def api_cancel_identity_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _identity_probe_action(
            request, store_id, probe_id, "cancel_identity_probe"
        )

    async def _marketplace_setup_probe_action(
        request: Request,
        store_id: int,
        probe_id: str,
        method_name: str,
    ) -> JSONResponse:
        action = getattr(request.app.state.automation_service, method_name, None)
        if not callable(action):
            raise HTTPException(503, "付款账户自动建档服务尚未配置")
        try:
            result = await action(store_id, probe_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        waiting = result.get("status") in {"WAITING_AUTH", "CHECKING"}
        return JSONResponse(result, status_code=202 if waiting else 200)

    @api.post("/stores/{store_id}/detect-marketplace-setup")
    @serialized_store_catalog
    async def api_detect_marketplace_setup(
        store_id: int,
        payload: MarketplaceSetupInput,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        detector = getattr(
            request.app.state.automation_service, "detect_marketplace_setup", None
        )
        if not callable(detector):
            raise HTTPException(503, "付款账户自动建档服务尚未配置")
        try:
            result = await detector(store_id, payload.marketplace_codes)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        waiting = result.get("status") in {"WAITING_AUTH", "CHECKING"}
        return JSONResponse(result, status_code=202 if waiting else 200)

    @api.get("/stores/{store_id}/marketplace-setup-probes/{probe_id}")
    async def api_get_marketplace_setup_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _marketplace_setup_probe_action(
            request, store_id, probe_id, "get_marketplace_setup_probe"
        )

    @api.post("/stores/{store_id}/marketplace-setup-probes/{probe_id}/continue")
    @serialized_store_catalog
    async def api_continue_marketplace_setup_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _marketplace_setup_probe_action(
            request, store_id, probe_id, "continue_marketplace_setup_probe"
        )

    @api.post("/stores/{store_id}/marketplace-setup-probes/{probe_id}/cancel")
    async def api_cancel_marketplace_setup_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _marketplace_setup_probe_action(
            request, store_id, probe_id, "cancel_marketplace_setup_probe"
        )

    @api.post("/stores/{store_id}/store-setup-probes/{probe_id}/check-release")
    async def api_setup_check_release(store_id: int, probe_id: str, _: LocalOperator = Depends(verified_operator)):
        try:
            return await SetupRepository(sessions).check_release(store_id, probe_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SetupConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/stores/{store_id}/setup-summary")
    async def api_setup_summary(store_id: int, _: LocalOperator = Depends(current_operator)):
        try:
            return SetupRepository(sessions).summary(store_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/stores/{store_id}/store-setup-probes/{probe_id}/release-environment")
    @serialized_store_catalog
    async def api_setup_release_environment(store_id: int, probe_id: str, request: Request,
                                            _: LocalOperator = Depends(verified_operator)):
        try:
            service = request.app.state.automation_service
            if await service.has_active_store_setup(store_id):
                raise SetupConflict("该店仍在核验，请先取消并等待结束", probe_id)
            return await SetupRepository(sessions).release_environment(store_id, probe_id, service.ziniao_controller)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SetupConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(504, "紫鸟释放确认超时，记录已保留，请稍后重新检查") from exc

    @api.post("/stores/setup-batch/preview")
    async def api_setup_batch_preview(payload: StoreSetupBatchPreview, _: LocalOperator = Depends(verified_operator)):
        if set(payload.selections) - set(payload.store_ids):
            raise HTTPException(422, "站点选择包含未选择的店铺")
        return SetupRepository(sessions).preview(payload.store_ids, payload.mode, payload.selections)

    @api.post("/stores/{store_id}/detect-store-setup")
    @serialized_store_catalog
    async def api_detect_store_setup(
        store_id: int,
        payload: StoreSetupInput,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        detector = getattr(
            request.app.state.automation_service, "detect_store_setup", None
        )
        if not callable(detector):
            raise HTTPException(503, "店铺统一自动建档服务尚未配置")
        try:
            options = payload.model_dump(exclude_unset=True, exclude={"marketplace_codes"})
            result = await detector(store_id, payload.marketplace_codes, **options)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ZiniaoCredentialError as exc:
            # Keep this a system-level, machine-readable conflict.  The
            # browser can pause a multi-store queue at its current position
            # instead of counting every store as a separate launch failure.
            safe_message = _safe_probe_error(exc).removeprefix(
                f"{type(exc).__name__}: "
            )
            raise HTTPException(
                409,
                detail={
                    "code": _ZINIAO_CREDENTIAL_ERROR_CODE,
                    "message": (
                        "紫鸟凭据预检未通过："
                        f"{safe_message}。"
                        "请到“系统诊断”重新保存紫鸟公司、账号和密码后重试。"
                    ),
                },
            ) from exc
        except SetupConflict as exc:
            raise HTTPException(409, {"code": exc.code, "message": str(exc), "probe_id": exc.probe_id,
                                      "store_id": exc.store_id}) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        waiting = result.get("status") in {"WAITING_AUTH", "CHECKING"}
        return JSONResponse(result, status_code=202 if waiting else 200)

    @api.get("/stores/{store_id}/store-setup-probe")
    async def api_get_active_store_setup_probe(
        store_id: int,
        request: Request,
        db: Session = Depends(get_session),
    ) -> Response:
        """The probe currently holding this store, without needing its id.

        The editor opens against whatever the server says is running, instead of
        relying on a ``probe_id`` the browser may have dropped on a reload.  A
        service that cannot answer degrades to "nothing running" rather than
        failing: this is the call that restores the editor, and it must never be
        the reason the editor will not open.
        """

        StoreRepository(db).get(store_id)
        lookup = getattr(
            request.app.state.automation_service,
            "get_active_store_setup_probe",
            None,
        )
        if not callable(lookup):
            return Response(status_code=204)
        payload = await lookup(store_id)
        if payload is None:
            return Response(status_code=204)
        return JSONResponse(payload)

    @api.get("/stores/{store_id}/store-setup-probes/{probe_id}")
    async def api_get_store_setup_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _marketplace_setup_probe_action(
            request, store_id, probe_id, "get_store_setup_probe"
        )

    @api.post("/stores/{store_id}/store-setup-probes/{probe_id}/continue")
    @serialized_store_catalog
    async def api_continue_store_setup_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _marketplace_setup_probe_action(
            request, store_id, probe_id, "continue_store_setup_probe"
        )

    @api.post("/stores/{store_id}/store-setup-probes/{probe_id}/cancel")
    async def api_cancel_store_setup_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _marketplace_setup_probe_action(
            request, store_id, probe_id, "cancel_store_setup_probe"
        )

    @api.post("/stores", response_model=StoreView, status_code=201)
    def api_create_store(
        payload: StoreCreate,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Any:
        return StoreRepository(db).create(**payload.model_dump())

    async def _require_no_active_store_setup(request: Request, store_id: int) -> None:
        """Refuse to rewrite a store while a probe is driving its browser.

        Both writers on the store editor need this.  Reset used to be the only
        guarded one, which made the guard theatre: 「保存建档」 sits directly
        above 「删除 / 重置建档」, carries the same identity fields, and also
        calls ``replace_marketplaces`` — flipping the very site rows that reset
        deliberately leaves alone.  The blocked path was strictly gentler than
        the open one.
        """

        active_check = getattr(
            request.app.state.automation_service, "has_active_store_setup", None
        )
        if not callable(active_check):
            raise HTTPException(503, "店铺建档活动状态检查服务尚未配置")
        try:
            if await active_check(store_id):
                raise ConflictError(
                    "该店铺的统一建档正在运行，正占用对应的紫鸟店铺窗口。"
                    "请先在该店铺的建档窗口点击「取消并关闭该店铺窗口」，"
                    "或等待本次检测结束后再操作。"
                )
        except ConflictError:
            raise
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.patch("/stores/{store_id}", response_model=StoreView)
    async def api_patch_store(
        store_id: int,
        payload: StorePatch,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Any:
        await _require_no_active_store_setup(request, store_id)
        changes = payload.model_dump(exclude_unset=True)
        marketplaces = changes.pop("marketplaces", None)
        repo = StoreRepository(db)
        store = repo.update(store_id, **changes) if changes else repo.get(store_id)
        if marketplaces is not None:
            repo.replace_marketplaces(store_id, marketplaces)
            db.flush()
            db.expire(store, ["marketplaces"])
            store = repo.get(store_id)
        return store

    @api.post("/stores/setup/reset-batch")
    async def api_reset_store_setup_batch(
        payload: StoreSetupBatchResetInput,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        # Async process-local checks happen BEFORE reserving SQLite for writing.
        # Probe creation also reserves SQLite before launching a browser, so the
        # durable checks below close the gap between checking and resetting.
        process_blockers: dict[int, str] = {}
        recover_resources = getattr(request.app.state.automation_service, "recover_setup_resources", None)
        db.rollback()
        for item in payload.stores:
            try:
                await _require_no_active_store_setup(request, item.store_id)
                if callable(recover_resources):
                    await recover_resources([item.store_id])
            except (ConflictError, SetupConflict) as exc:
                process_blockers[item.store_id] = str(exc)
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        repo = StoreRepository(db)
        blocked = []
        for item in payload.stores:
            name = f"店铺 #{item.store_id}"
            try:
                store = repo.get(item.store_id)
                name = store.name
                if not store.platform.is_amazon:
                    raise ConflictError("批量删除建档只处理亚马逊店铺")
                if store.setup_revision != item.expected_setup_revision:
                    raise ConflictError("建档已变化，请刷新页面、重新勾选并确认")
                if item.store_id in process_blockers:
                    raise ConflictError(process_blockers[item.store_id])
                repo.require_setup_resettable(item.store_id)
            except (ConflictError, NotFoundError) as exc:
                blocked.append({"store_id": item.store_id, "store_name": name,
                                "eligible": False, "reason": str(exc)})
        if blocked:
            db.rollback()
            raise HTTPException(409, detail={"code": "BATCH_SETUP_RESET_BLOCKED",
                "message": "整批未修改，请处理受阻店铺或取消勾选后重试", "targets": blocked})
        disabled = []
        reset_ids = []
        # No await / network work until this one transaction is committed.
        for item in payload.stores:
            _, schedule_ids = repo.reset_setup(item.store_id)
            disabled.extend(schedule_ids)
            reset_ids.append(item.store_id)
        db.commit()
        response = {"status": "reset", "reset_store_ids": reset_ids,
                    "reset_count": len(reset_ids), "disabled_schedules": len(disabled),
                    "scheduler_refreshed": True}
        if disabled:
            # One full refresh, not N store/schedule refreshes. Failure after
            # commit is explicitly reported as committed; never invite replay.
            try:
                manager = request.app.state.schedule_manager
                if manager is None:
                    raise RuntimeError("scheduler not configured")
                projection = await refresh_schedule_projection(manager, disabled[0])
                if projection is not None:
                    projected = set(projection.projected_schedule_ids)
                    failed = set(projection.failed_schedule_ids)
                    if set(disabled) & (projected | failed):
                        raise RuntimeError("disabled schedules still projected")
            except Exception:
                logger.exception("batch_setup_reset_scheduler_refresh_failed")
                response.update(scheduler_refreshed=False,
                    warning="建档已删除，排期已在数据库停用；定时器刷新未完成，请勿重复删除。重新启动后台后会按数据库重建定时器。")
        return response

    @api.delete(
        "/stores/{store_id}/setup", response_model=StoreSetupResetView
    )
    async def api_reset_store_setup(
        store_id: int,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Any:
        repo = StoreRepository(db)
        repo.get(store_id)
        await _require_no_active_store_setup(request, store_id)
        db.rollback()
        recover_resources = getattr(request.app.state.automation_service, "recover_setup_resources", None)
        if callable(recover_resources):
            try:
                await recover_resources([store_id])
            except SetupConflict as exc:
                raise HTTPException(409, {"code": exc.code, "message": str(exc), "probe_id": exc.probe_id,
                                          "store_id": exc.store_id}) from exc

        store, disabled_schedule_ids = repo.reset_setup(store_id)
        # SQLite must become authoritative before the in-memory scheduler is
        # rebuilt. A disabled schedule callback that races after this commit
        # will find enabled=False and create no task.
        db.commit()
        manager = request.app.state.schedule_manager
        if manager is not None and disabled_schedule_ids:
            await refresh_schedule_projection(manager, disabled_schedule_ids[0])
        db.expire(store, ["marketplaces"])
        store = repo.get(store_id)
        return {
            "status": "reset",
            "store": store,
            "disabled_schedules": len(disabled_schedule_ids),
        }

    @api.get("/schedules", response_model=list[ScheduleView])
    def api_schedules(db: Session = Depends(get_session)) -> list[Any]:
        return ScheduleRepository(db).list()

    @api.post("/schedules/batch/preview")
    def api_preview_schedule_batch(
        payload: BatchSchedulePreviewInput,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        template = payload.template.model_dump()
        config, _, _ = normalise_workflow_input(
            request,
            workflow=payload.template.workflow,
            mode=payload.template.mode,
            workflow_config=payload.template.workflow_config,
        )
        template["workflow_config"] = config
        return BatchScheduleService(
            db, active_workflow_registry(request)
        ).preview(
            store_ids=payload.store_ids,
            template=template,
        )

    @api.post("/schedules/batch", status_code=201)
    async def api_create_schedule_batch(
        payload: BatchScheduleCreateInput,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        service = BatchScheduleService(db, active_workflow_registry(request))
        template = payload.template.model_dump()
        config, _, _ = normalise_workflow_input(
            request,
            workflow=payload.template.workflow,
            mode=payload.template.mode,
            workflow_config=payload.template.workflow_config,
        )
        template["workflow_config"] = config
        # Authentication and CSRF checks have already completed.  End their
        # read transaction so BatchScheduleService can reserve SQLite with
        # BEGIN IMMEDIATE before the idempotency lookup and all child inserts.
        db.commit()
        try:
            result = service.create(
                request_id=str(payload.request_id),
                store_ids=payload.store_ids,
                template=template,
            )
        except BatchScheduleValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.preview) from exc
        db.commit()

        # SQLite is authoritative.  One full refresh projects every new child
        # into APScheduler and avoids N repeated rebuilds for an N-store batch.
        manager = request.app.state.schedule_manager
        result["scheduler_refreshed"] = False
        if manager is None:
            result["warning"] = (
                "排期已保存，但当前进程没有定时器；请重启后台后再确认，"
                "不要重新新建排期。"
            )
        else:
            try:
                refresh = getattr(manager, "refresh", None)
                if not callable(refresh):
                    raise RuntimeError("当前定时器不支持完整刷新")
                projection = await refresh()
                failed_schedule_ids = frozenset()
                # ``None`` keeps compatibility with small embedded adapters;
                # the production ScheduleManager returns a structured report.
                if projection is not None:
                    reported_failures = getattr(
                        projection, "failed_schedule_ids", None
                    )
                    if reported_failures is None:
                        raise RuntimeError("定时器刷新结果格式无效")
                    failed_schedule_ids = frozenset(
                        int(schedule_id) for schedule_id in reported_failures
                    )
                batch_schedule_ids = {
                    int(item["id"]) for item in result.get("schedules", ())
                }
                failed_batch_schedule_ids = sorted(
                    batch_schedule_ids & failed_schedule_ids
                )
                if failed_batch_schedule_ids:
                    logger.error(
                        "schedule_batch_projection_incomplete batch_id=%s schedule_ids=%s",
                        result.get("batch_id"),
                        failed_batch_schedule_ids,
                    )
                    result["scheduler_failed_schedule_ids"] = (
                        failed_batch_schedule_ids
                    )
                    result["scheduler_refreshed"] = False
                    result["warning"] = (
                        "排期已保存，但本批次有启用排期未成功加载到定时器；"
                        "请使用同一批次重试，不要重新创建排期。重启后台也会自动重新加载。"
                    )
                else:
                    # Unrelated malformed legacy schedules remain isolated and
                    # must not make this healthy batch look unprojected.
                    result["scheduler_refreshed"] = True
            except Exception:
                # The batch is already durable.  Report projection lag without
                # turning a successful idempotent write into a client retry
                # loop; retrying the same UUID will attempt refresh again.
                logger.exception(
                    "schedule_batch_refresh_failed batch_id=%s",
                    result.get("batch_id"),
                )
                result["scheduler_refreshed"] = False
                result["warning"] = (
                    "排期已保存，但定时器刷新失败；请在当前页面使用同一批次重试，"
                    "不要重新新建排期。重启后台也会自动重新加载。"
                )
        return result

    @api.post(
        "/schedules",
        response_model=ScheduleMutationView,
        response_model_exclude_none=True,
        status_code=201,
    )
    async def api_create_schedule(
        payload: ScheduleCreate,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Any:
        values = payload.model_dump()
        config, version, codes = normalise_workflow_input(
            request,
            workflow=payload.workflow,
            mode=payload.mode,
            workflow_config=payload.workflow_config,
            marketplace_codes=list(payload.marketplace_codes),
        )
        values.update(
            workflow_config=config,
            workflow_config_version=version,
            marketplace_codes=codes,
        )
        schedule = ScheduleRepository(
            db, active_workflow_registry(request)
        ).create(**values)
        db.commit()
        projection = await schedule_mutation_projection(
            request,
            schedule_id=schedule.id,
            expects_projection=bool(schedule.enabled),
            durable_action="排期已保存",
        )
        result = ScheduleView.model_validate(schedule).model_dump()
        result.update(projection)
        return result

    @api.patch(
        "/schedules/{schedule_id}",
        response_model=ScheduleMutationView,
        response_model_exclude_none=True,
    )
    async def api_patch_schedule(
        schedule_id: int,
        payload: SchedulePatch,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Any:
        repo = ScheduleRepository(db, active_workflow_registry(request))
        schedule = repo.get(schedule_id)
        changes = payload.model_dump(exclude_unset=True)
        future_mode = str(changes.get("mode", schedule.mode))
        supplied_config = changes.get("workflow_config")
        supplied_codes = changes.get("marketplace_codes")
        if supplied_config is not None or supplied_codes is not None or "mode" in changes:
            effective_config = (
                dict(supplied_config)
                if supplied_config is not None
                else dict(schedule.workflow_config or {})
            )
            # Legacy clients only know marketplace_codes.  Treat that field as
            # an update to the canonical payout config rather than comparing it
            # with the old snapshot and rejecting every real change.
            if supplied_config is None and supplied_codes is not None:
                effective_config["marketplace_codes"] = list(supplied_codes)
            config, version, codes = normalise_workflow_input(
                request,
                workflow=schedule.workflow,
                mode=future_mode,
                workflow_config=effective_config,
                marketplace_codes=(
                    list(supplied_codes)
                    if supplied_codes is not None
                    else list(schedule.marketplace_codes or [])
                ),
            )
            changes.update(
                workflow_config=config,
                workflow_config_version=version,
                marketplace_codes=codes,
            )
        schedule = repo.update(schedule_id, **changes)
        db.commit()
        projection = await schedule_mutation_projection(
            request,
            schedule_id=schedule.id,
            expects_projection=bool(schedule.enabled),
            durable_action="排期修改已保存",
        )
        result = ScheduleView.model_validate(schedule).model_dump()
        result.update(projection)
        return result

    @api.delete(
        "/schedules/{schedule_id}",
        response_model=ScheduleDeleteView,
        response_model_exclude_none=True,
    )
    async def api_delete_schedule(
        schedule_id: int,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        ScheduleRepository(db).delete(schedule_id)
        # Commit before rebuilding the process-local projection.  SQLite is
        # authoritative; even if an old callback is already queued it will
        # find no schedule and perform no browser/financial action.
        db.commit()
        projection = await schedule_mutation_projection(
            request,
            schedule_id=schedule_id,
            expects_projection=False,
            durable_action="排期已删除",
        )
        return {
            "status": "deleted",
            "schedule_id": schedule_id,
            **projection,
        }

    @api.post("/schedules/{schedule_id}/run-now", status_code=202)
    async def api_run_schedule_now(
        schedule_id: int,
        request: Request,
        operator: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        del db  # ScheduleManager owns the short atomic creation transaction.
        service = request.app.state.automation_service
        if isinstance(service, UnconfiguredAutomationService):
            raise HTTPException(503, "自动化运行服务尚未配置")
        manager = request.app.state.schedule_manager
        if manager is None:
            # Tests and embedded deployments may inject only the runtime
            # service.  This manager is not started and therefore creates no
            # APScheduler jobs; it only reuses the guarded run-now operation.
            from .scheduler import ScheduleManager

            manager = ScheduleManager(
                request.app.state.sessions,
                service,
                workflow_registry=active_workflow_registry(request),
            )
        try:
            run_id = await manager.run_now(schedule_id, requested_by=operator.username)
        except ConflictError:
            raise
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {
            "status": "queued",
            "run_id": run_id,
            "redirect": f"/runs/{run_id}",
        }

    @api.post("/runs", response_model=RunView, status_code=202)
    async def api_create_run(
        payload: RunCreate,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> Any:
        if isinstance(request.app.state.automation_service, UnconfiguredAutomationService):
            raise HTTPException(503, "自动化运行服务尚未配置")
        store = StoreRepository(db).get(payload.store_id)
        requested_input = payload.marketplace_codes
        registry = active_workflow_registry(request)
        # 未注册的流程留给 normalise_workflow_input 按 422 拒绝。
        if (
            requested_input is None
            and not payload.workflow_config
            and payload.workflow in registry.names()
        ):
            # 没指定站点时取「已启用且这个流程能跑」的那些。只取「已启用」的话，
            # 迁移 0010 给每家店补的 DE 会让提现/反馈的「发起检查」一律 422。
            supported = registry.definition(payload.workflow).supported_marketplace_codes()
            requested_input = [
                m.code for m in store.marketplaces if m.enabled and m.code in supported
            ]
            if supported and not requested_input:
                # 发起检查的对话框里没有站点选择框,「请至少选择一个站点」让人无从下手。
                raise HTTPException(
                    422,
                    "该店铺没有启用这个流程支持的站点（支持："
                    + "、".join(sorted(supported))
                    + "），请先在店铺设置里启用",
                )
        config, version, codes = normalise_workflow_input(
            request,
            workflow=payload.workflow,
            mode=payload.mode,
            workflow_config=payload.workflow_config,
            marketplace_codes=(list(requested_input) if requested_input is not None else None),
        )
        requested = set(codes)
        enabled = {m.code for m in store.marketplaces if m.enabled}
        definition = active_workflow_registry(request).definition(payload.workflow)
        if definition.requires_marketplace_targets and (
            not requested or not requested.issubset(enabled)
        ):
            raise HTTPException(422, "请选择该店铺已启用的站点")
        workflow_repo = WorkflowRepository(db, active_workflow_registry(request))
        run = workflow_repo.create_run(
            store_id=payload.store_id,
            workflow=payload.workflow,
            mode=payload.mode,
            workflow_config=config,
            workflow_config_version=version,
        )
        run.result_summary = {"requested_marketplaces": sorted(requested)}
        db.commit()
        try:
            await request.app.state.automation_service.enqueue_run(run.id)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return workflow_repo.get_run(run.id, full=True)

    @api.get("/runs/{run_id}", response_model=RunView)
    def api_get_run(run_id: str, db: Session = Depends(get_session)) -> Any:
        run = WorkflowRepository(db).get_run(run_id, full=True)
        _attach_queue_projection(db, [run])
        return run

    @api.post("/runs/{run_id}/approve", status_code=202)
    async def api_approve_run(
        run_id: str,
        request: Request,
        operator: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, str]:
        run = WorkflowRepository(db).get_run(run_id)
        approval = db.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.run_id == run_id, ApprovalRequest.status == "PENDING")
            .order_by(ApprovalRequest.created_at.desc())
        )
        if approval is None:
            raise HTTPException(409, "没有可处理的审批")
        _release_before_automation(db)
        try:
            action = request.app.state.automation_service.approve_run
            try:
                await action(run.id, approval.id, actor=operator.username)
            except TypeError:
                # Small injected test doubles may still expose the V1 shape.
                await action(run.id, approval.id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @api.post("/runs/{run_id}/continue-auth", status_code=202)
    async def api_continue_auth(run_id: str, request: Request, _: LocalOperator = Depends(verified_operator), db: Session = Depends(get_session)) -> dict[str, str]:
        WorkflowRepository(db).get_run(run_id)
        _release_before_automation(db)
        try:
            await request.app.state.automation_service.continue_auth(run_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @api.post("/runs/{run_id}/resume-inventory", status_code=202)
    async def api_resume_inventory_run(
        run_id: str,
        request: Request,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, str]:
        run = WorkflowRepository(db).get_run(run_id)
        if not can_resume_inventory_run(db, run):
            raise HTTPException(409, "当前任务不符合库存清单继续条件，请查看任务状态")
        # The service re-checks under its transaction before queueing this same run.
        _release_before_automation(db)
        try:
            await request.app.state.automation_service.resume_inventory_run(run_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @api.post("/runs/{run_id}/cancel", status_code=202)
    async def api_cancel_run(run_id: str, request: Request, _: LocalOperator = Depends(verified_operator), db: Session = Depends(get_session)) -> dict[str, str]:
        WorkflowRepository(db).get_run(run_id)
        _release_before_automation(db)
        try:
            await request.app.state.automation_service.cancel_run(run_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @api.post("/runs/{run_id}/guards/{guard_key}/release", status_code=200)
    async def api_release_guard(run_id: str, guard_key: str, _: LocalOperator = Depends(verified_operator), db: Session = Depends(get_session)) -> dict[str, str]:
        """Clear a money guard for which no dispatch was ever recorded.

        The only escape from an operation that armed but never reached the
        payout click.  Without it such a row blocks its site and, on every
        read-back, reports a payout that was never requested.

        ``submitted_at`` is the hard boundary: it is stamped only after the sole
        irreversible click returns, so a row that has it can never be released
        here.  The delete is conditional on the same predicate, so a guard that
        reaches SUBMITTED between this check and the write survives.
        """

        repository = WorkflowRepository(db)
        repository.get_run(run_id)
        guard = db.scalar(
            select(OperationGuard).where(
                OperationGuard.run_id == run_id,
                OperationGuard.guard_key == guard_key,
            )
        )
        if guard is None:
            raise HTTPException(404, "找不到该资金记录")
        if guard.submitted_at is not None:
            raise HTTPException(409, "该资金记录已派发过提现点击，不可释放")
        if guard.state not in ("ARMED", "UNCERTAIN"):
            raise HTTPException(409, f"资金记录当前为 {guard.state}，不可释放")
        # ⚠️ 任务还在跑时,ARMED 可能正处在「已锁定、点击还没返回」的那十几秒里。
        # 这时删掉记录,点击照样会发出去,而它唯一的记录已经没了 —— 之后连
        # 「已提交」都写不进去(09-23 自审复核)。等任务结束再裁定。
        run_status = db.scalar(select(Run.status).where(Run.id == run_id))
        if run_status in ACTIVE_RUN_STATUSES:
            raise HTTPException(
                409,
                f"任务还在进行中（{run_status}），这一笔可能正在提交；等任务结束后再裁定",
            )
        marketplace_code, amount, currency = (
            guard.marketplace_code,
            guard.amount,
            guard.currency,
        )
        deleted = db.execute(
            delete(OperationGuard).where(
                OperationGuard.id == guard.id,
                OperationGuard.submitted_at.is_(None),
            )
        ).rowcount
        if not deleted:
            db.rollback()
            raise HTTPException(409, "资金记录状态已变化，未执行释放")
        # 释放 = 操作员断定这一下没点下去。站点行留在「已锁定 / 待人工核对」的话,
        # 之后每一张卡都还把它算成待办(2026-09-26 自审)。
        _sync_site_after_verdict(db, run_id, marketplace_code, "CANCELLED")
        repository.append_event(
            run_id,
            "operation_released",
            message=(
                f"操作员确认 {marketplace_code} 从未发出提现请求，已释放资金锁定；"
                "该站点可重新尝试"
            ),
            details={
                "guard_key": guard_key,
                "marketplace_code": marketplace_code,
                "amount": str(amount),
                "currency": currency,
                "released_by": "operator",
            },
        )
        db.commit()
        return {"status": "released", "marketplace_code": marketplace_code}

    @api.post("/runs/{run_id}/guards/{guard_key}/acknowledge", status_code=200)
    async def api_acknowledge_guard(run_id: str, guard_key: str, _: LocalOperator = Depends(verified_operator), db: Session = Depends(get_session)) -> dict[str, str]:
        """Close out a dispatched operation after a person checked Seller Central.

        The mirror image of ``release``.  That one covers "the click never
        happened"; this one covers "the click happened".  The automatic
        read-back that used to try this was removed on 2026-09-23 (Amazon often
        shows a disbursement only the next day, so a one-minute read-back
        structurally could not close such a row).

        The verdict here is the human's, not the system's: only a person who
        has looked at Seller Central knows.  ``receipt_id`` is therefore left
        empty rather than fabricated, and the event records who decided.

        This is a state transition, never a delete.  The ``submitted_at``
        boundary is untouched: a row that recorded a dispatch remains
        permanently undeletable, exactly as before.
        """

        repository = WorkflowRepository(db)
        repository.get_run(run_id)
        guard = db.scalar(
            select(OperationGuard).where(
                OperationGuard.run_id == run_id,
                OperationGuard.guard_key == guard_key,
            )
        )
        if guard is None:
            raise HTTPException(404, "找不到该资金记录")
        # SUBMITTED 也要放行:2026-09-23 删掉联网回读之后,已派发的记录停在
        # SUBMITTED 而不再转成 UNCERTAIN。只认 UNCERTAIN 的话,新记录永远没有
        # 人工出口,而那 43 笔存量(UNCERTAIN)仍然走得通。
        #
        # ⚠️ **不看 ``submitted_at``**。它为空只说明「系统没记录到点击返回」——
        # 点击之后、写 SUBMITTED 之前服务被停掉,就是这个样子,而钱可能已经发出去了。
        # 原来这里要求它非空,于是操作员在卖家后台看到了这笔,页面却只给
        # 「确认未发出，释放」—— 一个与事实相反的选项(09-23 自审复核)。
        # 结案只是改状态、不删行;释放不接受 CONFIRMED,所以不会弱化「已派发不可删」。
        # ARMED 分两种:任务还在跑 —— 那一下可能正在点,不许替它下结论;任务已经
        # 结束(例如点击之后流程抛了异常,运行停在 UNCERTAIN_FINANCIAL)—— 它就是
        # 一条「说不清」的记录,和 UNCERTAIN 一样由人凭卖家后台裁定。原来这里一律
        # 拒绝、叫人「等收口」,而收口只在重启时才跑,操作员手上只剩「释放」
        # (09-23 第二轮复核)。
        if guard.state == "ARMED":
            run_status = db.scalar(select(Run.status).where(Run.id == run_id))
            if run_status in ACTIVE_RUN_STATUSES:
                raise HTTPException(
                    409,
                    f"任务还在进行中（{run_status}），这一笔可能正在提交；等任务结束后再核对",
                )
        elif guard.state not in ("SUBMITTED", "UNCERTAIN"):
            raise HTTPException(409, f"资金记录当前为 {guard.state}，无需人工裁定")
        marketplace_code, amount, currency = (
            guard.marketplace_code,
            guard.amount,
            guard.currency,
        )
        # Merge, never replace: the existing metadata is the audit trail this
        # decision was made against.
        merged = dict(guard.metadata_json or {})
        merged["closed_by"] = "operator"
        changed = repository.transition_guard(
            guard.id,
            expected_states=("ARMED", "SUBMITTED", "UNCERTAIN"),
            to_state="CONFIRMED",
            metadata=merged,
        )
        if not changed:
            db.rollback()
            raise HTTPException(409, "资金记录状态已变化，未执行确认")
        # 站点行也要跟着走。只改 guard 的话,运行结束后站点徽章仍写「资金结果待人工
        # 核对」、卡片仍把它列成「待人工核对」,而人早就裁定完了(2026-09-26 自审)。
        _sync_site_after_verdict(db, run_id, marketplace_code, "CONFIRMED")
        repository.append_event(
            run_id,
            "operation_acknowledged",
            message=(
                f"操作员在亚马逊卖家后台人工核对 {marketplace_code} 的转账确实发生，"
                "本条资金记录结案"
            ),
            details={
                "guard_key": guard_key,
                "marketplace_code": marketplace_code,
                "amount": str(amount),
                "currency": currency,
                "closed_by": "operator",
            },
        )
        db.commit()
        return {"status": "acknowledged", "marketplace_code": marketplace_code}

    @api.post("/feedback-reviews/{review_id}/decision", status_code=200)
    async def api_decide_feedback_reason(
        review_id: str,
        payload: FeedbackDecisionInput,
        _: LocalOperator = Depends(verified_operator),
        db: Session = Depends(get_session),
    ) -> dict[str, str]:
        """Let an operator pick the reason the classifier declined to choose.

        This only records the decision.  Nothing is submitted here — the entry
        moves to ``PENDING`` and the next scheduled run of that store submits
        it, under the same per-run cap and the same one-shot guarantee.
        """

        review = db.get(FeedbackReview, review_id)
        if review is None:
            raise HTTPException(404, "找不到这条反馈记录")
        if review.state in FEEDBACK_TERMINAL_STATES:
            # Already submitted; a second request is exactly what must never
            # happen, so this is refused rather than quietly ignored.
            raise HTTPException(
                409, f"这条反馈已经是「{review.state}」，不能再改原因或重新提交"
            )
        if not is_known_reason(payload.category, payload.reason_code):
            raise HTTPException(422, "请求原因不在亚马逊提供的选项里")

        review.category = payload.category
        review.reason_code = payload.reason_code
        review.decision_source = "human"
        review.decision_note = reason_label(payload.category, payload.reason_code)
        review.state = FEEDBACK_PENDING
        db.commit()
        return {
            "status": FEEDBACK_PENDING,
            "category": payload.category,
            "reason_code": payload.reason_code,
        }

    @api.post("/runs/{run_id}/settle", status_code=200)
    async def api_settle_run(run_id: str, _: LocalOperator = Depends(verified_operator), db: Session = Depends(get_session)) -> dict[str, str]:
        """End a run whose money questions have all been answered by hand.

        ``UNCERTAIN_FINANCIAL`` had no way out. Read-back can reach a terminal
        status only if Amazon has published the transfer, which it often does
        not do until the next day, and the two per-guard actions — release and
        acknowledge — settle a guard row without ever writing ``Run.status``.
        So an operator could resolve every last funds question and still be left
        with a run pinned open, blocking its schedule from creating anything new.

        Cancelling is not the answer and stays refused: a run that armed an
        operation must never be written off as if nothing had happened. This
        instead reads the outcome back off the guards the operator just settled.
        """

        repository = WorkflowRepository(db)
        run = repository.get_run(run_id, full=True)
        if run.status != "UNCERTAIN_FINANCIAL":
            raise HTTPException(
                409, f"任务当前为 {run.status}，只有「资金结果待确认」的任务需要人工收尾"
            )
        guards = list(
            db.scalars(select(OperationGuard).where(OperationGuard.run_id == run_id))
        )
        # 只有「说不清点没点下去」的记录(ARMED/UNCERTAIN)还需要人裁定。
        # SUBMITTED 在 2026-09-23 删掉回读之后是成功终态:那一下点击返回过。
        unsettled = sorted(
            {
                guard.marketplace_code
                for guard in guards
                if guard.state in {state.value for state in UNSETTLED_GUARD_STATES}
            }
        )
        if unsettled:
            raise HTTPException(
                409,
                "还有资金记录没有裁定：" + "、".join(unsettled)
                + "。请先去亚马逊卖家后台核对，再按结果选择"
                "「确认未发出，释放」/「我已在亚马逊核对，结案」。",
            )
        # 释放会删掉记录,所以留下来的就是**确实发出去了**的那几笔:
        # 系统看到点击返回的(SUBMITTED)和人工结案的(CONFIRMED)。
        # 终态和引擎收口用**同一个**判据 —— 原来这里按「已派发条数 == 站点行数」算,
        # 一个因余额为零被跳过的站点就让全额付清的运行变成 PARTIAL,而引擎对
        # 同一组事实报 SUCCEEDED(09-23 自审复核)。
        dispatched = [
            guard for guard in guards if guard.state in {"SUBMITTED", "CONFIRMED"}
        ]
        site_count = len(run.site_runs)
        if not dispatched:
            status = "FAILED"
        else:
            status = run_status_from_facts(
                [(guard.marketplace_code, guard.state) for guard in guards],
                {site.marketplace_code: site.status for site in run.site_runs},
            ).value
        changed = repository.set_run_status(
            run_id, status, allowed_from=("UNCERTAIN_FINANCIAL",)
        )
        if not changed:
            db.rollback()
            raise HTTPException(409, "任务状态已变化，未执行收尾")
        # 和引擎收口一样:还在等 / 还没轮到的站点,就是本次没做的站点。
        for site in run.site_runs:
            if site.status in _PARKED_SITE_VALUES:
                site.status = "CANCELLED"
                site.error = "任务中断（服务重启或人工收尾），该站点本次未执行"
        repository.append_event(
            run_id,
            "run_settled_by_operator",
            message=(
                f"操作员在每条资金记录都已裁定后人工收尾，任务标记为 {status}；"
                "系统没有重新提交任何转账，也没有删除任何资金记录"
            ),
            details={
                "closed_by": "operator",
                "dispatched_guards": len(dispatched),
                "site_runs": site_count,
            },
        )
        db.commit()
        return {"status": status}

    app.include_router(api)
    return app


def _sync_site_after_verdict(
    db: Session, run_id: str, marketplace_code: str, verdict: str
) -> None:
    """人工裁定完一条资金记录之后,把它那一行站点状态也带到终态。

    ``verdict`` 只有两种:``CONFIRMED``(结案:确实发出去了)、``CANCELLED``(释放:
    确认没发出)。只动**还停在资金态**的站点行 —— 已经是别的终态(例如被跳过)的不碰。
    """

    site = db.scalar(
        select(SiteRun).where(
            SiteRun.run_id == run_id, SiteRun.marketplace_code == marketplace_code
        )
    )
    if site is None or site.status not in _MONEY_PENDING_SITE_VALUES:
        return
    site.status = verdict
    site.error = (
        "操作员在亚马逊卖家后台核对后结案" if verdict == "CONFIRMED"
        else "操作员确认未发出，已释放资金锁定；本次未提现"
    )


# 还挂着「钱没有结论」的站点态。人工裁定之后它们必须跟着走。
_MONEY_PENDING_SITE_VALUES = frozenset({"ARMED", "SUBMITTED", "UNCERTAIN_FINANCIAL"})
# 收尾时「还在等 / 还没轮到」的站点(与 engine._PARKED_SITE_STATUSES 同一组)。
_PARKED_SITE_VALUES = frozenset(
    {"PENDING", "PREFLIGHT", "PLANNED", "WAITING_APPROVAL", "WAITING_AUTH"}
)

def _safe_probe_error(exc: BaseException) -> str:
    """One short line for the operator, with anything secret-shaped removed.

    A failing Feishu call can carry the tenant token or the full request URL in
    its message.  The console is loopback-only, but this text also lands in the
    log, and the project's rule is that secrets never reach disk.
    """

    text = " ".join(str(exc).split())
    text = re.sub(
        r'''(?ix)
        (?<![\w])
        ["']?(?P<name>app_secret|password|token|secret)["']?
        (?:\s*[:=]\s*|\s+)
        (?:
            "(?:\\.|[^"\\])*"?
            |'(?:\\.|[^'\\])*'?
            |(?:(?!\s+(?:app_secret|password|token|secret)(?:\s*[:=]\s*|\s+)).)+?
             (?=
                \s+(?:app_secret|password|token|secret)(?:\s*[:=]\s*|\s+)
                |[,;}\]]
                |$
             )
        )
        ''',
        lambda match: f"{match.group('name')}=***",
        text,
    )
    text = text[:300]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _set_session_cookies(response: Response, token: str, csrf: str, settings: Settings) -> None:
    max_age = settings.session_hours * 3600
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True,
        secure=settings.cookie_secure, samesite="strict", path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, httponly=False,
        secure=settings.cookie_secure, samesite="strict", path="/",
    )


def _port_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False
