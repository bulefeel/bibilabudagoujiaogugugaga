"""Switch the Ziniao client into WebDriver mode from the console.

This replaces ``Ziniao-WebDriver.bat`` for everyday use.  The batch file stays
in the repository as a manual fallback for the case where the service itself
will not start.

Switching modes **force-closes the operator's open Ziniao store windows**, so
this is the one place in the codebase that kills a process it does not own.
Two protections make that safe:

* the caller must first prove no run is holding the browser (see
  :func:`describe_blockers`), and
* only processes whose executable path equals the located ``ziniao.exe`` are
  terminated — never a match on image name alone, which would also hit an
  unrelated program that happened to be called ``ziniao.exe``.

``runner.stop()`` sets the precedent for the second rule: it resolves the real
image path with ``QueryFullProcessImageNameW`` before signalling anything.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import socket

logger = logging.getLogger(__name__)

# Ziniao ships two executables; a store window is a child of the second.
PROCESS_NAMES = ("ziniao.exe", "ziniaobrowser.exe")
LAUNCH_ARGUMENTS = ("--run_type=web_driver", "--ipc_type=http")
# Ziniao asks Windows for its own permissions on first launch, so the initial
# switch on a fresh machine is much slower than later ones.
READY_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0
SHUTDOWN_SETTLE_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class ModeReport:
    """Everything the console needs to describe what happened."""

    ok: bool
    status: str
    message: str
    closed_processes: int = 0
    details: dict[str, object] = field(default_factory=dict)


def port_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def _image_path(pid: int) -> Path | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


async def _running_ziniao_pids(executable: Path) -> list[int]:
    """PIDs whose image really is the located Ziniao, not just a matching name."""

    if os.name != "nt":
        return []
    try:
        target_dir = executable.resolve().parent
    except OSError:
        target_dir = executable.parent
    found: list[int] = []
    for name in PROCESS_NAMES:
        process = await asyncio.create_subprocess_exec(
            "tasklist.exe", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            parts = [item.strip('" ') for item in line.split('","')]
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            pid = int(parts[1])
            image = _image_path(pid)
            # Same folder as the located client.  ziniaobrowser.exe lives
            # beside ziniao.exe, so comparing the directory covers both without
            # letting an unrelated same-named binary through.
            if image is not None and image.parent == target_dir:
                found.append(pid)
    return found


async def _terminate(pids: list[int]) -> int:
    closed = 0
    for pid in pids:
        process = await asyncio.create_subprocess_exec(
            "taskkill.exe", "/PID", str(pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await process.wait() == 0:
            closed += 1
    return closed


def describe_blockers(
    lock_snapshot: dict[str, object] | None,
    pending_guard_sites: list[str] | None,
) -> list[str]:
    """Reasons it is not safe to close Ziniao right now, in the operator's words.

    Killing the browser mid-run would abandon a session that may be holding a
    funds lock, and in the worst case would do it between ``arm_operation`` and
    the irreversible click.  An empty list means the switch may proceed.
    """

    blockers: list[str] = []
    snapshot = lock_snapshot or {}
    if snapshot.get("funds_locked"):
        blockers.append("有提现任务正在执行（资金锁被占用）")
    busy = [
        key
        for key, locked in (snapshot.get("stores") or {}).items()  # type: ignore[union-attr]
        if locked
    ]
    if busy:
        blockers.append(f"以下店铺的浏览器会话仍在进行：{'、'.join(sorted(busy))}")
    if pending_guard_sites:
        blockers.append(
            "存在尚未收尾的资金记录："
            + "、".join(sorted(pending_guard_sites))
            + "（先在任务详情页回读或结案）"
        )
    return blockers


async def start_webdriver_mode(
    executable: Path,
    *,
    host: str,
    port: int,
    blockers: list[str],
) -> ModeReport:
    """Bring the local WebDriver API up, closing normal-mode windows if needed."""

    if port_open(host, port):
        return ModeReport(
            True, "already_ready", f"紫鸟 WebDriver 模式已在运行（{host}:{port}）。"
        )

    if not executable.is_file():
        return ModeReport(
            False,
            "not_installed",
            f"找不到紫鸟客户端：{executable}\n"
            "请确认已安装紫鸟浏览器；若装在别处，可在系统诊断页手工填写路径。",
            details={"executable": str(executable)},
        )

    if blockers:
        return ModeReport(
            False,
            "busy",
            "现在切换会关掉正在使用的紫鸟窗口，因此已阻止：\n" + "\n".join(blockers),
            details={"blockers": blockers},
        )

    closed = 0
    running = await _running_ziniao_pids(executable)
    if running:
        logger.info(
            "Closing Ziniao normal-mode processes before switching: count=%s",
            len(running),
            extra={"event": "webdriver_switch"},
        )
        closed = await _terminate(running)
        await asyncio.sleep(SHUTDOWN_SETTLE_SECONDS)

    logger.info(
        "Starting Ziniao WebDriver mode: port=%s", port, extra={"event": "webdriver_switch"}
    )
    try:
        await asyncio.create_subprocess_exec(
            str(executable),
            *LAUNCH_ARGUMENTS,
            f"--port={port}",
            cwd=str(executable.parent),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        return ModeReport(
            False,
            "launch_failed",
            f"无法启动紫鸟客户端：{type(exc).__name__}: {exc}",
            closed_processes=closed,
        )

    deadline = asyncio.get_running_loop().time() + READY_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if port_open(host, port):
            return ModeReport(
                True,
                "ready",
                f"紫鸟 WebDriver 模式已就绪（{host}:{port}）。现在可以同步店铺了。",
                closed_processes=closed,
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    return ModeReport(
        False,
        "timeout",
        f"紫鸟已启动，但 {int(READY_TIMEOUT_SECONDS)} 秒内 {host}:{port} 仍未响应。\n"
        "常见原因：紫鸟弹出了「是否允许 WebDriver」的授权提示，需要在紫鸟窗口里点允许；"
        "或该端口被别的程序占用。",
        closed_processes=closed,
    )
