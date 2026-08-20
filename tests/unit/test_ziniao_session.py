import pytest
from uuid import uuid4

from ziniao_automation.ziniao.errors import CdpHealthError
from ziniao_automation.ziniao.models import CdpHealth, ProfileSelector, ZiniaoLaunchProof
from ziniao_automation.ziniao.session import CdpSessionManager


class Page:
    def __init__(self, url: str, *, webdriver=None, error: Exception | None = None):
        self.url = url
        self.webdriver = webdriver
        self.error = error

    async def title(self):
        return "page"

    async def evaluate(self, _):
        if self.error:
            raise self.error
        return self.webdriver


@pytest.mark.asyncio
async def test_real_http_page_fails_closed_when_js_cannot_be_evaluated() -> None:
    manager = CdpSessionManager()
    health = await manager._inspect_page(
        Page("https://sellercentral.amazon.ca", error=RuntimeError("blocked")),
        CdpHealth(reachable=True),
    )
    assert health.instrumentation_live is False
    assert health.healthy is False


@pytest.mark.asyncio
async def test_internal_page_is_unknown_not_false_positive() -> None:
    manager = CdpSessionManager()
    health = await manager._inspect_page(
        Page("chrome-extension://abc/page.html", webdriver=True),
        CdpHealth(reachable=True),
    )
    assert health.instrumentation_live is None
    assert health.healthy is True


@pytest.mark.asyncio
async def test_webdriver_true_is_unhealthy_on_real_page() -> None:
    manager = CdpSessionManager()
    health = await manager._inspect_page(
        Page("https://sellercentral.amazon.co.uk", webdriver=True),
        CdpHealth(reachable=True),
    )
    assert health.instrumentation_live is False
    assert health.issues


def test_manual_or_external_cdp_endpoint_has_no_trusted_launch_proof() -> None:
    manager = CdpSessionManager()
    selector = ProfileSelector("oauth", "store")
    forged = ZiniaoLaunchProof(selector, "127.0.0.1", 9222, uuid4())

    with pytest.raises(CdpHealthError):
        manager._consume_ziniao_launch(
            forged, selector=selector, host="127.0.0.1", port=9222
        )


def test_launch_proof_is_bound_to_dynamic_port_and_single_use() -> None:
    manager = CdpSessionManager()
    selector = ProfileSelector("oauth", "store")
    proof = ZiniaoLaunchProof(selector, "127.0.0.1", 60806, uuid4())
    manager.register_ziniao_launch(proof)

    with pytest.raises(CdpHealthError):
        manager._consume_ziniao_launch(
            proof, selector=selector, host="127.0.0.1", port=9222
        )
    # A mismatch consumes the proof too, so it cannot later be replayed.
    with pytest.raises(CdpHealthError):
        manager._consume_ziniao_launch(
            proof, selector=selector, host="127.0.0.1", port=60806
        )
