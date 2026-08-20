import asyncio
from collections import Counter

import httpx
import pytest

from ziniao_automation.ziniao.client import ZiniaoClient, ZiniaoClientConfig
from ziniao_automation.ziniao.errors import ZiniaoApiError
from ziniao_automation.ziniao.models import ProfileSelector


@pytest.mark.asyncio
async def test_start_and_stop_use_explicit_oauth_selector() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        bodies.append(body)
        if body["action"] == "startBrowser":
            return httpx.Response(200, json={"statusCode": 0, "debuggingPort": 19999})
        return httpx.Response(200, json={"statusCode": "0"})

    client = ZiniaoClient(
        ZiniaoClientConfig(company="c", username="u", password="p"),
        transport=httpx.MockTransport(handler),
    )
    selector = ProfileSelector("oauth", "12345")
    await client.start_browser(selector)
    await client.stop_browser(selector)
    await client.close()

    assert all(body["browserOauth"] == "12345" for body in bodies)
    assert all("browserId" not in body for body in bodies)
    assert {body["action"] for body in bodies} == {"startBrowser", "stopBrowser"}
    launch = next(body for body in bodies if body["action"] == "startBrowser")
    assert launch["isHeadless"] == 0


@pytest.mark.asyncio
async def test_numeric_id_requires_explicit_id_selector() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"statusCode": 0, "debuggingPort": 19999})

    client = ZiniaoClient(ZiniaoClientConfig(), transport=httpx.MockTransport(handler))
    await client.start_browser(ProfileSelector("id", "12345"))
    await client.close()
    assert seen["browserId"] == "12345"
    assert "browserOauth" not in seen


@pytest.mark.asyncio
async def test_get_browser_list_preserves_selector_source() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "statusCode": 0,
                "data": {
                    "browserList": [
                        {"browserOauth": "oauth-a", "browserId": 7, "browserName": "A"},
                        {"browserId": 8, "browserName": "legacy"},
                    ]
                },
            },
        )

    client = ZiniaoClient(ZiniaoClientConfig(), transport=httpx.MockTransport(handler))
    profiles = await client.get_browser_list()
    await client.close()
    assert [(p.selector_type, p.selector_value) for p in profiles] == [
        ("oauth", "oauth-a"),
        ("id", "8"),
    ]
    assert profiles[0].browser_id == "7"
    assert profiles[1].browser_oauth is None


@pytest.mark.asyncio
async def test_api_error_does_not_include_password() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"statusCode": 42, "err": "password=SUPER_SECRET denied"},
        )

    client = ZiniaoClient(
        ZiniaoClientConfig(password="SUPER_SECRET"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ZiniaoApiError) as caught:
        await client.get_browser_list()
    await client.close()
    assert "SUPER_SECRET" not in str(caught.value)
