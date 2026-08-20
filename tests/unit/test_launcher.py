"""The desktop shortcut's behaviour, which has no console to report failures."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import ziniao_automation.launcher as launcher


@pytest.fixture()
def settings(tmp_path: Path) -> SimpleNamespace:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_bytes(b"")
    return SimpleNamespace(
        host="127.0.0.1", port=8765, project_root=tmp_path, log_dir=log_dir
    )


def _install(monkeypatch, settings, *, serving, spawned, boxes, opened):
    monkeypatch.setattr(launcher.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(launcher, "_is_serving", lambda *a, **k: serving())
    monkeypatch.setattr(
        launcher, "_spawn_detached", lambda command, _s: spawned.append(command)
    )
    monkeypatch.setattr(
        launcher, "_message_box", lambda title, text, **k: boxes.append(text)
    )
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(launcher.time, "sleep", lambda _s: None)


def test_an_already_running_service_is_not_started_a_second_time(
    monkeypatch, settings
) -> None:
    """Clicking the icon twice must not race two servers onto one port.

    ``runner.serve()`` would refuse the duplicate via its PID file, but the
    operator would still see a second window's worth of delay and a stray
    process.  Probing the port first is both cheaper and the same answer.
    """

    spawned: list[list[str]] = []
    boxes: list[str] = []
    opened: list[str] = []
    _install(monkeypatch, settings, serving=lambda: True, spawned=spawned, boxes=boxes, opened=opened)

    assert launcher.main() == 0
    assert spawned == [], "服务已在运行时不得重复拉起"
    assert opened == ["http://127.0.0.1:8765/"]
    assert boxes == []


def test_a_stopped_service_is_started_then_the_browser_opens(
    monkeypatch, settings
) -> None:
    """The whole point of the shortcut: one click, service up, page open."""

    states = iter([False, False, True])
    spawned: list[list[str]] = []
    boxes: list[str] = []
    opened: list[str] = []
    _install(
        monkeypatch,
        settings,
        serving=lambda: next(states, True),
        spawned=spawned,
        boxes=boxes,
        opened=opened,
    )

    assert launcher.main() == 0
    assert len(spawned) == 1
    assert spawned[0][1:] == ["-m", "ziniao_automation.runner"]
    assert spawned[0][0].lower().endswith("pythonw.exe")
    assert opened == ["http://127.0.0.1:8765/"]
    assert boxes == [], "成功路径不该弹窗打扰"


def test_a_service_that_never_binds_shows_the_real_reason_not_a_file_path(
    monkeypatch, settings
) -> None:
    """pythonw has no console: without a message box the click does nothing.

    And the box has to carry the cause.  Handing a non-technical operator the
    path to a .jsonl is the failure mode this whole helper exists to remove.
    """

    (settings.log_dir / "ziniao-automation.jsonl").write_text(
        '{"level":"ERROR","message":"runner exited during startup",'
        '"exception_type":"OSError","exception_message":"端口被占用"}\n',
        encoding="utf-8",
    )
    spawned: list[list[str]] = []
    boxes: list[str] = []
    opened: list[str] = []
    _install(
        monkeypatch, settings, serving=lambda: False, spawned=spawned, boxes=boxes, opened=opened
    )
    monkeypatch.setattr(launcher, "STARTUP_TIMEOUT_SECONDS", 0.01)

    assert launcher.main() == 4
    assert opened == [], "没起来就不该打开一个必然报错的页面"
    assert len(boxes) == 1
    assert "端口被占用" in boxes[0]
    assert "OSError" in boxes[0]


def test_a_silent_failure_is_named_as_a_likely_antivirus_block(
    monkeypatch, settings
) -> None:
    """No log line at all is itself a diagnosis, and the common cause is 安全软件.

    Saying "日志里没有错误" and stopping would leave the operator with nothing
    to try.
    """

    (settings.log_dir / "ziniao-automation.jsonl").write_text(
        '{"level":"INFO","message":"一切正常"}\n', encoding="utf-8"
    )
    boxes: list[str] = []
    _install(
        monkeypatch, settings, serving=lambda: False, spawned=[], boxes=boxes, opened=[]
    )
    monkeypatch.setattr(launcher, "STARTUP_TIMEOUT_SECONDS", 0.01)

    assert launcher.main() == 4
    assert "安全软件" in boxes[0]


def test_a_half_finished_install_is_reported_instead_of_crashing(
    monkeypatch, settings, tmp_path
) -> None:
    """A missing interpreter must not become an unhandled traceback into nowhere."""

    (tmp_path / ".venv" / "Scripts" / "pythonw.exe").unlink()
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "python.exe"))
    boxes: list[str] = []
    spawned: list[list[str]] = []
    _install(
        monkeypatch, settings, serving=lambda: False, spawned=spawned, boxes=boxes, opened=[]
    )

    assert launcher.main() == 2
    assert spawned == []
    assert "安装没有完成" in boxes[0]
