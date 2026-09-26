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
    """Log identifiers/status only; never serialise site data or secrets.

    ⚠️ **标题也不许进日志**:卡片标题里有店铺名(= 卖家名),例如
    「紫鸟提现 · <店铺名> · 提现已提交」。这里原来记了 title=,于是每一张卡的
    店铺名都进了 data/logs/*.jsonl(2026-09-23 自审复核在线上日志里读到)。
    """

    async def send(self, report: "SafeRunNotice | WorkflowReport") -> None:
        if isinstance(report, SafeRunNotice):
            logger.info(
                "workflow_notification run_id=%s status=%s workflow=%s",
                report.run_short_id,
                report.kind.value,
                report.workflow,
            )
            return
        logger.info(
            "workflow_notification run_id=%s status=%s",
            str(report.run_id)[:8],
            report.status.value,
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
