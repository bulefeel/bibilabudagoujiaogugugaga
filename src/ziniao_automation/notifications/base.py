"""Notification interfaces and composable implementations."""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable, TYPE_CHECKING

from .dto import SafeRunNotice

if TYPE_CHECKING:  # pragma: no cover
    from ..workflows.types import WorkflowReport

logger = logging.getLogger(__name__)

class NullNotifier:
    async def send(self, report: "SafeRunNotice | WorkflowReport") -> None:
        del report


class LoggingNotifier:
    """Log identifiers/status only; never serialise site data or secrets."""

    async def send(self, report: "SafeRunNotice | WorkflowReport") -> None:
        if isinstance(report, SafeRunNotice):
            logger.info(
                "workflow_notification run_id=%s status=%s title=%s",
                report.run_short_id,
                report.kind.value,
                report.title,
            )
            return
        logger.info(
            "workflow_notification run_id=%s status=%s title=%s",
            report.run_id,
            report.status.value,
            report.title,
        )


class CompositeNotifier:
    def __init__(self, notifiers: Iterable[object]) -> None:
        self.notifiers = tuple(notifiers)

    async def send(self, report: "SafeRunNotice | WorkflowReport") -> None:
        results = await asyncio.gather(
            *(notifier.send(report) for notifier in self.notifiers),
            return_exceptions=True,
        )
        failures = [item for item in results if isinstance(item, Exception)]
        if failures:
            raise RuntimeError(f"{len(failures)} 个通知通道发送失败") from failures[0]
