"""Compatibility adapter from WorkflowEngine reports to database notices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .database import NotificationDeliveryService
from .dto import NotificationKind, SafeRunNotice

if TYPE_CHECKING:  # pragma: no cover
    from ..workflows.types import WorkflowReport


class DatabaseNotificationAdapter:
    """Give WorkflowEngine its legacy ``send(report)`` interface safely.

    Only ``report.run_id`` and ``report.status`` are inspected.  The title,
    summary and free-form fields are ignored; the detailed notification is
    rebuilt from explicit database columns by NotificationDeliveryService.
    A successful run produces exactly one database-backed notice: a green
    payment confirmation when a CONFIRMED guard exists, otherwise a neutral
    task-completed/read-only card.  A run in which every site was skipped gets
    its own neutral card naming each site's reason — staying silent there made
    Amazon's 24-hour disbursement cap indistinguishable from a crash.  Only
    cancellation remains silent, because the operator caused it.
    """

    def __init__(self, deliveries: NotificationDeliveryService) -> None:
        self.deliveries = deliveries

    async def send(self, report: "WorkflowReport | SafeRunNotice") -> None:
        if isinstance(report, SafeRunNotice):
            raise TypeError(
                "DatabaseNotificationAdapter accepts WorkflowReport; "
                "workers should call NotificationDeliveryService for explicit notices"
            )
        # Import lazily to keep ``queue -> notifications`` from initialising
        # workflows -> runtime -> queue while the package is still loading.
        from ..workflows.types import WorkflowReport

        if not isinstance(report, WorkflowReport):
            raise TypeError("DatabaseNotificationAdapter accepts WorkflowReport")
        if report.status.value == "SUCCEEDED":
            # Both calls are intentional.  The database fact checks are
            # mutually exclusive, so exactly one notice can be reserved and
            # sent.  This also keeps a failed green-card delivery from being
            # downgraded to a misleading neutral completion card.
            await self.deliveries.deliver(
                report.run_id, NotificationKind.PAYMENT_CONFIRMED
            )
            await self.deliveries.deliver(
                report.run_id, NotificationKind.RUN_COMPLETED
            )
            return
        kind = _ENGINE_STATUS_KINDS.get(report.status.value)
        if kind is None:
            return
        await self.deliveries.deliver(report.run_id, kind)


_ENGINE_STATUS_KINDS: dict[str, NotificationKind] = {
    "WAITING_APPROVAL": NotificationKind.WAITING_APPROVAL,
    "WAITING_AUTH": NotificationKind.WAITING_AUTH,
    "NEEDS_HUMAN_AUTH": NotificationKind.AUTH_TIMEOUT,
    "FAILED": NotificationKind.RUN_FAILED,
    "PARTIAL": NotificationKind.RUN_PARTIAL,
    "SKIPPED": NotificationKind.RUN_SKIPPED,
    "UNCERTAIN_FINANCIAL": NotificationKind.UNCERTAIN_FINANCIAL,
}


class CompositeEngineNotifier:
    """Pair safe persistent delivery with a non-network diagnostic notifier.

    This is useful for keeping ``LoggingNotifier`` while ensuring the raw
    Feishu notifier is never handed a free-form WorkflowReport directly.
    """

    def __init__(self, database: DatabaseNotificationAdapter, diagnostic: object) -> None:
        self.database = database
        self.diagnostic = diagnostic

    async def send(self, report: "WorkflowReport") -> None:
        # Persistent delivery contains its own failures and never raises for a
        # network error. Keep diagnostic logging independent either way.
        await self.database.send(report)
        sender = getattr(self.diagnostic, "send", None)
        if callable(sender):
            await sender(report)
