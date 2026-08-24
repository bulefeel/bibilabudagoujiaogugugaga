"""Production composition root and process resource lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import ZiniaoAccount
from .notifications import (
    CompositeNotifier,
    DatabaseFeishuCredentialProvider,
    DatabaseNotificationAdapter,
    FeishuNotifier,
    LoggingNotifier,
    NotificationDeliveryService,
)
from .scheduler import ScheduleManager
from .workflows import (
    AutomationService,
    DatabaseRunLoader,
    SqlAlchemyWorkflowRepository,
    WorkflowEngine,
    WorkflowRegistry,
)
from .workflows.amazon_disbursement import (
    AmazonDisbursementWorkflow,
    AmazonPaymentsPage,
)
from .ziniao.factory import build_controller
from .ziniao.credentials import read_generic_credential
from .ziniao.errors import ZiniaoCredentialError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeComposition:
    """The one process-wide object graph.

    Keeping the controller singleton is important: its launch, store and funds
    locks are the V1 concurrency boundary.
    """

    settings: Settings
    session_factory: sessionmaker[Session]
    controller: Any
    workflow_repository: Any
    workflow_engine: Any
    run_loader: Any
    automation_service: Any
    schedule_manager: ScheduleManager
    _started: bool = False
    _closed: bool = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        # The loader selects only ARMED/SUBMITTED/UNCERTAIN and calls
        # reconcile(), never start()/execute().  Recovery is awaited before
        # schedules become live so no due job can overtake it.
        await self.automation_service.recover_startup()
        await self.schedule_manager.start()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Stop new triggers, wait for in-process work to settle, then release
        # auth leases, CDP handles, Playwright and the local HTTP client.
        await self.schedule_manager.shutdown(wait=False)
        await self.automation_service.wait_idle()
        shutdown_worker = getattr(self.automation_service, "shutdown_worker", None)
        if callable(shutdown_worker):
            await shutdown_worker()
        shutdown_probes = getattr(
            self.automation_service, "shutdown_identity_probes", None
        )
        if callable(shutdown_probes):
            await shutdown_probes()
        await self.controller.close()


def build_runtime(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    controller: Any | None = None,
    notifier: Any | None = None,
    scheduler: Any | None = None,
) -> RuntimeComposition:
    """Wire the fixed V1 allow-list without dynamic imports or script upload."""

    controller = controller or _build_controller_from_database(settings, session_factory)
    workflow_repository = SqlAlchemyWorkflowRepository(session_factory)
    workflow = AmazonDisbursementWorkflow(
        repository=workflow_repository,
        page_adapter=AmazonPaymentsPage(),
    )
    registry = WorkflowRegistry((workflow,))
    if notifier is None:
        notification_adapter, delivery_service = _build_notifier(session_factory)
    else:
        notification_adapter, delivery_service = notifier, None
    workflow_engine = WorkflowEngine(
        registry=registry,
        repository=workflow_repository,
        browser_sessions=controller,
        notifier=notification_adapter,
        auth_timeout_seconds=float(settings.auth_wait_minutes * 60),
    )
    run_loader = DatabaseRunLoader(
        session_factory,
        artifact_root=settings.evidence_dir,
    )
    automation_service = AutomationService(
        engine=workflow_engine,
        run_loader=run_loader,
        recovery_loader=run_loader.recovery_runs,
        ziniao_controller=controller,
        session_factory=session_factory,
        identity_auth_timeout_seconds=float(settings.auth_wait_minutes * 60),
    )
    automation_service.set_delivery_service(delivery_service)
    # The worker uses the same persistent service for cross-day and account
    # conflict events that do not originate from an engine terminal status.
    workflow_engine.delivery_service = delivery_service
    schedule_manager = ScheduleManager(
        session_factory,
        automation_service,
        scheduler=scheduler,
    )
    return RuntimeComposition(
        settings=settings,
        session_factory=session_factory,
        controller=controller,
        workflow_repository=workflow_repository,
        workflow_engine=workflow_engine,
        run_loader=run_loader,
        automation_service=automation_service,
        schedule_manager=schedule_manager,
    )


def _build_controller_from_database(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> Any:
    with session_factory() as session:
        account = session.scalar(
            select(ZiniaoAccount)
            .where(ZiniaoAccount.enabled.is_(True))
            .order_by(ZiniaoAccount.id)
            .limit(1)
        )
        company = account.company if account else ""
        username = account.username if account else ""
        credential_ref = account.credential_ref if account else None

    def resolve_credentials() -> dict[str, str]:
        """Read the account fresh on every Ziniao call.

        The controller is built once when the process starts, but the operator
        configures the account from the console afterwards — so anything
        captured above is empty on precisely the first run they attempt.  Ziniao
        then answers ``-10003 参数不能为空（登录状态错误）`` and the console blames
        a login that is in fact correct.  Observed 2026-08-24, immediately after
        the credentials page shipped; the same shape as the upgrade that served
        new templates from an old process.

        This resolver is authoritative.  An incomplete snapshot fails before
        any HTTP request instead of mixing a newly selected database account
        with a password captured for the previous account at startup.
        """

        with session_factory() as session:
            row = session.scalar(
                select(ZiniaoAccount)
                .where(ZiniaoAccount.enabled.is_(True))
                .order_by(ZiniaoAccount.id)
                .limit(1)
            )
            if row is None:
                raise ZiniaoCredentialError(
                    "紫鸟账号尚未配置或已停用，请先在系统设置中保存紫鸟账号"
                )
            reference = str(row.credential_ref or "").strip()
            company = str(row.company or "").strip()
            username = str(row.username or "").strip()
        if not reference:
            raise ZiniaoCredentialError(
                "紫鸟凭据引用缺失，请在系统设置中重新保存紫鸟账号"
            )
        try:
            secret = read_generic_credential(reference)
        except Exception as exc:
            raise ZiniaoCredentialError(
                "Windows 凭据中的紫鸟密码不可读取，请在系统设置中重新保存"
            ) from exc

        # Current SQLite metadata identifies the enabled account.  The vault
        # must carry the same identity as well as the password.  Accepting a
        # legacy password-only record here could combine a newly selected DB
        # account with an old account's password; the operator must re-save it
        # once instead of the service ever guessing.
        secret_company = str(secret.get("company") or "").strip()
        secret_username = str(secret.get("username") or "").strip()
        if not secret_company or not secret_username:
            raise ZiniaoCredentialError(
                "Windows中的紫鸟凭据格式过旧或不完整，请在系统设置中重新保存"
            )
        if secret_company != company:
            raise ZiniaoCredentialError(
                "紫鸟账号元数据与Windows凭据不一致，请重新保存紫鸟账号"
            )
        if secret_username != username:
            raise ZiniaoCredentialError(
                "紫鸟账号元数据与Windows凭据不一致，请重新保存紫鸟账号"
            )
        password = str(secret.get("password") or "")
        if not company or not username or not password:
            raise ZiniaoCredentialError(
                "紫鸟凭据不完整，请在系统设置中重新保存公司、账号和密码"
            )
        return {"company": company, "username": username, "password": password}

    # No password is loaded into Settings, logs or SQLite.  The resolver is
    # invoked only when an action is requested, so first-run ASGI startup still
    # succeeds and a credential saved from the web console works immediately.
    return build_controller(
        host=settings.ziniao_host,
        port=settings.ziniao_port,
        client_path=settings.ziniao_executable,
        company=company or "",
        username=username or "",
        credential_ref=credential_ref,
        credential_resolver=resolve_credentials,
        credential_resolver_authoritative=True,
    )


def _build_notifier(
    session_factory: sessionmaker[Session],
) -> tuple[Any, NotificationDeliveryService | None]:
    """Build a persistent channel whose credentials are resolved per send.

    First-run setup happens after this process-wide composition is created.
    Keeping the delivery service alive even before Feishu is configured lets
    the next real workflow notice use settings saved from the web console
    without restarting the service.
    """

    provider = DatabaseFeishuCredentialProvider(session_factory)
    safe_sender = CompositeNotifier((LoggingNotifier(), FeishuNotifier(provider)))
    deliveries = NotificationDeliveryService(session_factory, safe_sender)
    return DatabaseNotificationAdapter(deliveries), deliveries

