"""FastAPI routes for the loopback-only operations console."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import inspect
import logging
from pathlib import Path
import re
import socket
from typing import Any, AsyncIterator, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import AuthService, AuthenticatedAdmin
from .config import Settings
from .db import create_sqlite_engine, database_revision, init_database, make_session_factory
from .models import (
    ApprovalRequest,
    Evidence,
    OperationGuard,
    Run,
    RunQueueEntry,
    SystemSetting,
    ZiniaoAccount,
)
from .presentation_time import format_display_datetime, to_display_datetime
from .repositories import (
    ConflictError,
    DashboardRepository,
    NotFoundError,
    ScheduleRepository,
    StoreRepository,
    WorkflowRepository,
)
from .schemas import (
    BootstrapInput,
    FeishuSettingsInput,
    LoginInput,
    MarketplaceSetupInput,
    RunCreate,
    RunView,
    ScheduleCreate,
    SchedulePatch,
    ScheduleView,
    StoreCreate,
    StorePatch,
    StoreSetupResetView,
    StoreView,
    ZiniaoSettingsInput,
)
from .version import __version__, build_commit
from .ziniao.credentials import (
    DEFAULT_FEISHU_TARGET,
    DEFAULT_ZINIAO_TARGET,
    CredentialStoreError,
    credential_exists,
    credential_matches,
    read_generic_credential,
    write_generic_credential,
)

logger = logging.getLogger(__name__)


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


SESSION_COOKIE = "ziniao_session"
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
    async def cancel_run(self, run_id: str) -> None: ...
    async def reconcile_run(self, run_id: str) -> None: ...


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

    async def enqueue_run(self, run_id: str) -> None:
        raise RuntimeError("自动化运行服务尚未配置")

    async def approve_run(self, run_id: str, approval_id: str | None = None, *, actor: str = "admin") -> None:
        raise RuntimeError("自动化运行服务尚未配置")

    async def continue_auth(self, run_id: str) -> None:
        raise RuntimeError("自动化运行服务尚未配置")

    async def cancel_run(self, run_id: str) -> None:
        raise RuntimeError("自动化运行服务尚未配置")

    async def reconcile_run(self, run_id: str) -> None:
        raise RuntimeError("自动化运行服务尚未配置")


STATUS_LABELS = {
    "QUEUED": "排队中", "RUNNING": "执行中", "WAITING_APPROVAL": "待审核",
    "WAITING_AUTH": "等待人工验证", "RECONCILING": "回读核对",
    "SUCCEEDED": "已完成", "PARTIAL": "部分完成", "FAILED": "失败", "CANCELLED": "已取消",
    "SKIPPED": "已跳过", "NEEDS_HUMAN_AUTH": "需要人工验证",
    "UNCERTAIN_FINANCIAL": "已提交，等待平台审核", "PENDING": "待处理",
    "APPROVED": "已批准", "CONFIRMED": "已确认", "ARMED": "已锁定",
    "SUBMITTED": "已提交", "UNCERTAIN": "已提交，等待平台审核", "INVALIDATED": "已失效",
    "EXPIRED": "已过期",
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
    "RECONCILE": "资金回读",
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
        run.queued_at = entry.enqueued_at if entry else None


def create_app(
    settings: Settings | None = None,
    *,
    automation_service: AutomationService | None = None,
    runtime_factory: Any | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = create_sqlite_engine(settings)
    sessions = make_session_factory(engine)
    package_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(package_dir / "templates"))
    templates.env.filters["status_label"] = lambda value: STATUS_LABELS.get(str(value), str(value))
    templates.env.filters["queue_state_label"] = lambda value: QUEUE_STATE_LABELS.get(str(value), str(value))
    templates.env.filters["queue_action_label"] = lambda value: QUEUE_ACTION_LABELS.get(str(value), str(value))
    templates.env.filters["money"] = lambda value: "—" if value is None else f"{value:,.2f}"
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
        runtime = None
        try:
            if runtime_factory is not None:
                runtime = runtime_factory(settings, sessions)
                app.state.runtime = runtime
                app.state.automation_service = runtime.automation_service
                app.state.schedule_manager = runtime.schedule_manager
                await runtime.start()
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
    app.state.templates = templates
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.mount("/static", StaticFiles(directory=str(package_dir / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
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

    def current_admin(
        request: Request,
        db: Session = Depends(get_session),
    ) -> AuthenticatedAdmin:
        auth = AuthService(db, session_hours=settings.session_hours)
        if not auth.is_initialized():
            raise HTTPException(status_code=428, detail="请先完成管理员初始化")
        admin = auth.validate(request.cookies.get(SESSION_COOKIE))
        if admin is None:
            raise HTTPException(status_code=401, detail="登录已失效")
        return admin

    def verified_admin(
        request: Request,
        x_csrf_token: str | None = Header(None),
        db: Session = Depends(get_session),
    ) -> AuthenticatedAdmin:
        auth = AuthService(db, session_hours=settings.session_hours)
        admin = auth.validate(request.cookies.get(SESSION_COOKIE))
        # The readable CSRF cookie is a delivery channel, not proof.  State
        # changes must echo it in this custom header.
        csrf = x_csrf_token
        if admin is None:
            raise HTTPException(status_code=401, detail="登录已失效")
        if not auth.validate_csrf(request.cookies.get(SESSION_COOKIE), csrf):
            raise HTTPException(status_code=403, detail="页面校验已失效，请刷新后重试")
        return admin

    def render(
        request: Request,
        template: str,
        context: dict[str, Any],
        *,
        db: Session,
        page_title: str,
    ) -> Response:
        auth = AuthService(db, session_hours=settings.session_hours)
        if not auth.is_initialized():
            return RedirectResponse("/setup", status_code=303)
        admin = auth.validate(request.cookies.get(SESSION_COOKIE))
        if admin is None:
            return RedirectResponse("/login", status_code=303)
        common = {
            "request": request,
            "admin": admin,
            "page_title": page_title,
            "active_path": request.url.path,
            **context,
        }
        return templates.TemplateResponse(request=request, name=template, context=common)

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

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, db: Session = Depends(get_session)) -> Response:
        if AuthService(db).is_initialized():
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="setup.html", context={"request": request, "page_title": "首次初始化"}
        )

    @app.post("/auth/bootstrap")
    def bootstrap(payload: BootstrapInput, response: Response, db: Session = Depends(get_session)) -> dict[str, str]:
        try:
            new_session = AuthService(db, session_hours=settings.session_hours).bootstrap(
                payload.username, payload.password
            )
            db.commit()
        except RuntimeError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="管理员已经初始化") from exc
        _set_session_cookies(response, new_session.session_token, new_session.csrf_token, settings)
        return {"next": "/", "username": new_session.username}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, db: Session = Depends(get_session)) -> Response:
        auth = AuthService(db)
        if not auth.is_initialized():
            return RedirectResponse("/setup", status_code=303)
        if auth.validate(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request, name="login.html", context={"request": request, "page_title": "管理员登录"}
        )

    @app.post("/auth/login")
    def login(payload: LoginInput, response: Response, db: Session = Depends(get_session)) -> dict[str, str]:
        new_session = AuthService(db, session_hours=settings.session_hours).authenticate(
            payload.username, payload.password
        )
        if new_session is None:
            raise HTTPException(status_code=401, detail="账号或密码不正确，连续失败将暂时锁定")
        db.commit()
        _set_session_cookies(response, new_session.session_token, new_session.csrf_token, settings)
        return {"next": "/"}

    @app.post("/auth/logout")
    def logout(
        request: Request,
        response: Response,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Response:
        AuthService(db).logout(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")
        return {"next": "/login"}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, db: Session = Depends(get_session)) -> Response:
        data = DashboardRepository(db).summary()
        runs = WorkflowRepository(db).list_runs(limit=8)
        guards = WorkflowRepository(db).list_guards(states=("UNCERTAIN", "ARMED", "SUBMITTED"))
        return render(request, "dashboard.html", {"summary": data, "runs": runs, "guards": guards}, db=db, page_title="今日总览")

    @app.get("/stores", response_class=HTMLResponse)
    def stores_page(request: Request, db: Session = Depends(get_session)) -> Response:
        return render(request, "stores.html", {"stores": StoreRepository(db).list()}, db=db, page_title="店铺账册")

    @app.get("/schedules", response_class=HTMLResponse)
    def schedules_page(request: Request, db: Session = Depends(get_session)) -> Response:
        return render(request, "schedules.html", {"schedules": ScheduleRepository(db).list(), "stores": StoreRepository(db).list(enabled=True)}, db=db, page_title="任务排期")

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, db: Session = Depends(get_session)) -> Response:
        approvals = WorkflowRepository(db).list_pending_approvals()
        return render(request, "approvals.html", {"approvals": approvals}, db=db, page_title="资金审核台")

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, db: Session = Depends(get_session)) -> Response:
        runs = WorkflowRepository(db).list_runs(limit=200)
        _attach_queue_projection(db, runs)
        return render(request, "runs.html", {"runs": runs}, db=db, page_title="运行流水")

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: str, request: Request, db: Session = Depends(get_session)) -> Response:
        run = WorkflowRepository(db).get_run(run_id, full=True)
        _attach_queue_projection(db, [run])
        guards = list(db.scalars(select(OperationGuard).where(OperationGuard.run_id == run_id)))
        return render(request, "run_detail.html", {"run": run, "guards": guards}, db=db, page_title=f"任务 {run.id[:8]}")

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
        _: AuthenticatedAdmin = Depends(current_admin),
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

    api = APIRouter(prefix="/api", dependencies=[Depends(current_admin)])

    @api.post("/settings/feishu", status_code=200)
    async def save_feishu_settings(
        payload: FeishuSettingsInput,
        _: AuthenticatedAdmin = Depends(verified_admin),
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
        _: AuthenticatedAdmin = Depends(verified_admin),
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
        _: AuthenticatedAdmin = Depends(verified_admin),
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
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Put Ziniao into WebDriver mode, which force-closes its open windows.

        Refused outright while a run holds the browser or an ARMED/SUBMITTED
        guard is outstanding — see ``webdriver_mode.describe_blockers``.  The
        service answers 409 in that case and kills nothing.
        """

        starter = getattr(
            request.app.state.automation_service, "start_webdriver_mode", None
        )
        if not callable(starter):
            raise HTTPException(503, "紫鸟运行服务尚未配置")
        _release_before_automation(db)
        try:
            result = await starter()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if not isinstance(result, dict):
            raise HTTPException(502, "紫鸟模式切换返回了无效结构")
        if not result.get("ok"):
            status = 409 if result.get("status") == "busy" else 502
            raise HTTPException(status, str(result.get("message") or "切换失败"))
        return result

    @api.post("/ziniao/sync")
    async def sync_ziniao(
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        _release_before_automation(db)
        try:
            result = await request.app.state.automation_service.sync_ziniao()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        if not isinstance(result, dict):
            raise HTTPException(502, "紫鸟同步服务返回了无效结构")
        profiles = list(result.get("profiles") or [])
        if not profiles:
            return {**result, "created": int(result.get("created", 0)), "updated": int(result.get("updated", 0))}
        for profile in profiles:
            if not isinstance(profile, dict):
                raise HTTPException(502, "紫鸟返回了无效的店铺记录")
            if profile.get("selector_type") not in {"oauth", "id"} or not profile.get("selector_value"):
                raise HTTPException(502, "紫鸟返回了缺少显式环境选择器的店铺记录")
        account = db.scalar(select(ZiniaoAccount).order_by(ZiniaoAccount.id).limit(1))
        if account is None:
            account = ZiniaoAccount(display_name="紫鸟主账号")
            db.add(account)
            db.flush()
        try:
            created, updated = StoreRepository(db).upsert_profiles(
                profiles, account_id=account.id
            )
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                409, "紫鸟店铺标识存在冲突，请重试同步或检查重复环境"
            ) from exc
        account.last_synced_at = datetime.now(timezone.utc)
        account.sync_error = None
        return {**result, "created": created, "updated": updated}

    @api.get("/stores", response_model=list[StoreView])
    def api_stores(db: Session = Depends(get_session)) -> list[Any]:
        return StoreRepository(db).list()

    @api.post("/stores/{store_id}/detect-identity")
    async def api_detect_store_identity(
        store_id: int,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
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
    async def api_continue_identity_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
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
        _: AuthenticatedAdmin = Depends(verified_admin),
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
    async def api_detect_marketplace_setup(
        store_id: int,
        payload: MarketplaceSetupInput,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
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
    async def api_continue_marketplace_setup_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
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
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _marketplace_setup_probe_action(
            request, store_id, probe_id, "cancel_marketplace_setup_probe"
        )

    @api.post("/stores/{store_id}/detect-store-setup")
    async def api_detect_store_setup(
        store_id: int,
        payload: MarketplaceSetupInput,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        detector = getattr(
            request.app.state.automation_service, "detect_store_setup", None
        )
        if not callable(detector):
            raise HTTPException(503, "店铺统一自动建档服务尚未配置")
        try:
            result = await detector(store_id, payload.marketplace_codes)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        waiting = result.get("status") in {"WAITING_AUTH", "CHECKING"}
        return JSONResponse(result, status_code=202 if waiting else 200)

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
    async def api_continue_store_setup_probe(
        store_id: int,
        probe_id: str,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
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
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Response:
        StoreRepository(db).get(store_id)
        return await _marketplace_setup_probe_action(
            request, store_id, probe_id, "cancel_store_setup_probe"
        )

    @api.post("/stores", response_model=StoreView, status_code=201)
    def api_create_store(
        payload: StoreCreate,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Any:
        return StoreRepository(db).create(**payload.model_dump())

    @api.patch("/stores/{store_id}", response_model=StoreView)
    def api_patch_store(
        store_id: int,
        payload: StorePatch,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Any:
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

    @api.delete(
        "/stores/{store_id}/setup", response_model=StoreSetupResetView
    )
    async def api_reset_store_setup(
        store_id: int,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Any:
        repo = StoreRepository(db)
        repo.get(store_id)
        active_check = getattr(
            request.app.state.automation_service, "has_active_store_setup", None
        )
        if not callable(active_check):
            raise HTTPException(503, "店铺建档活动状态检查服务尚未配置")
        try:
            if await active_check(store_id):
                raise ConflictError(
                    "该店铺仍有身份与站点核验窗口正在运行，请先完成或取消检测"
                )
        except ConflictError:
            raise
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

        store, disabled_schedule_ids = repo.reset_setup(store_id)
        # SQLite must become authoritative before the in-memory scheduler is
        # rebuilt. A disabled schedule callback that races after this commit
        # will find enabled=False and create no task.
        db.commit()
        manager = request.app.state.schedule_manager
        if manager is not None:
            for schedule_id in disabled_schedule_ids:
                await manager.refresh_schedule(schedule_id)
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

    @api.post("/schedules", response_model=ScheduleView, status_code=201)
    async def api_create_schedule(
        payload: ScheduleCreate,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Any:
        schedule = ScheduleRepository(db).create(**payload.model_dump())
        db.commit()
        manager = request.app.state.schedule_manager
        if manager is not None:
            await manager.refresh_schedule(schedule.id)
        return schedule

    @api.patch("/schedules/{schedule_id}", response_model=ScheduleView)
    async def api_patch_schedule(
        schedule_id: int,
        payload: SchedulePatch,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Any:
        schedule = ScheduleRepository(db).update(schedule_id, **payload.model_dump(exclude_unset=True))
        db.commit()
        manager = request.app.state.schedule_manager
        if manager is not None:
            await manager.refresh_schedule(schedule.id)
        return schedule

    @api.delete("/schedules/{schedule_id}")
    async def api_delete_schedule(
        schedule_id: int,
        request: Request,
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> dict[str, Any]:
        ScheduleRepository(db).delete(schedule_id)
        # Commit before rebuilding the process-local projection.  SQLite is
        # authoritative; even if an old callback is already queued it will
        # find no schedule and perform no browser/financial action.
        db.commit()
        manager = request.app.state.schedule_manager
        if manager is not None:
            await manager.refresh_schedule(schedule_id)
        return {"status": "deleted", "schedule_id": schedule_id}

    @api.post("/schedules/{schedule_id}/run-now", status_code=202)
    async def api_run_schedule_now(
        schedule_id: int,
        request: Request,
        admin: AuthenticatedAdmin = Depends(verified_admin),
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

            manager = ScheduleManager(request.app.state.sessions, service)
        try:
            run_id = await manager.run_now(schedule_id, requested_by=admin.username)
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
        _: AuthenticatedAdmin = Depends(verified_admin),
        db: Session = Depends(get_session),
    ) -> Any:
        if isinstance(request.app.state.automation_service, UnconfiguredAutomationService):
            raise HTTPException(503, "自动化运行服务尚未配置")
        store = StoreRepository(db).get(payload.store_id)
        requested = set(payload.marketplace_codes or [m.code for m in store.marketplaces if m.enabled])
        enabled = {m.code for m in store.marketplaces if m.enabled}
        if not requested or not requested.issubset(enabled):
            raise HTTPException(422, "请选择该店铺已启用的站点")
        run = WorkflowRepository(db).create_run(
            store_id=payload.store_id, workflow=payload.workflow, mode=payload.mode
        )
        run.result_summary = {"requested_marketplaces": sorted(requested)}
        db.commit()
        try:
            await request.app.state.automation_service.enqueue_run(run.id)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return WorkflowRepository(db).get_run(run.id, full=True)

    @api.get("/runs/{run_id}", response_model=RunView)
    def api_get_run(run_id: str, db: Session = Depends(get_session)) -> Any:
        run = WorkflowRepository(db).get_run(run_id, full=True)
        _attach_queue_projection(db, [run])
        return run

    @api.post("/runs/{run_id}/approve", status_code=202)
    async def api_approve_run(
        run_id: str,
        request: Request,
        admin: AuthenticatedAdmin = Depends(verified_admin),
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
                await action(run.id, approval.id, actor=admin.username)
            except TypeError:
                # Small injected test doubles may still expose the V1 shape.
                await action(run.id, approval.id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @api.post("/runs/{run_id}/continue-auth", status_code=202)
    async def api_continue_auth(run_id: str, request: Request, _: AuthenticatedAdmin = Depends(verified_admin), db: Session = Depends(get_session)) -> dict[str, str]:
        WorkflowRepository(db).get_run(run_id)
        _release_before_automation(db)
        try:
            await request.app.state.automation_service.continue_auth(run_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @api.post("/runs/{run_id}/cancel", status_code=202)
    async def api_cancel_run(run_id: str, request: Request, _: AuthenticatedAdmin = Depends(verified_admin), db: Session = Depends(get_session)) -> dict[str, str]:
        WorkflowRepository(db).get_run(run_id)
        _release_before_automation(db)
        try:
            await request.app.state.automation_service.cancel_run(run_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @api.post("/runs/{run_id}/reconcile", status_code=202)
    async def api_reconcile_run(run_id: str, request: Request, _: AuthenticatedAdmin = Depends(verified_admin), db: Session = Depends(get_session)) -> dict[str, str]:
        repository = WorkflowRepository(db)
        repository.get_run(run_id)
        if not repository.has_financial_guard(run_id):
            raise HTTPException(409, "该任务没有需要回读的资金操作")
        _release_before_automation(db)
        try:
            await request.app.state.automation_service.reconcile_run(run_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @api.post("/runs/{run_id}/guards/{guard_key}/release", status_code=200)
    async def api_release_guard(run_id: str, guard_key: str, _: AuthenticatedAdmin = Depends(verified_admin), db: Session = Depends(get_session)) -> dict[str, str]:
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
    async def api_acknowledge_guard(run_id: str, guard_key: str, _: AuthenticatedAdmin = Depends(verified_admin), db: Session = Depends(get_session)) -> dict[str, str]:
        """Settle a dispatched operation that the read-back can never resolve.

        The mirror image of ``release``.  That one covers "the click never
        happened"; this one covers "the click happened and Amazon never
        published it".  Amazon frequently does not show a disbursement in the
        statements page until the next day, so an automatic read-back bounded
        to about a minute structurally cannot close such a row — it stayed
        UNCERTAIN for ever, kept its run pinned, and no operator action existed
        to end it.

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
        if guard.state != "UNCERTAIN":
            raise HTTPException(409, f"资金记录当前为 {guard.state}，无需人工裁定")
        if guard.submitted_at is None:
            raise HTTPException(
                409, "该资金记录没有派发过提现点击，请改用「确认从未发出」释放"
            )
        marketplace_code, amount, currency = (
            guard.marketplace_code,
            guard.amount,
            guard.currency,
        )
        # Merge, never replace: the existing metadata holds why the read-back
        # gave up, which is the audit trail this decision was made against.
        merged = dict(guard.metadata_json or {})
        merged["closed_by"] = "operator"
        changed = repository.transition_guard(
            guard.id,
            expected_states=("UNCERTAIN",),
            to_state="CONFIRMED",
            metadata=merged,
        )
        if not changed:
            db.rollback()
            raise HTTPException(409, "资金记录状态已变化，未执行确认")
        repository.append_event(
            run_id,
            "operation_acknowledged",
            message=(
                f"操作员在亚马逊后台人工确认 {marketplace_code} 的转账确实发生，"
                "本条资金记录结案；系统本身并未回读到该记录"
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

    app.include_router(api)
    return app


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
