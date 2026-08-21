"""The notification-area icon is optional decoration and must behave like it."""

from __future__ import annotations

import os
import threading
import time

import pytest

from ziniao_automation import tray


@pytest.mark.skipif(os.name != "nt", reason="tray icon is Windows-only")
def test_the_icon_registers_and_keeps_its_callback_alive() -> None:
    """The window procedure must outlive ``_build``.

    ctypes does not hold a reference to a WINFUNCTYPE callback on Windows'
    behalf.  If Python collects it while the shell still has the pointer, the
    next mouse message over the icon crashes the whole service — taking the
    payout scheduler with it.
    """

    tray.hwnd_keepalive.clear()
    tray.start("http://127.0.0.1:8765/", icon_path=None)
    for _ in range(40):
        if len(tray.hwnd_keepalive) == 3:
            break
        time.sleep(0.1)

    assert len(tray.hwnd_keepalive) == 3, "窗口过程/窗口类/图标数据都必须被持有"
    assert any(
        thread.name == "ziniao-tray" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_a_shell_that_refuses_the_icon_does_not_take_the_service_down(
    monkeypatch,
) -> None:
    """Losing the icon is an inconvenience; losing the scheduler is not.

    Session 0, a locked-down shell or a broken icon file must all degrade to
    "no icon" rather than propagating out of the thread.
    """

    def explode(*args, **kwargs):
        raise OSError(5, "Shell_NotifyIconW failed")

    monkeypatch.setattr(tray, "_build", explode)
    tray._run("http://127.0.0.1:8765/", None, lambda: None)  # must not raise


def test_start_is_a_no_op_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(tray.os if hasattr(tray, "os") else os, "name", "posix")
    started: list[str] = []
    monkeypatch.setattr(
        threading, "Thread", lambda *a, **k: started.append("spawned")
    )
    tray.start("http://127.0.0.1:8765/")
    assert started == [] or os.name == "nt"
