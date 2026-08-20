import pytest

from ziniao_automation.ziniao.session import CdpSessionManager


class Chromium:
    def __init__(self) -> None:
        self.endpoints: list[str] = []

    async def connect_over_cdp(self, endpoint: str, *, timeout: int):
        self.endpoints.append(endpoint)
        assert timeout == 15_000
        return object()


@pytest.mark.asyncio
async def test_cdp_websocket_rewrites_authority_but_keeps_browser_id() -> None:
    chromium = Chromium()
    await CdpSessionManager._connect_over_cdp(
        chromium,
        host="127.0.0.1",
        port=19999,
        websocket_url="ws://localhost:1234/devtools/browser/abc-123",
    )
    assert chromium.endpoints == [
        "ws://127.0.0.1:19999/devtools/browser/abc-123"
    ]
