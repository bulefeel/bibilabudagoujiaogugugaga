"""通知通道写进日志文件的内容:只有标识和状态,**没有店铺名**。

店铺名就是卖家名(见 ``webdriver_mode.start_webdriver_mode`` 的说明),属于
「绝不进 data/logs/*.jsonl」的那一类。卡片标题里带着它 ——
「紫鸟提现 · <店铺名> · 提现已提交」—— 而 ``LoggingNotifier`` 原来把标题原样记了下来,
2026-09-23 自审复核在线上日志里读到了真实的店铺名。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.notifications import (
    CompositeNotifier,
    DatabaseNotificationAdapter,
    LoggingNotifier,
    NotificationDeliveryService,
    NotificationKind,
    SafeRunNotice,
)
from ziniao_automation.workflows.types import RunStatus, WorkflowReport

from test_database_notifications import seed  # tests/unit 不是包,按 rootdir 同目录导入


STORE_NAME = "加拿大示例店"  # seed() 建的店铺名


@pytest.fixture()
def database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'notices.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    yield make_session_factory(engine)
    engine.dispose()


class _Collect:
    def __init__(self) -> None:
        self.notices: list[SafeRunNotice] = []

    async def send(self, notice: SafeRunNotice) -> None:
        self.notices.append(notice)


@pytest.mark.asyncio
async def test_no_notification_writes_the_store_name_to_the_log(database, caplog) -> None:
    """走生产里那条链(``CompositeNotifier((LoggingNotifier(), 卡片通道))``),
    发一张真的「提现已提交」卡,再看日志记录里有没有店铺名。"""

    run_id, _ = seed(database, status="SUCCEEDED", guard_state="SUBMITTED")
    cards = _Collect()
    sender = CompositeNotifier((LoggingNotifier(), cards))
    adapter = DatabaseNotificationAdapter(NotificationDeliveryService(database, sender))

    with caplog.at_level(logging.DEBUG):
        await adapter.send(
            WorkflowReport(run_id, RunStatus.SUCCEEDED, "ignored", "ignored")
        )

    payout = [n for n in cards.notices if n.kind is NotificationKind.PAYMENT_SUBMITTED]
    assert payout and STORE_NAME in payout[0].title, "前提:卡片标题里确实有店铺名"
    logged = [record.getMessage() for record in caplog.records]
    assert any("workflow_notification" in line for line in logged), "通知本身要留痕"
    for line in logged:
        assert STORE_NAME not in line, line


@pytest.mark.asyncio
async def test_a_legacy_report_is_logged_without_its_title(caplog) -> None:
    report = WorkflowReport(
        "run-abcdef123456", RunStatus.FAILED, f"紫鸟提现 · {STORE_NAME} · 任务失败", "x"
    )

    with caplog.at_level(logging.INFO):
        await LoggingNotifier().send(report)

    (line,) = [record.getMessage() for record in caplog.records]
    assert STORE_NAME not in line
    assert "run-abcd" in line and "FAILED" in line
