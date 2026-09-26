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
from collections.abc import Mapping
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


# Variables that hijack an Electron binary before its own main script runs.
# ``ELECTRON_RUN_AS_NODE=1`` makes ziniao.exe start as a bare Node interpreter:
# it rejects its own flags ("bad option: --run_type=web_driver"), exits with
# code 9, and never opens 16851.  VS Code exports it to everything under its
# extension host, so a service started from a VS Code terminal inherits it and
# can never launch Ziniao — with no hint as to why.  ``NODE_OPTIONS`` is the
# same class of hazard: Electron honours it and a stray ``--require`` changes
# what the client is before we get a say.
ELECTRON_HIJACK_VARIABLES = ("ELECTRON_RUN_AS_NODE", "NODE_OPTIONS")


def launch_environment() -> dict[str, str]:
    """Our environment minus anything that would replace Ziniao's entry point."""

    env = dict(os.environ)
    for name in ELECTRON_HIJACK_VARIABLES:
        env.pop(name, None)
    return env


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
    *,
    store_names: Mapping[str, str],
) -> list[str]:
    """Reasons it is not safe to close Ziniao right now, in the operator's words.

    Killing the browser mid-run would abandon a session that may be holding a
    funds lock, and in the worst case would do it between ``arm_operation`` and
    the irreversible click.  An empty list means the switch may proceed.

    ⚠️ 前两条是**本服务进程内的 asyncio 锁**，第三条才是 SQLite 里的记录。
    这个区别必须写在文案里:2026-09-22 运营关掉了全部紫鸟窗口、报错照旧，于是以为
    判据坏了 —— 实际上关浏览器根本不碰这两把锁，只有持锁的协程释放、或者重启服务
    才会没。不说清楚，操作员只会一遍遍去关浏览器。

    ``store_names`` 把锁上的键(店铺 id 的字符串)换成店铺名。之前直接打印键，
    屏幕上是一个孤零零的「36」——现在有三十多家店，那个数字谁也认不出来。
    """

    blockers: list[str] = []
    snapshot = lock_snapshot or {}
    if snapshot.get("funds_locked"):
        blockers.append("有提现任务正在执行（资金锁被占用）；关闭紫鸟窗口不会释放它")
    busy = [
        key
        for key, locked in (snapshot.get("stores") or {}).items()  # type: ignore[union-attr]
        if locked
    ]
    if busy:
        labelled = [
            f"{store_names[key]}（#{key}）" if key in store_names else f"#{key}"
            for key in sorted(busy)
        ]
        blockers.append(
            "以下店铺的浏览器会话仍在进行："
            + "、".join(labelled)
            + "；关闭紫鸟窗口不会释放它"
        )
    if pending_guard_sites:
        blockers.append(
            "存在点击尚未返回的资金记录："
            + "、".join(sorted(pending_guard_sites))
            + "（提现任务可能正在点击，等它结束；若任务已经卡住，重启服务后会自动收口）"
        )
    return blockers


async def start_webdriver_mode(
    executable: Path,
    *,
    host: str,
    port: int,
    blockers: list[str],
    force: bool = False,
    log_blockers: list[str] | tuple[str, ...] = (),
) -> ModeReport:
    """Bring the local WebDriver API up, closing normal-mode windows if needed.

    ``force=True`` 越过 ``blockers``。这是运营明确要的:锁卡住时(比如持锁的协程
    正等一个已经关掉的浏览器)，除了重启服务没有别的出路，而人就坐在机器前、
    能自己确认还有没有残留进程。

    ⚠️ 越过归越过，**不许静默**:被忽略的每一条都写进 WARNING 日志和返回的
    ``details``。真出了「提现提交到一半被杀」的事，得能在日志里查到是谁在什么时候
    越过了什么。

    ``blockers`` 是给界面看的，里面有店铺名（= 卖家名）；卖家名不进日志文件，
    所以日志只写 ``log_blockers`` —— 同样的理由，店铺只用 ``#id`` 标。
    """

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

    if blockers and not force:
        return ModeReport(
            False,
            "busy",
            "现在切换会关掉正在使用的紫鸟窗口，因此已阻止：\n"
            + "\n".join(blockers)
            + "\n\n前两类是本服务进程内的锁，关闭紫鸟窗口不会清除；"
            "确认无人正在提现后，可用「强制切换」越过。",
            details={"blockers": blockers, "forceable": True},
        )
    if blockers:
        logger.warning(
            "WebDriver switch forced past %d blocker(s): %s",
            len(blockers),
            " | ".join(log_blockers),
            extra={"event": "webdriver_switch_forced"},
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
    # Detached, exactly like ``start ""`` in Ziniao-WebDriver.bat — which is the
    # form that has always worked.  Launched as an ordinary child of this
    # service it inherits our console-less pythonw context and process group,
    # and Ziniao exits again within seconds: the log shows three successive
    # "Starting Ziniao WebDriver mode" lines and afterwards no ziniao.exe alive
    # at all, while 16851 never opens.  DETACHED_PROCESS gives it no console to
    # inherit and CREATE_NEW_PROCESS_GROUP keeps our shutdown signals away from
    # a browser the operator is using.
    creation_flags = 0
    if os.name == "nt":
        creation_flags = 0x00000008 | 0x00000200
    try:
        await asyncio.create_subprocess_exec(
            str(executable),
            *LAUNCH_ARGUMENTS,
            f"--port={port}",
            cwd=str(executable.parent),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=creation_flags,
            close_fds=True,
            env=launch_environment(),
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
            message = f"紫鸟 WebDriver 模式已就绪（{host}:{port}）。现在可以同步店铺了。"
            if blockers:
                # 强制切换成功也要把越过了什么摆在操作员眼前，否则这一步在界面上
                # 和一次干净的切换长得一模一样。
                message += "\n⚠️ 本次为强制切换，已越过：" + "；".join(blockers)
            return ModeReport(
                True,
                "ready",
                message,
                closed_processes=closed,
                details={"overridden_blockers": blockers} if blockers else {},
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
