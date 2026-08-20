"""Ziniao process, per-store launch recovery and CDP session orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import inspect
import logging
from pathlib import Path
import subprocess
from typing import Any
from uuid import uuid4

from .client import ZiniaoClient
from .errors import (
    AuthWaitCancelled,
    AuthWaitExpired,
    CdpHealthError,
    ZiniaoConnectionError,
    ZiniaoLaunchError,
)
from .locks import ExecutionLocks
from .models import (
    BrowserProfile,
    ProfileSelector,
    ZiniaoBrowserHandle,
    ZiniaoLaunchProof,
)
from .session import CdpHealthChecker, CdpSessionManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _AuthLease:
    handle: ZiniaoBrowserHandle
    decision: asyncio.Future[bool]


@dataclass(frozen=True, slots=True)
class ZiniaoControllerConfig:
    client_path: Path = Path(r"D:\紫鸟浏览器\ziniao\ziniao.exe")
    max_start_attempts: int = 4
    startup_timeout_seconds: float = 60.0
    startup_poll_seconds: float = 2.0
    # Cancellation is the exceptional hard-stop path used by setup deadlines.
    # Normal session close remains unbounded so its existing semantics do not
    # change; only cancellation cleanup gets these short best-effort bounds.
    cancel_cleanup_step_timeout_seconds: float = 0.75
    clear_stale_singleton_files: bool = True

    def __post_init__(self) -> None:
        if self.max_start_attempts < 1:
            raise ValueError("max_start_attempts must be at least 1")
        if self.startup_timeout_seconds <= 0 or self.startup_poll_seconds <= 0:
            raise ValueError("startup timeouts must be positive")
        if self.cancel_cleanup_step_timeout_seconds <= 0:
            raise ValueError("cancel cleanup timeout must be positive")


class ZiniaoController:
    """Single-account controller with isolated, recoverable store sessions.

    The shared Ziniao client is never restarted to recover one bad store.
    Recovery is strictly ``stopBrowser(store) -> startBrowser(store)`` and is
    bounded by ``max_start_attempts``.
    """

    def __init__(
        self,
        client: ZiniaoClient,
        *,
        config: ZiniaoControllerConfig | None = None,
        locks: ExecutionLocks | None = None,
        health_checker: CdpHealthChecker | None = None,
        sessions: CdpSessionManager | None = None,
        process_running: Callable[[], bool] | None = None,
        process_launcher: Callable[[Path, int], Any] | None = None,
    ) -> None:
        self.client = client
        self.config = config or ZiniaoControllerConfig()
        self.locks = locks or ExecutionLocks()
        self.health_checker = health_checker or CdpHealthChecker()
        self.sessions = sessions or CdpSessionManager()
        self._process_running = process_running or is_ziniao_process_running
        self._process_launcher = process_launcher or launch_ziniao_webdriver
        self._core_ready = False
        self._core_guard = asyncio.Lock()
        self._closed = False
        self._auth_leases: dict[str, _AuthLease] = {}
        self._auth_guard = asyncio.Lock()

    @property
    def funds_lock(self) -> asyncio.Lock:
        return self.locks.funds

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            async with self._auth_guard:
                leases = list(self._auth_leases.values())
                self._auth_leases.clear()
            for lease in leases:
                if not lease.decision.done():
                    lease.decision.set_result(False)
                await lease.handle.expire()
            await self.client.close()

    async def wait_for_auth(
        self,
        handle: ZiniaoBrowserHandle,
        auth_key: str,
        *,
        timeout_seconds: float = 1800.0,
        on_waiting: Callable[[], Any] | None = None,
    ) -> ZiniaoBrowserHandle:
        """Pause in-place while the user completes Passkey/CAPTCHA.

        Call this *inside* ``session``/``financial_session``.  It keeps the
        same Playwright page, per-store lock and (for financial sessions) the
        global funds lock. The web endpoint calls :meth:`continue_auth`.
        """
        key = str(auth_key).strip()
        if not key:
            raise ValueError("auth_key must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        loop = asyncio.get_running_loop()
        lease = _AuthLease(handle=handle, decision=loop.create_future())
        async with self._auth_guard:
            if key in self._auth_leases:
                raise RuntimeError(f"auth lease {key!r} already exists")
            handle.pause()
            self._auth_leases[key] = lease
        try:
            # Publish WAITING_AUTH only after the lease exists. Otherwise a
            # very fast user click can reach continue_auth between the web
            # response and lease registration and incorrectly lose the live
            # Ziniao window.
            if on_waiting is not None:
                callback_result = on_waiting()
                if inspect.isawaitable(callback_result):
                    await callback_result
            try:
                should_continue = await asyncio.wait_for(
                    asyncio.shield(lease.decision),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                await handle.expire()
                raise AuthWaitExpired(f"人工验证等待超过 {timeout_seconds:g} 秒") from exc
            if not should_continue:
                await handle.expire()
                raise AuthWaitCancelled("人工验证已取消")
            handle.resume()
            return handle
        except asyncio.CancelledError:
            await handle.expire()
            raise
        except (AuthWaitExpired, AuthWaitCancelled):
            raise
        except BaseException:
            # A failed state-publication callback must not strand a paused
            # handle and its per-store lock.
            await handle.expire()
            raise
        finally:
            async with self._auth_guard:
                if self._auth_leases.get(key) is lease:
                    self._auth_leases.pop(key, None)

    async def continue_auth(self, auth_key: str) -> bool:
        """Signal a waiting workflow to re-run its identity preflight."""
        async with self._auth_guard:
            lease = self._auth_leases.get(str(auth_key).strip())
            if lease is None or lease.decision.done():
                return False
            lease.decision.set_result(True)
            return True

    async def cancel_auth(self, auth_key: str) -> bool:
        async with self._auth_guard:
            lease = self._auth_leases.get(str(auth_key).strip())
            if lease is None or lease.decision.done():
                return False
            lease.decision.set_result(False)
            return True

    async def auth_snapshot(self) -> dict[str, dict[str, str | int]]:
        async with self._auth_guard:
            return {
                key: {
                    "selector_type": lease.handle.selector_type,
                    "selector_value": lease.handle.selector_value,
                    "debugging_port": lease.handle.debugging_port,
                }
                for key, lease in self._auth_leases.items()
            }

    async def sync_profiles(self) -> list[BrowserProfile]:
        await self.ensure_running()
        return await self.client.get_browser_list()

    async def ensure_running(self) -> None:
        """Ensure the local control API is reachable without global restarts."""
        try:
            response = await self.client.probe()
        except ZiniaoConnectionError:
            pass
        else:
            # Any syntactically valid response proves WebDriver mode is
            # listening. A non-zero status is surfaced by the subsequent
            # business action with Ziniao's precise error message.
            logger.debug("Ziniao API responded with status %s", response.status_code)
            return
        if self._process_running():
            raise ZiniaoLaunchError(
                "紫鸟进程正在运行，但 16851 WebDriver API 未响应；请使用 "
                "Ziniao-WebDriver.bat 手动切换模式，避免关闭其他店铺环境"
            )
        path = self.config.client_path
        if not path.is_file():
            raise ZiniaoLaunchError(f"未找到紫鸟程序：{path}")
        logger.info("Launching Ziniao WebDriver from %s", path)
        self._process_launcher(path, self.client.config.port)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.startup_timeout_seconds
        while loop.time() < deadline:
            await asyncio.sleep(self.config.startup_poll_seconds)
            try:
                await self.client.probe()
                return
            except ZiniaoConnectionError:
                continue
        raise ZiniaoLaunchError(
            f"紫鸟已启动，但端口 {self.client.config.port} 在 "
            f"{self.config.startup_timeout_seconds:g} 秒内没有就绪"
        )

    async def _update_core_once(self) -> None:
        async with self._core_guard:
            if self._core_ready:
                return
            response = await self.client.update_core()
            # -10003 is used by older clients that do not expose updateCore.
            # It must not block startBrowser, whose own response is authoritative.
            if response.status_code in {"0", "-10003"}:
                self._core_ready = True
            else:
                logger.info(
                    "Ziniao updateCore returned %s; continuing with installed core",
                    response.status_code,
                )

    @staticmethod
    def _consume_cleanup_task(task: asyncio.Task[Any]) -> None:
        """Consume a detached best-effort cleanup result without noisy warnings."""

        try:
            task.exception()
        except BaseException:
            pass

    async def _bounded_cancel_cleanup_step(
        self,
        awaitable: Any,
        *,
        operation: str,
    ) -> tuple[bool, Any | None]:
        """Await one cancellation-only cleanup step without blocking locks."""

        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait(
                {task},
                timeout=self.config.cancel_cleanup_step_timeout_seconds,
            )
        except BaseException:
            task.cancel()
            task.add_done_callback(self._consume_cleanup_task)
            raise
        if task not in done:
            cancel_requested = task.cancel()
            if not cancel_requested and task.done():
                # The awaitable completed exactly at the timeout boundary.
                # In particular, a launch-lock acquire may already own the
                # lock and must be reported as acquired so its caller releases
                # it in the surrounding finally block.
                return True, task.result()
            task.add_done_callback(self._consume_cleanup_task)
            logger.warning(
                "Cancelled Ziniao session cleanup timed out: operation=%s",
                operation,
            )
            return False, None
        return True, task.result()

    async def _stop_browser_after_cancel(
        self,
        selector: ProfileSelector,
    ) -> None:
        """Bounded best-effort per-store stop without leaking the launch lock."""

        launch_acquired = False
        acquire_task = asyncio.create_task(self.locks.launch.acquire())
        try:
            try:
                acquired, _ = await self._bounded_cancel_cleanup_step(
                    acquire_task,
                    operation="acquire_launch_lock",
                )
            except BaseException:
                # If cancellation raced the exact successful-acquire boundary,
                # record ownership before propagating so finally cannot leak it.
                if acquire_task.done() and not acquire_task.cancelled():
                    launch_acquired = bool(acquire_task.result())
                raise
            if not acquired:
                return
            launch_acquired = True
            stopped, _ = await self._bounded_cancel_cleanup_step(
                self.client.stop_browser(selector, ignore_errors=True),
                operation="stop_browser",
            )
            if not stopped:
                logger.warning(
                    "Cancelled Ziniao session could not confirm stopBrowser: selector_type=%s",
                    selector.type,
                )
        except asyncio.CancelledError:
            raise
        except BaseException:
            logger.warning(
                "Cancelled Ziniao session best-effort stopBrowser failed: selector_type=%s",
                selector.type,
                exc_info=True,
            )
        finally:
            # asyncio.Lock is not task-owned. This scope is the sole acquirer,
            # so synchronous release is safe even when stopBrowser ignores
            # cancellation after its bounded child task has been detached.
            if launch_acquired and self.locks.launch.locked():
                self.locks.launch.release()

    async def open_store(
        self,
        selector: ProfileSelector,
        store_key: str | None = None,
    ) -> ZiniaoBrowserHandle:
        """Open and lock one store until the returned handle is closed."""
        key = store_key or f"{selector.type}-{selector.value}"
        store_lock = await self.locks.store_lock(key)
        await store_lock.acquire()
        try:
            handle = await self._open_store_locked(selector)
        except BaseException:
            store_lock.release()
            raise
        disconnect = handle._close_callback

        async def _close_and_release(_: ZiniaoBrowserHandle) -> None:
            current_task = asyncio.current_task()
            cancellation_mode = bool(
                current_task is not None and current_task.cancelling()
            )
            disconnect_error: BaseException | None = None
            try:
                if disconnect is not None:
                    if cancellation_mode:
                        await self._bounded_cancel_cleanup_step(
                            disconnect(handle),
                            operation="disconnect_cdp",
                        )
                    else:
                        await disconnect(handle)
            except BaseException as exc:
                disconnect_error = exc
                if isinstance(exc, asyncio.CancelledError):
                    # Covers the race where normal close began just before the
                    # setup deadline cancelled this task inside disconnect.
                    cancellation_mode = True
            finally:
                try:
                    if cancellation_mode:
                        await self._stop_browser_after_cancel(selector)
                    else:
                        async with self.locks.launch:
                            await self.client.stop_browser(
                                selector, ignore_errors=True
                            )
                finally:
                    if store_lock.locked():
                        store_lock.release()
            # On the cancellation path cleanup is best-effort and the original
            # task cancellation remains authoritative. Normal close preserves
            # the previous behavior of surfacing a disconnect failure.
            if isinstance(disconnect_error, asyncio.CancelledError):
                raise disconnect_error
            if disconnect_error is not None and not cancellation_mode:
                raise disconnect_error

        handle._close_callback = _close_and_release
        return handle

    @asynccontextmanager
    async def session(
        self,
        selector: ProfileSelector,
        store_key: str | None = None,
        *,
        financial: bool = False,
    ) -> AsyncIterator[ZiniaoBrowserHandle]:
        """Workflow-facing async session provider.

        ``financial=True`` acquires the global funds lock before the per-store
        lock.  This fixed order prevents deadlocks and serialises V1 payouts.
        """
        if financial:
            async with self.locks.funds:
                handle = await self.open_store(selector, store_key)
                try:
                    yield handle
                finally:
                    await handle.close()
            return
        handle = await self.open_store(selector, store_key)
        try:
            yield handle
        finally:
            await handle.close()

    @asynccontextmanager
    async def financial_session(
        self,
        selector: ProfileSelector,
        store_key: str | None = None,
    ) -> AsyncIterator[ZiniaoBrowserHandle]:
        async with self.session(selector, store_key, financial=True) as handle:
            yield handle

    def oauth_selector(self, browser_oauth: str) -> ProfileSelector:
        """Explicit compatibility helper for stores known to use OAuth."""
        return ProfileSelector("oauth", browser_oauth)

    async def open_oauth_store(
        self, browser_oauth: str, store_key: str | None = None
    ) -> ZiniaoBrowserHandle:
        return await self.open_store(ProfileSelector("oauth", browser_oauth), store_key)

    async def stop_store(self, selector: ProfileSelector, store_key: str | None = None) -> bool:
        """Stop only the requested environment; never the shared client."""
        key = store_key or f"{selector.type}-{selector.value}"
        async with self.locks.store(key):
            async with self.locks.launch:
                return await self.client.stop_browser(selector, ignore_errors=True)

    async def _open_store_locked(self, selector: ProfileSelector) -> ZiniaoBrowserHandle:
        last_issue = "unknown launch failure"
        # A startup target replacement is different from a dead CDP port: it
        # can happen only after the first connect succeeds.  Allow one local,
        # low-frequency recovery for that exact pre-business phase.  Other
        # launch failures keep the normal max-attempt policy, and a workflow
        # exception after this method returns is never replayed here.
        startup_target_recoveries = 0
        launch_attempt = 1
        attempts_used = 0
        while launch_attempt <= self.config.max_start_attempts:
            attempt = launch_attempt
            attempts_used = attempt
            payload: dict[str, Any] | None = None
            start_attempted = False
            try:
                async with self.locks.launch:
                    await self.ensure_running()
                    await self._update_core_once()
                    start_attempted = True
                    payload = await self.client.start_browser(selector)
                port = _debugging_port(payload)
                host = self.client.config.host
                health = await self.health_checker.probe(host, port)
                if not health.reachable:
                    raise CdpHealthError(
                        f"紫鸟返回了调试端口 {port}，但 Chrome CDP 没有响应",
                        details={"health": health},
                    )
                proof = ZiniaoLaunchProof(
                    selector=selector,
                    debugging_host=host,
                    debugging_port=port,
                    nonce=uuid4(),
                )
                register_launch = getattr(self.sessions, "register_ziniao_launch", None)
                if not callable(register_launch):
                    raise CdpHealthError(
                        "浏览器会话管理器不支持紫鸟来源校验，已拒绝连接"
                    )
                register_launch(proof)
                try:
                    handle = await self.sessions.connect(
                        selector=selector,
                        host=host,
                        port=port,
                        base_health=health,
                        launch_proof=proof,
                    )
                finally:
                    # connect consumes the proof. If a custom session manager
                    # failed earlier, also make sure it cannot be replayed.
                    discard_launch = getattr(
                        self.sessions, "discard_ziniao_launch", None
                    )
                    if callable(discard_launch):
                        discard_launch(proof)
                _assert_ziniao_handle_origin(
                    handle,
                    proof=proof,
                    selector=selector,
                    host=host,
                    port=port,
                )
                logger.info(
                    "Ziniao store %s ready on CDP %s:%s (attempt %s/%s)",
                    selector.value,
                    host,
                    port,
                    attempt,
                    self.config.max_start_attempts,
                )
                return handle
            except Exception as exc:
                last_issue = str(exc)
                startup_target_retry = _is_retryable_startup_target_error(exc)
                logger.warning(
                    "Ziniao store %s launch attempt %s/%s failed: %s",
                    selector.value,
                    attempt,
                    self.config.max_start_attempts,
                    exc,
                )
                # Clean a partial environment before retrying. If the local
                # control API itself is unavailable, a blind stop would only
                # add delay and cannot improve the next attempt.
                if start_attempted:
                    async with self.locks.launch:
                        await self.client.stop_browser(selector, ignore_errors=True)
                    if payload and self.config.clear_stale_singleton_files:
                        _clear_singleton_locks(payload.get("userData"))
                if startup_target_retry:
                    if startup_target_recoveries >= 1:
                        logger.warning(
                            "Ziniao store %s startup target closed again; "
                            "bounded recovery exhausted",
                            selector.value,
                        )
                        break
                    startup_target_recoveries += 1
                    # Give Ziniao's own profile shutdown a small quiet window
                    # before requesting a fresh dynamic port.  Do not restart
                    # the shared desktop client.
                    await asyncio.sleep(max(0.5, self.config.startup_poll_seconds))
                if attempt >= self.config.max_start_attempts:
                    break
                launch_attempt += 1
        raise ZiniaoLaunchError(
            f"店铺 {selector.value} 连续 {attempts_used} 次启动未通过健康检查："
            f"{last_issue}。其他店铺不受影响。"
        )


def _debugging_port(payload: dict[str, Any]) -> int:
    value = payload.get("debuggingPort")
    if value is None and isinstance(payload.get("data"), dict):
        value = payload["data"].get("debuggingPort")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ZiniaoLaunchError("startBrowser 响应缺少有效 debuggingPort") from exc
    if not 1 <= port <= 65535:
        raise ZiniaoLaunchError(f"startBrowser 返回了无效 debuggingPort：{port}")
    return port


def _assert_ziniao_handle_origin(
    handle: ZiniaoBrowserHandle,
    *,
    proof: ZiniaoLaunchProof,
    selector: ProfileSelector,
    host: str,
    port: int,
) -> None:
    """Fail closed before any Amazon navigation can use the handle."""
    if (
        getattr(handle, "launch_proof", None) is not proof
        or handle.selector != selector
        or handle.debugging_host != host
        or handle.debugging_port != port
    ):
        raise CdpHealthError(
            "CDP 会话与本次紫鸟 startBrowser 响应不一致，已拒绝访问 Seller Central"
        )


def _clear_singleton_locks(user_data: Any) -> None:
    if not user_data:
        return
    try:
        base = Path(str(user_data)).resolve()
        if not base.is_dir():
            return
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            candidate = base / name
            if candidate.exists() or candidate.is_symlink():
                candidate.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not clear stale Chrome singleton files", exc_info=True)


def _is_retryable_startup_target_error(exc: BaseException) -> bool:
    """Accept only a session-manager proof of a pre-business target loss."""

    return (
        isinstance(exc, CdpHealthError)
        and exc.details.get("phase") == "startup_target"
        and exc.details.get("retry_safe") is True
    )


def is_ziniao_process_running() -> bool:
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ziniao.exe"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return "ziniao.exe" in completed.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def launch_ziniao_webdriver(path: Path, port: int) -> subprocess.Popen[Any]:
    """Start the desktop client in WebDriver mode without a shell."""
    return subprocess.Popen(
        [
            str(path),
            "--run_type=web_driver",
            "--ipc_type=http",
            f"--port={port}",
        ],
        cwd=str(path.parent),
        close_fds=True,
    )
