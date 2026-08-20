import pytest
from uuid import uuid4

from ziniao_automation.ziniao.errors import CdpHealthError
from ziniao_automation.ziniao.models import CdpHealth, ProfileSelector, ZiniaoLaunchProof
from ziniao_automation.ziniao.session import CdpSessionManager


class Page:
    url = "about:blank"

    async def title(self):
        return ""


class Context:
    def __init__(self):
        self.startup_page = Page()
        self.pages = [self.startup_page]
        self.dedicated_page = Page()
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        return self.dedicated_page


class Browser:
    def __init__(self):
        self.contexts = [Context()]

    def is_connected(self):
        return True


class Chromium:
    def __init__(self):
        self.browser = Browser()

    async def connect_over_cdp(self, endpoint, timeout):
        return self.browser


class Playwright:
    def __init__(self):
        self.chromium = Chromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


class Manager:
    def __init__(self):
        self.playwright = Playwright()

    async def start(self):
        return self.playwright


@pytest.mark.asyncio
async def test_handle_close_stops_playwright_driver_not_browser() -> None:
    manager = Manager()
    sessions = CdpSessionManager(
        playwright_factory=lambda: manager,
        startup_stability_checks=1,
    )
    selector = ProfileSelector("oauth", "store")
    proof = ZiniaoLaunchProof(selector, "127.0.0.1", 9000, uuid4())
    sessions.register_ziniao_launch(proof)
    handle = await sessions.connect(
        selector=selector,
        host="127.0.0.1",
        port=9000,
        base_health=CdpHealth(reachable=True),
        launch_proof=proof,
    )
    context = manager.playwright.chromium.browser.contexts[0]
    assert context.new_page_calls == 1
    assert handle.page is context.dedicated_page
    assert handle.page is not context.startup_page
    await handle.close()
    assert manager.playwright.stopped is True


@pytest.mark.asyncio
async def test_startup_target_closing_during_stability_gate_is_retry_safe() -> None:
    class ClosingPage(Page):
        def __init__(self):
            self.calls = 0

        def is_closed(self):
            return self.calls >= 2

        async def title(self):
            self.calls += 1
            if self.calls >= 2:
                raise RuntimeError(
                    "Locator.count: Target page, context or browser has been closed"
                )
            return ""

    manager = Manager()
    context = manager.playwright.chromium.browser.contexts[0]
    context.dedicated_page = ClosingPage()
    sessions = CdpSessionManager(
        playwright_factory=lambda: manager,
        startup_stability_checks=2,
        startup_stability_delay_seconds=0,
    )
    selector = ProfileSelector("oauth", "store")
    proof = ZiniaoLaunchProof(selector, "127.0.0.1", 9000, uuid4())
    sessions.register_ziniao_launch(proof)

    with pytest.raises(CdpHealthError) as caught:
        await sessions.connect(
            selector=selector,
            host="127.0.0.1",
            port=9000,
            base_health=CdpHealth(reachable=True),
            launch_proof=proof,
        )

    assert caught.value.details["phase"] == "startup_target"
    assert caught.value.details["retry_safe"] is True
    assert manager.playwright.stopped is True
