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
    # No password is loaded into Settings, logs or SQLite. If Credential
    # Manager was cleared after configuration, keep the console available and
    # let sync return an actionable error instead of failing ASGI startup.
    try:
        return build_controller(
            host=settings.ziniao_host,
            port=settings.ziniao_port,
            client_path=settings.ziniao_executable,
            company=company or "",
            username=username or "",
            credential_ref=credential_ref,
        )
    except Exception as exc:
        if type(exc).__name__ != "CredentialStoreError":
            raise
        logger.warning(
            "Ziniao credential reference is missing; using signed-in desktop session metadata"
        )
        # Some Ziniao versions accept the already signed-in desktop session
        # without replaying the account password.  Keep the non-secret account
        # metadata so sync/startBrowser can still work after Credential Manager
        # was cleared; the API response remains authoritative.
        return build_controller(
            host=settings.ziniao_host,
            port=settings.ziniao_port,
            client_path=settings.ziniao_executable,
            company=company or "",
            username=username or "",
        )


def _build_notifier(
    session_factory: sessionmaker[Session],
) -> tuple[Any, NotificationDeliveryService | None]:
    """Build logging plus optional Feishu App notification channels."""
    from .models import SystemSetting

    with session_factory() as session:
        row = session.get(SystemSetting, "feishu")
        metadata = dict(row.value) if row and row.value else {}
    credential_ref = str(metadata.get("credential_ref", "")).strip()
    if not credential_ref or not metadata.get("enabled", False):
        return LoggingNotifier(), None

    def provider() -> dict[str, str] | None:
        try:
            secret = read_generic_credential(credential_ref)
        except Exception:
            logger.error("Feishu credential reference could not be resolved")
            return None
        secret.setdefault("app_id", str(metadata.get("app_id", "")))
        secret.setdefault("chat_id", str(metadata.get("chat_id", "")))
        return secret

    safe_sender = CompositeNotifier((LoggingNotifier(), FeishuNotifier(provider)))
    deliveries = NotificationDeliveryService(session_factory, safe_sender)
    return DatabaseNotificationAdapter(deliveries), deliveries

