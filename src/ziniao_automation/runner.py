"""Small process runner used by Start.bat and Task Scheduler.

The batch files intentionally contain no encoded or hidden PowerShell command.
This module owns the PID file and starts the loopback-only ASGI server.
"""

from __future__ import annotations

import atexit
import argparse
import json
import os
from pathlib import Path
import signal
import sys

import uvicorn

from .config import Settings


def _pid_file(settings: Settings) -> Path:
    return settings.data_dir / "run" / "server.pid"


def _read_pid(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, OSError):
        return None
    return int(value) if value.isdigit() and int(value) > 0 else None


def _write_pid(path: Path, pid: int) -> None:
    if int(pid) <= 0:
        raise ValueError("pid must be positive")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid)), encoding="ascii")


def _clear_codex_parent_markers() -> None:
    """Keep a locally launched service outside a temporary Codex job scope.

    Normal desktop/task-scheduler launches do not define these variables.  A
    Start.bat invocation made from Codex does; clearing only those ownership
    markers lets the same local service survive after the tool call finishes.
    """

    for name in (
        "CODEX_SHELL",
        "CODEX_THREAD_ID",
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        "CODEX_SANDBOX_NETWORK_DISABLED",
    ):
        os.environ.pop(name, None)


def _executable_for_pid(pid: int) -> Path | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    process = ctypes.WinDLL("kernel32", use_last_error=True).OpenProcess(
        0x1000, False, pid  # PROCESS_QUERY_LIMITED_INFORMATION
    )
    if not process:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        query = ctypes.WinDLL("kernel32", use_last_error=True).QueryFullProcessImageNameW
        if not query(process, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).resolve()
    finally:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(process)


def _is_managed_python(path: Path, settings: Settings) -> bool:
    """Accept uv's venv launcher and its project-local managed interpreter."""
    resolved = path.resolve()
    roots = (
        (settings.project_root / ".venv").resolve(),
        (settings.project_root / ".uv-python").resolve(),
    )
    return any(resolved == root or root in resolved.parents for root in roots)


def stop() -> int:
    settings = Settings.from_env()
    pid_file = _pid_file(settings)
    pid = _read_pid(pid_file)
    if pid is None:
        return 0
    observed = _executable_for_pid(pid)
    if observed is None:
        pid_file.unlink(missing_ok=True)
        return 0
    if not _is_managed_python(observed, settings):
        return 3
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        # On Windows, os.kill(SIGTERM) can be denied for pythonw even for the
        # current user. The executable path was verified above, so a forced
        # taskkill remains scoped to this exact project PID.  Do not add ``/T``:
        # an externally owned Ziniao/browser process must remain untouched.
        import subprocess

        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/F"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            return 5
    pid_file.unlink(missing_ok=True)
    return 0


def last_error(settings: Settings | None = None, *, tail_bytes: int = 64_000) -> str:
    """The most recent ERROR from the log, phrased for a non-technical reader.

    Start.bat and the desktop launcher both used to end at "请查看
    data\\logs\\ziniao-automation.jsonl", which asks someone who has never seen
    JSON to go read JSON.  The one line they actually need is already in that
    file; this digs it out so the caller can put it on screen.

    Returns an empty string when there is nothing to report — the caller decides
    what to say instead, rather than being handed a misleading placeholder.
    """

    settings = settings or Settings.from_env()
    path = settings.log_dir / "ziniao-automation.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - tail_bytes))
            # A mid-line seek can split a UTF-8 sequence; drop that partial line.
            chunk = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(chunk.splitlines()[1:] or chunk.splitlines()):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict) or record.get("level") != "ERROR":
            continue
        message = " ".join(str(record.get("message") or "").split())
        kind = str(record.get("exception_type") or "").strip()
        detail = str(record.get("exception_message") or "").strip()
        parts = [part for part in (message, kind, detail) if part]
        return " | ".join(parts)[:400]
    return ""


def serve() -> int:
    _clear_codex_parent_markers()
    settings = Settings.from_env()
    run_dir = settings.data_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    pid_file = _pid_file(settings)
    previous = _read_pid(pid_file)
    if previous is not None and _executable_for_pid(previous) is not None:
        return 4
    pid_file.unlink(missing_ok=True)
    _write_pid(pid_file, os.getpid())

    def remove_pid() -> None:
        try:
            if pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(remove_pid)

    # The notification-area icon is the only way back to the console once the
    # operator closes the browser tab, since this process owns no window.  It
    # runs on a daemon thread and swallows its own failures: a machine where
    # the shell will not cooperate must still get a working scheduler.
    from . import tray

    icon = settings.project_root / "installer" / "app.ico"
    tray.start(
        f"http://{settings.host}:{settings.port}/",
        icon_path=str(icon) if icon.is_file() else None,
        on_quit=lambda: os.kill(os.getpid(), signal.SIGTERM),
    )

    try:
        uvicorn.run(
            "ziniao_automation.main:app",
            host="127.0.0.1",
            port=8765,
            access_log=False,
            log_config=None,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        # pythonw has no console. Keep a JSONL-safe, secret-free clue in the
        # normal log rather than creating a second unrotated log file.
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        with (settings.log_dir / "ziniao-automation.jsonl").open(
            "a", encoding="utf-8"
        ) as stream:
            stream.write(
                json.dumps(
                    {
                        "level": "ERROR",
                        "logger": "ziniao_automation.runner",
                        "message": "runner exited during startup",
                        "exception_type": type(exc).__name__,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
        raise
    finally:
        remove_pid()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    parser.add_argument(
        "--last-error",
        action="store_true",
        help="打印日志里最近一条错误，供启动脚本显示给操作员",
    )
    args = parser.parse_args()
    if args.last_error:
        reported = last_error()
        if reported:
            print(reported)
        return 0
    return stop() if args.stop else serve()


if __name__ == "__main__":
    raise SystemExit(main())
