"""Raw CDP health probes and Playwright connection lifecycle."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from .errors import CdpHealthError
from .models import (
    CdpHealth,
    ProfileSelector,
    ZiniaoBrowserHandle,
    ZiniaoLaunchProof,
)

logger = logging.getLogger(__name__)


class CdpHealthChecker:
    def __init__(
        self,
        *,
        attempts: int = 4,
        delay_seconds: float = 2.0,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.attempts = attempts
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    async def probe(self, host: str, port: int) -> CdpHealth:
        endpoint = f"http://{host}:{port}/json/version"
        issues: list[str] = []
        payload: dict[str, Any] | None = None
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
            transport=self._transport,
        ) as http:
            for attempt in range(self.attempts):
                try:
                    response = await http.get(endpoint)
                    response.raise_for_status()
                    candidate = response.json()
                    if isinstance(candidate, dict) and candidate.get("webSocketDebuggerUrl"):
                        payload = candidate
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                if attempt < self.attempts - 1:
                    await asyncio.sleep(self.delay_seconds)
        if payload is None:
            return CdpHealth(reachable=False, issues=("CDP 端口不可达",))
        return CdpHealth(
            reachable=True,
            browser=_optional_string(payload.get("Browser")),
            websocket_url=_optional_string(payload.get("webSocketDebuggerUrl")),
            issues=tuple(issues),
        )


class CdpSessionManager:
    """Connects Playwright to an existing Ziniao Chromium over CDP.

    No browser executable is downloaded or launched.  Disconnecting Playwright
    does not close the persistent Ziniao window; ``stopBrowser`` remains the
    controller's responsibility.
    """

    def __init__(
        self,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        startup_stability_checks: int = 4,
        startup_stability_delay_seconds: float = 0.5,
    ) -> None:
        if startup_stability_checks < 1:
            raise ValueError("startup_stability_checks must be at least 1")
        if startup_stability_delay_seconds < 0:
            raise ValueError("startup_stability_delay_seconds must not be negative")
        self._playwright_factory = playwright_factory
        self._startup_stability_checks = int(startup_stability_checks)
        self._startup_stability_delay_seconds = float(
            startup_stability_delay_seconds
        )
        # Proofs are registered in memory by ZiniaoController immediately
        # after startBrowser. There is deliberately no public/manual CDP
        # registration path.
        self._launch_proofs: dict[object, ZiniaoLaunchProof] = {}

    def register_ziniao_launch(self, proof: ZiniaoLaunchProof) -> None:
        """Trust exactly one controller-issued Ziniao launch response."""
        self._launch_proofs[proof.nonce] = proof

    def discard_ziniao_launch(self, proof: ZiniaoLaunchProof) -> None:
        current = self._launch_proofs.get(proof.nonce)
        if current is proof:
            self._launch_proofs.pop(proof.nonce, None)

    def _consume_ziniao_launch(
        self,
        proof: ZiniaoLaunchProof,
        *,
        selector: ProfileSelector,
        host: str,
        port: int,
    ) -> None:
        registered = self._launch_proofs.pop(proof.nonce, None)
        matches = (
            registered is proof
            and proof.selector == selector
            and proof.debugging_host == host
            and proof.debugging_port == port
        )
        if not matches:
            raise CdpHealthError(
                "已阻止非紫鸟来源的 CDP 连接；必须使用本次 startBrowser 返回的动态端口"
            )

    async def connect(
        self,
        *,
        selector: ProfileSelector,
        host: str,
        port: int,
        base_health: CdpHealth,
        launch_proof: ZiniaoLaunchProof,
    ) -> ZiniaoBrowserHandle:
        # Fail before Playwright starts: an endpoint alone is never enough.
        # A proof is single-use, preventing stale ports from being replayed.
        self._consume_ziniao_launch(
            launch_proof,
            selector=selector,
            host=host,
            port=port,
        )
        factory = self._playwright_factory
        if factory is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise CdpHealthError(
                    "Playwright 未安装；请先安装项目依赖（无需执行 playwright install）"
                ) from exc
            factory = async_playwright
        manager = factory()
        playwright = await manager.start()
        browser: Any = None
        try:
            browser = await self._connect_over_cdp(
                playwright.chromium,
                host=host,
                port=port,
                websocket_url=base_health.websocket_url,
            )
            contexts = list(browser.contexts)
            if not contexts:
                raise CdpHealthError("CDP 已连接，但紫鸟环境没有浏览器上下文")
            context = contexts[0]
            # A Ziniao startup tab can close or be replaced asynchronously
            # after startBrowser.  Never hold that transient Page reference;
            # create a dedicated automation tab inside the *same* persistent
            # profile context, preserving its proxy/fingerprint/session.
            page = await context.new_page()
            health = await self._inspect_page(page, base_health)
            if not health.healthy:
                raise CdpHealthError(
                    "紫鸟环境健康检查未通过",
                    details={"issues": list(health.issues)},
                )
            # Ziniao can finish replacing its startup target *after* CDP has
            # accepted a connection and ``new_page`` has returned.  Without a
            # short stability gate the controller hands a doomed Page to the
            # workflow, whose very first ``Locator.count`` then reports
            # "Target page, context or browser has been closed".  Keep this
            # gate before any Seller Central navigation or business click so
            # the controller may safely retry this store launch only.
            await self._wait_for_startup_target_stability(
                browser=browser,
                context=context,
                page=page,
            )

            async def _close(_: ZiniaoBrowserHandle) -> None:
                # Playwright's Browser.close() sends Browser.close over CDP,
                # which would terminate the persistent Ziniao environment
                # before controller.stopBrowser owns the lifecycle. Stopping
                # the Playwright driver only detaches this CDP client.
                await playwright.stop()

            return ZiniaoBrowserHandle(
                selector=selector,
                debugging_host=host,
                debugging_port=port,
                browser=browser,
                context=context,
                page=page,
                health=health,
                launch_proof=launch_proof,
                _close_callback=_close,
            )
        except BaseException:
            # Cancellation during a launch attempt must also detach the CDP
            # driver; otherwise a task cancellation leaks its transport.
            await asyncio.shield(playwright.stop())
            raise

    async def _wait_for_startup_target_stability(
        self,
        *,
        browser: Any,
        context: Any,
        page: Any,
    ) -> None:
        for check in range(self._startup_stability_checks):
            self._assert_startup_target_open(
                browser=browser,
                context=context,
                page=page,
            )
            try:
                # A harmless round-trip proves that the Page target still
                # accepts CDP commands.  It neither navigates nor clicks.
                await page.title()
            except Exception as exc:
                if _is_target_closed_error(exc):
                    raise CdpHealthError(
                        "紫鸟启动期页面目标被关闭，尚未进入业务流程",
                        details={
                            "phase": "startup_target",
                            "retry_safe": True,
                            "cause": type(exc).__name__,
                        },
                    ) from exc
                raise
            if check < self._startup_stability_checks - 1:
                await asyncio.sleep(self._startup_stability_delay_seconds)

    @staticmethod
    def _assert_startup_target_open(
        *,
        browser: Any,
        context: Any,
        page: Any,
    ) -> None:
        states = (
            (browser, "is_connected", True),
            (context, "is_closed", False),
            (page, "is_closed", False),
        )
        for target, attribute, expected in states:
            value = getattr(target, attribute, expected)
            try:
                observed = value() if callable(value) else value
            except Exception as exc:
                if _is_target_closed_error(exc):
                    observed = not expected
                else:
                    raise
            if bool(observed) is not expected:
                raise CdpHealthError(
                    "紫鸟启动期页面目标已关闭，尚未进入业务流程",
                    details={"phase": "startup_target", "retry_safe": True},
                )

    @staticmethod
    async def _connect_over_cdp(
        chromium: Any,
        *,
        host: str,
        port: int,
        websocket_url: str | None,
    ) -> Any:
        """Prefer the browser WebSocket, preserving Ziniao's endpoint path.

        Chromium's `/json/version` may advertise `127.0.0.1` even when the
        caller reached it through another host. Rewrite only the authority;
        the browser id/path stays untouched.
        """
        endpoint = f"http://{host}:{port}"
        if websocket_url:
            from urllib.parse import urlsplit, urlunsplit

            parsed = urlsplit(websocket_url)
            if parsed.scheme in {"ws", "wss"} and parsed.path:
                endpoint = urlunsplit(
                    (parsed.scheme, f"{host}:{port}", parsed.path, parsed.query, "")
                )
        try:
            return await chromium.connect_over_cdp(endpoint, timeout=15_000)
        except Exception:
            if endpoint.startswith(("ws://", "wss://")):
                logger.info("CDP WebSocket connection failed; trying HTTP discovery")
                return await chromium.connect_over_cdp(
                    f"http://{host}:{port}", timeout=15_000
                )
            raise

    async def _inspect_page(self, page: Any, base_health: CdpHealth) -> CdpHealth:
        issues = list(base_health.issues)
        observed_url = str(getattr(page, "url", "") or "")
        try:
            observed_title = await page.title()
        except Exception:
            observed_title = None
        instrumentation_live: bool | None
        is_http_page = observed_url.lower().startswith(("http://", "https://"))
        if not is_http_page:
            # chrome://, about:blank and extension pages cannot prove whether
            # the page-world injection works. Workflows must not perform a
            # financial action before navigating to and validating HTTP(S).
            instrumentation_live = None
        else:
            try:
                webdriver = await page.evaluate("() => navigator.webdriver")
                instrumentation_live = webdriver is not True
                if webdriver is True:
                    issues.append("紫鸟防自动化注入未生效（navigator.webdriver=true）")
            except Exception:
                # A real web page must be fail-closed: JS evaluation failure
                # means the injection could not be verified.
                instrumentation_live = False
                issues.append("真实网页上未能验证紫鸟注入状态")
        return CdpHealth(
            reachable=base_health.reachable,
            browser=base_health.browser,
            websocket_url=base_health.websocket_url,
            instrumentation_live=instrumentation_live,
            observed_url=observed_url,
            observed_title=observed_title,
            issues=tuple(issues),
        )


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _is_target_closed_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "context has been closed",
            "page has been closed",
            "connection closed while reading from the driver",
        )
    )
