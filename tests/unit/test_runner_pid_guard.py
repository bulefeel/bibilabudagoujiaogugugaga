"""服务启动时那道 PID 闸。

测试机上 0.6.1 装完打不开服务,弹窗写「日志里没有留下错误,多半是启动被安全软件
拦下了」—— 那句话是错的。真实原因是 `serve()` 只问「这个 PID 号现在有进程占着
吗」,不问「占着它的是不是我们自己」。Windows 的 PID 回收很快,重启后那个号基本
落到别的程序头上;而 `data/` 目录安装程序**故意从不触碰**(里面是资金守卫记录),
所以陈旧的 `server.pid` 会跨越每一次升级活下来,服务从此再也起不来。

`stop()` 一直做了这个区分,`serve()` 没有 —— 同一个文件里的两处判据不一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ziniao_automation.config import Settings
from ziniao_automation import runner


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
    (made.data_dir / "run").mkdir(parents=True, exist_ok=True)
    return made


def write_pid(settings: Settings, pid: int) -> Path:
    path = settings.data_dir / "run" / "server.pid"
    path.write_text(str(pid), encoding="ascii")
    return path


def log_lines(settings: Settings) -> list[dict]:
    path = settings.log_dir / "ziniao-automation.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def arrange_serve(monkeypatch, settings: Settings, *, owner: Path | None):
    """让 serve() 跑到 uvicorn 之前就停下，并伪造 PID 的归属。"""

    reached = []
    pid_file = settings.data_dir / "run" / "server.pid"

    def fake_run(*args, **kwargs):
        # 正常返回后 finally 会把自己的记录删掉，所以只能在"运行期间"取样。
        reached.append(
            pid_file.read_text(encoding="ascii").strip()
            if pid_file.is_file()
            else "(no pid file)"
        )

    monkeypatch.setattr(runner.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(runner, "_executable_for_pid", lambda pid: owner)
    monkeypatch.setattr(runner, "uvicorn", type("U", (), {"run": staticmethod(fake_run)}))
    import ziniao_automation.tray as tray

    monkeypatch.setattr(tray, "start", lambda *a, **k: None)
    return reached


def test_a_recycled_pid_must_not_block_startup(monkeypatch, settings: Settings) -> None:
    """别的程序恰好占着那个号，不等于我们的服务在跑。

    这就是测试机上的症状：升级后服务永远起不来，日志里一个字都没有。
    """

    pid_file = write_pid(settings, 4321)
    reached = arrange_serve(
        monkeypatch, settings, owner=Path(r"C:\Windows\System32\notepad.exe")
    )

    import os

    assert runner.serve() == 0
    assert len(reached) == 1, "陈旧记录必须被清掉并继续启动"
    # 运行期间写的是本进程的号，而不是那个陌生的号。
    assert reached[0] == str(os.getpid())
    assert reached[0] != "4321"
    # 干净退出后自己的记录也要撤掉，不给下一次留下陈旧值。
    assert not pid_file.exists()


def test_clearing_a_stale_pid_is_written_down(monkeypatch, settings: Settings) -> None:
    """日志配置发生在 uvicorn 之后，所以这之前的每个出口都必须自己留痕。

    「日志里什么都没有」正是把操作员引向「被杀软拦了」这个错误结论的原因。
    """

    write_pid(settings, 4321)
    arrange_serve(monkeypatch, settings, owner=Path(r"C:\Windows\System32\notepad.exe"))

    runner.serve()

    assert any("stale pid" in row.get("message", "") for row in log_lines(settings))


def test_our_own_running_service_still_blocks_a_second_start(
    monkeypatch, settings: Settings
) -> None:
    """真的已经在跑就不能起第二个——这道闸本来的用途要保住。"""

    ours = settings.project_root / ".venv" / "Scripts" / "pythonw.exe"
    ours.parent.mkdir(parents=True, exist_ok=True)
    ours.write_text("", encoding="utf-8")
    write_pid(settings, 4321)
    reached = arrange_serve(monkeypatch, settings, owner=ours)

    assert runner.serve() == 4
    assert reached == [], "已经在跑时不该再启动一个"
    assert any("already running" in row.get("message", "") for row in log_lines(settings))


def test_stop_clears_a_recycled_pid_instead_of_leaving_it(
    monkeypatch, settings: Settings
) -> None:
    """否则那条记录会一直卡住之后每一次启动。

    原来这里返回 3 并**保留**文件，于是一旦号被回收就再也自愈不了。
    """

    pid_file = write_pid(settings, 4321)
    monkeypatch.setattr(runner.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(
        runner, "_executable_for_pid", lambda pid: Path(r"C:\Windows\System32\notepad.exe")
    )

    assert runner.stop() == 0
    assert not pid_file.exists()


def test_serve_and_stop_agree_on_who_owns_the_pid() -> None:
    """两处判据必须都过 _is_managed_python；不一致正是这个 bug 的形状。"""

    import inspect

    # serve() 现在只是个记日志的外壳，闸在 _serve_inner 里。
    for function in (runner._serve_inner, runner.stop):
        assert "_is_managed_python" in inspect.getsource(function), (
            f"{function.__name__} 必须核对 PID 的归属"
        )


def test_every_pre_server_failure_reaches_the_log(monkeypatch, settings: Settings) -> None:
    """uvicorn 之前的异常本来会静默杀死 pythonw 进程，日志里一个字都没有。

    那正是把操作员引向「被安全软件拦了」的原因——不管真实原因是什么，下次都要
    在日志里留下异常类型。
    """

    monkeypatch.setattr(runner.Settings, "from_env", classmethod(lambda cls: settings))

    def boom() -> int:
        raise PermissionError("data/run 不可写")

    monkeypatch.setattr(runner, "_serve_inner", boom)

    with pytest.raises(PermissionError):
        runner.serve()

    assert any(
        row.get("exception_type") == "PermissionError"
        and "before the web server started" in row.get("message", "")
        for row in log_lines(settings)
    )
