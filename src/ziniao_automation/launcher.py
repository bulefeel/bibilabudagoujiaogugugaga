"""Desktop entry point: make sure the service is up, then open the console.

The shortcut created by the installer runs ``pythonw.exe -m
ziniao_automation.launcher``.  ``pythonw`` is deliberate: a ``.bat`` flashes a
black console that looks like a crash to a non-technical operator, and a
VBScript/PowerShell wrapper is exactly the shape that got this project flagged
by antivirus once already.

Having no console also means a failure here is completely invisible, so every
exit path that is not "the browser opened" ends in a message box.  That uses
``user32.MessageBoxW`` through ``ctypes`` rather than adding a GUI dependency.
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser

from .config import Settings
from .runner import last_error

# The service takes a few seconds to bind: alembic runs ``upgrade head`` and
# APScheduler restores its jobs before uvicorn listens.  Long enough to cover a
# cold start on a slow disk, short enough that a genuinely broken install does
# not leave the operator staring at nothing.
STARTUP_TIMEOUT_SECONDS = 40.0
POLL_INTERVAL_SECONDS = 0.5

MB_OK = 0x0
MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30


def _message_box(title: str, text: str, *, icon: int = MB_ICONERROR) -> None:
    if os.name != "nt":
        print(f"{title}: {text}", file=sys.stderr)
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, title, MB_OK | icon)


def _is_serving(host: str, port: int, *, timeout: float = 0.5) -> bool:
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def _service_command(settings: Settings) -> list[str] | None:
    """The exact interpreter that owns this install, or None if it is missing.

    ``sys.executable`` is already the project's ``pythonw.exe`` when the
    shortcut launches us, so prefer it and only fall back to looking beside the
    project root.  Resolving it explicitly keeps a stray Python on PATH from
    being handed the service.
    """

    candidates = []
    current = Path(sys.executable)
    if current.name.lower() == "pythonw.exe":
        candidates.append(current)
    candidates.append(settings.project_root / ".venv" / "Scripts" / "pythonw.exe")
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate), "-m", "ziniao_automation.runner"]
    return None


def _spawn_detached(command: list[str], settings: Settings) -> None:
    """Start the service so it outlives this launcher process."""

    creation_flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: no console is created and
        # closing the launcher cannot signal the service.
        creation_flags = 0x00000008 | 0x00000200
    subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        command,
        cwd=str(settings.project_root),
        creationflags=creation_flags,
        close_fds=True,
    )


def _wait_until_serving(host: str, port: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _is_serving(host, port):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def main() -> int:
    settings = Settings.from_env()
    host, port = settings.host, settings.port
    url = f"http://{host}:{port}/"

    if _is_serving(host, port):
        webbrowser.open(url)
        return 0

    command = _service_command(settings)
    if command is None:
        _message_box(
            "紫鸟提现自动化",
            "找不到程序运行环境（.venv\\Scripts\\pythonw.exe）。\n\n"
            "多半是安装没有完成。请重新运行安装程序。",
        )
        return 2

    try:
        _spawn_detached(command, settings)
    except OSError as exc:
        _message_box(
            "紫鸟提现自动化",
            f"无法启动后台服务。\n\n{type(exc).__name__}: {exc}\n\n"
            "如果这台电脑装了安全软件，请在它的拦截记录里放行本程序。",
        )
        return 3

    if not _wait_until_serving(host, port, time.monotonic() + STARTUP_TIMEOUT_SECONDS):
        # The service writes its startup exception to the normal log even under
        # pythonw, so the operator gets the actual cause instead of a file path.
        reason = last_error(settings)
        detail = (
            f"最近一条错误：\n{reason}"
            if reason
            else "日志里没有留下错误，多半是启动被安全软件拦下了。"
        )
        _message_box(
            "紫鸟提现自动化",
            f"后台服务在 {int(STARTUP_TIMEOUT_SECONDS)} 秒内没有就绪。\n\n{detail}\n\n"
            f"完整日志：{settings.log_dir / 'ziniao-automation.jsonl'}",
        )
        return 4

    webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
