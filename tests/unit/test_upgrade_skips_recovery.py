"""升级导致的停机不补跑；别的停机照常补跑。

操作员报的：装完新版打开页面，之前的定时任务自己跑了起来。停服务是安装程序
自己干的（为了避免"新按钮 404"那个更难查的故障），所以那段停机不是操作员选的，
打开应用也就不该有"自己开始干活"的副作用——提现尤其如此，亚马逊按滑动 24 小时
从上一次请求起算，临时补跑会把整个窗口拖走。

但崩溃后重启、或操作员自己重启，仍然要补跑：那时错过一次是意外，不是安排。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ziniao_automation.config import UPGRADE_MARKER_NAME, Settings, consume_upgrade_marker


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    made = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'x.db').as_posix()}",
        testing=True,
    )
    made.ensure_directories()
    return made


def write_marker(settings: Settings) -> Path:
    marker = settings.data_dir / UPGRADE_MARKER_NAME
    marker.write_text("installer stopped the service", encoding="utf-8")
    return marker


def test_no_marker_means_an_ordinary_restart(settings: Settings) -> None:
    """崩溃或手动重启没有标记——补跑照旧。"""

    assert consume_upgrade_marker(settings) is False


def test_a_marker_reports_an_upgrade(settings: Settings) -> None:
    write_marker(settings)

    assert consume_upgrade_marker(settings) is True


def test_the_marker_is_consumed_so_it_can_only_skip_once(settings: Settings) -> None:
    """一个残留的标记最多只影响一次启动。

    安装程序装完但用户一直没打开应用时，标记会留在盘上。读完即删，所以之后
    真正的崩溃重启仍然能正常补跑。
    """

    marker = write_marker(settings)

    assert consume_upgrade_marker(settings) is True
    assert not marker.exists()
    assert consume_upgrade_marker(settings) is False


class _Scheduler:
    def __init__(self) -> None:
        self.started = False

    def start(self, paused: bool = False) -> None:
        self.started = True

    def resume(self) -> None:
        pass

    def shutdown(self, wait: bool = False) -> None:
        self.started = False

    def add_job(self, *args, **kwargs):
        raise AssertionError("no schedules in this test")

    def get_jobs(self):
        return ()


@pytest.mark.asyncio
async def test_start_can_skip_recovery() -> None:
    """ScheduleManager.start(recover_missed=False) 不碰补跑那条路。"""

    from ziniao_automation.scheduler import ScheduleManager

    calls: list[str] = []

    class Manager(ScheduleManager):
        async def recover_latest_missed(self, *, now=None):
            calls.append("recovered")
            return ()

        async def refresh(self):
            calls.append("refreshed")
            return None

    def factory():
        return _Scheduler()

    manager = Manager(object(), object(), scheduler_factory=factory)
    await manager.start(recover_missed=False)

    assert calls == ["refreshed"], "升级后只投影，不补跑"


@pytest.mark.asyncio
async def test_start_recovers_by_default() -> None:
    """默认仍然补跑——崩溃后重启不能因为这次改动而漏掉一次。"""

    from ziniao_automation.scheduler import ScheduleManager

    calls: list[str] = []

    class Manager(ScheduleManager):
        async def recover_latest_missed(self, *, now=None):
            calls.append("recovered")
            return ()

        async def refresh(self):
            calls.append("refreshed")
            return None

    manager = Manager(object(), object(), scheduler_factory=lambda: _Scheduler())
    await manager.start()

    assert calls == ["recovered", "refreshed"]


def test_the_installer_writes_the_marker_only_when_it_stopped_a_service() -> None:
    """标记必须写在"确实停掉了一个正在跑的服务"那个分支里。

    写在外面的话，首次安装也会留下标记，于是第一次正常启动被白白跳过一次补跑。
    """

    iss = (
        Path(__file__).resolve().parents[2]
        / "installer"
        / "ziniao-automation.iss"
    ).read_text(encoding="utf-8-sig")

    guard = iss.index("if FileExists(StopScript) then")
    marker = iss.index(UPGRADE_MARKER_NAME)
    end_of_block = iss.index("function InitializeUninstall", guard)

    assert guard < marker < end_of_block, "标记必须在 Stop.bat 存在的那个分支内"
    # 服务端读的名字和安装端写的名字必须是同一个。
    assert f"data\\{UPGRADE_MARKER_NAME}" in iss


@pytest.mark.asyncio
async def test_the_runtime_skips_recovery_when_the_marker_is_present(
    settings: Settings,
) -> None:
    """整条接线：安装程序留下标记 -> 启动时不补跑，而且标记被吃掉。"""

    from ziniao_automation.composition import RuntimeComposition

    seen: list[bool] = []

    class Manager:
        async def start(self, *, recover_missed: bool = True) -> None:
            seen.append(recover_missed)

        async def shutdown(self, wait: bool = False) -> None:
            pass

    class Automation:
        async def recover_startup(self) -> None:
            pass

    def build() -> RuntimeComposition:
        return RuntimeComposition(
            settings=settings,
            session_factory=object(),
            controller=object(),
            workflow_repository=object(),
            workflow_engine=object(),
            run_loader=object(),
            automation_service=Automation(),
            schedule_manager=Manager(),
        )

    marker = write_marker(settings)
    await build().start()
    assert seen == [False], "升级后的第一次启动不补跑"
    assert not marker.exists()

    # 第二次启动（用户后来又开了一次应用）恢复正常。
    await build().start()
    assert seen == [False, True]
