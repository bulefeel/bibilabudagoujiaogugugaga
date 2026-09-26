"""09-23 自审复核抓到的几处通知缺口:一条运行里第二次等人没有卡、空跑卡说「已完成」、
站点为什么失败/跳过没上卡。每条都用真的构建器与真的卡片渲染来读。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import Run, RunEvent, SiteRun
from ziniao_automation.notifications import (
    DatabaseNoticeBuilder,
    NotificationDeliveryService,
    NotificationKind,
    SafeRunNotice,
)
from ziniao_automation.notifications.feishu import _message

from test_database_notifications import seed  # tests/unit 不是包,按 rootdir 同目录导入


@pytest.fixture()
def database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'followups.db').as_posix()}",
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


def _enter(database, run_id: str, status: str) -> None:
    """模拟引擎再一次把运行切进 ``status``(真引擎经仓储写 STATUS_CHANGED 事件)。"""

    with database() as session:
        session.add(
            RunEvent(
                run_id=run_id,
                event_type="STATUS_CHANGED",
                from_status="RUNNING",
                to_status=status,
                message="",
            )
        )
        session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "kind"),
    [
        ("WAITING_AUTH", NotificationKind.WAITING_AUTH),
        ("NEEDS_HUMAN_AUTH", NotificationKind.AUTH_TIMEOUT),
    ],
)
async def test_every_wait_for_a_human_gets_its_own_card(database, status, kind) -> None:
    """多站点运行每个站点各要登录一次。去重键只有「运行+类型」时,第二次起的
    「等待登录验证」全被吞掉,紫鸟窗口在没人知道的情况下等满 30 分钟。

    同一次等待的重试仍然只发一张。
    """

    run_id, _ = seed(database, status=status)
    sender = _Collect()
    service = NotificationDeliveryService(database, sender)

    assert await service.deliver(run_id, kind) is True
    assert await service.deliver(run_id, kind) is False, "同一次等待不重复发"

    _enter(database, run_id, status)
    assert await service.deliver(run_id, kind) is True, "第二次等待要有自己的卡"
    assert len(sender.notices) == 2


@pytest.mark.parametrize(
    "workflow", ["amazon_gpsr_attestation", "amazon_feedback_removal"]
)
def test_a_dry_run_card_never_says_the_work_was_done(database, workflow) -> None:
    """空跑只读取与判定。标题/状态写「安全证明已完成」「反馈处理已完成」会被读成
    「已经提交了」,而那正是空跑存在的意义 —— 提交前让人看一眼。"""

    run_id, _ = seed(database, status="SUCCEEDED")
    with database() as session:
        run = session.get(Run, run_id)
        run.workflow = workflow
        run.mode = "dry_run"
        session.commit()

    notice = DatabaseNoticeBuilder(database).build(
        run_id, NotificationKind.RUN_COMPLETED
    )
    assert notice is not None
    card = _message(notice)

    assert "空跑完成（未提交）" in notice.title
    assert "空跑完成（未提交）" in card
    for claim in ("安全证明已完成", "反馈处理已完成", "无需操作"):
        assert claim not in notice.title and claim not in card, claim


@pytest.mark.parametrize(
    ("event_type", "message"),
    [
        ("platform_duplicate", "亚马逊付款记录已有同日相同金额/结算周期"),
        ("gpsr_canary_failed", "本站点第一条安全证明未能确认落地，已停止该站点后续提交"),
        ("gpsr_panel_stuck", "有一条处理后侧栏未能关闭，已停止该站点后续提交（ASIN 见运行详情）"),
        ("gpsr_nothing_to_submit", "执行阶段重新读页时没有任何候选，未提交任何安全证明"),
        ("gpsr_site_failed", "页面结构不符"),
    ],
)
def test_the_card_carries_why_a_site_ended_the_way_it_did(
    database, event_type, message
) -> None:
    """站点级的失败/跳过原因必须上卡。漏掉的话,原因栏里显示的是计划阶段那句
    「待提交 N 个」,或者干脆是空的 —— 而卡片正文还写着「原因见下方」。"""

    run_id, site_id = seed(database, status="SKIPPED")
    with database() as session:
        session.get(SiteRun, site_id).status = "SKIPPED"
        session.add(
            RunEvent(
                run_id=run_id,
                site_run_id=site_id,
                event_type=event_type,
                message=message,
            )
        )
        session.commit()

    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.RUN_SKIPPED)

    assert notice is not None
    assert message.split("，")[0] in notice.sites[0].reason


def test_a_wait_lost_to_a_restart_says_so_instead_of_timeout(database) -> None:
    """重启让那次验证作废,不是等超时了。卡片要说真实原因,否则操作员会以为
    是自己动作慢(09-23 第二轮复核)。"""

    run_id, _ = seed(database, status="NEEDS_HUMAN_AUTH")
    with database() as session:
        session.add(
            RunEvent(
                run_id=run_id,
                event_type="AUTH_LEASE_LOST_ON_RESTART",
                from_status="WAITING_AUTH",
                to_status="NEEDS_HUMAN_AUTH",
                message="程序重启后需人工重新确认",
            )
        )
        session.commit()

    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.AUTH_TIMEOUT)

    assert notice is not None
    assert "服务重启" in notice.summary
    assert "等待期限内未完成" not in notice.summary


def test_an_auto_mode_drop_never_claims_a_wait_timed_out(database) -> None:
    """自动模式当场跳过,没有任何等待发生过。说「等待期限内未完成」是假话
    (2026-09-26 自审)。"""

    run_id, site_id = seed(database, status="NEEDS_HUMAN_AUTH")
    with database() as session:
        session.get(SiteRun, site_id).status = "NEEDS_HUMAN_AUTH"
        session.commit()

    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.AUTH_TIMEOUT)

    assert notice is not None
    assert "自动模式不等待人工验证" in notice.summary
    assert "等待期限内未完成" not in notice.summary
    card = _message(notice)
    assert "验证超时" not in card, card


def test_a_real_timeout_still_says_the_wait_ran_out(database) -> None:
    """反向:真的进过 WAITING_AUTH 再超时,照旧说「等待期限内未完成」。"""

    run_id, _ = seed(database, status="NEEDS_HUMAN_AUTH")
    _enter(database, run_id, "WAITING_AUTH")

    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.AUTH_TIMEOUT)

    assert notice is not None and "等待期限内未完成" in notice.summary


def test_a_marketplace_switch_park_is_not_described_as_a_login_problem(database) -> None:
    """停在账户/站点选择页不是登录问题;指人去处理一个没发生的登录毫无意义。"""

    run_id, _ = seed(database, status="WAITING_AUTH")
    with database() as session:
        session.add(
            RunEvent(
                run_id=run_id,
                event_type="human_auth_required",
                message="页面停在亚马逊账户/站点选择页，未能自动选定 CA（not_offered）",
                details={"kind": "marketplace_switch"},
            )
        )
        session.commit()

    notice = DatabaseNoticeBuilder(database).build(run_id, NotificationKind.WAITING_AUTH)

    assert notice is not None
    assert "账户/站点选择页" in notice.summary
    assert "自动登录尝试已停止" not in notice.summary


@pytest.mark.asyncio
async def test_a_card_abandoned_by_a_hard_kill_is_sent_again(database) -> None:
    """进程在发送途中被杀(Stop.bat / 升级在 Windows 上都是硬杀),回执停在 PENDING。
    原来 PENDING 和 SENT 一样被永久当成「已发过」,那张卡 —— 包括红色的资金卡 ——
    再也不会发(2026-09-26 自审)。"""

    from datetime import timedelta

    from ziniao_automation.db import utc_now
    from ziniao_automation.models import NotificationDelivery

    run_id, _ = seed(database, status="UNCERTAIN_FINANCIAL", guard_state="ARMED")
    sender = _Collect()
    service = NotificationDeliveryService(database, sender)

    class _Died(Exception):
        pass

    class _DyingSender:
        async def send(self, notice):
            raise _Died()

    dying = NotificationDeliveryService(database, _DyingSender())
    try:
        await dying.deliver(run_id, NotificationKind.UNCERTAIN_FINANCIAL)
    except _Died:
        pass
    # 硬杀 = 连 FAILED 都来不及写。手工把它摆回那一刻的样子。
    with database() as session:
        row = session.scalar(select_delivery(NotificationDelivery))
        row.status = "PENDING"
        row.last_error = None
        session.commit()

    assert await service.deliver(run_id, NotificationKind.UNCERTAIN_FINANCIAL) is False, (
        "刚挂起的不算被遗弃:正在发送中的不能重复发"
    )
    with database() as session:
        row = session.scalar(select_delivery(NotificationDelivery))
        row.updated_at = utc_now() - timedelta(minutes=30)
        session.commit()

    assert await service.deliver(run_id, NotificationKind.UNCERTAIN_FINANCIAL) is True
    assert len(sender.notices) == 1


def select_delivery(model):
    from sqlalchemy import select

    return select(model).limit(1)
