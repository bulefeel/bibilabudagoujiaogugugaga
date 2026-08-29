"""判定原因的分类器：白名单、fail-closed，以及「模型不知道的事不许它说」。

这个分类器决定一次**不可逆**的提交，所以它的默认答案是「不知道」。任何一条
路径拿不出白名单内的、有把握的结论，都必须返回 None（＝转人工），而不是猜。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ziniao_automation.workflows.amazon_feedback.config import (
    CLASSIFIER_MODEL,
    CLASSIFIER_REASONING_EFFORT,
)
from ziniao_automation.workflows.amazon_feedback.classifier import (
    SYSTEM_PROMPT,
    ClassifierEndpoint,
    FeedbackReasonClassifier,
    resolve_codex_endpoint,
)


ENDPOINT = ClassifierEndpoint(
    base_url="http://127.0.0.1:9/v1", api_key="sk-test", provider="custom"
)


def reply(content: str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )

    return httpx.MockTransport(handler)


def build(content: str, status: int = 200) -> FeedbackReasonClassifier:
    return FeedbackReasonClassifier(
        lambda: ENDPOINT, transport=reply(content, status)
    )


async def classify(classifier: FeedbackReasonClassifier):
    return await classifier.classify(comment="包裹一直没到", rating=1, order_date="2026/08/01")


# --------------------------------------------------------------------------
async def test_a_confident_whitelisted_reason_is_accepted() -> None:
    decision = await classify(
        build(json.dumps({
            "confident": True, "category": "product-feedback",
            "reason_code": "203", "note": "整条都是商品评论",
        }))
    )
    assert decision is not None
    assert (decision.category, decision.reason_code) == ("product-feedback", "203")


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"confident": False, "category": None, "reason_code": None}),
        json.dumps({"confident": True, "category": "product-feedback", "reason_code": "201"}),
        json.dumps({"confident": True, "category": "made-up", "reason_code": "203"}),
        "这不是 JSON",
        "",
    ],
    ids=["not-confident", "code-amazon-lacks", "unknown-category", "not-json", "empty"],
)
async def test_anything_less_than_a_whitelisted_answer_goes_to_a_human(content) -> None:
    assert await classify(build(content)) is None


@pytest.mark.parametrize("status", [401, 402, 429, 500])
async def test_an_http_error_never_becomes_a_guess(status: int) -> None:
    assert await classify(build("", status)) is None


async def test_a_transport_failure_never_becomes_a_guess() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    classifier = FeedbackReasonClassifier(
        lambda: ENDPOINT, transport=httpx.MockTransport(boom)
    )
    assert await classify(classifier) is None


async def test_no_endpoint_means_manual_not_failure() -> None:
    assert await classify(FeedbackReasonClassifier(lambda: None)) is None


# --------------------------------------------------------------------------
# 配送类：亚马逊自己会处理，我们不提
# --------------------------------------------------------------------------
@pytest.mark.parametrize("code", ["401", "402"])
async def test_a_delivery_reason_is_always_refused(code: str) -> None:
    """配送类不在选项里，模型给了也不算。

    亚马逊对自己配送的订单会主动剔除反馈（页面上划掉并附说明），所以那一类根本
    轮不到我们提；而还需要我们提的那些，页面上没有任何配送方式可读，选它就是
    在向亚马逊断言一件没有依据的事。
    """

    decision = await classify(build(json.dumps({
        "confident": True, "category": "delivery-related-feedback",
        "reason_code": code, "note": "该订单由亚马逊配送",
    })))
    assert decision is None


def test_the_prompt_does_not_offer_the_delivery_category() -> None:
    assert "delivery-related-feedback" not in SYSTEM_PROMPT
    assert "401" not in SYSTEM_PROMPT and "402" not in SYSTEM_PROMPT
    assert "不在你的选项里" in SYSTEM_PROMPT


def test_the_prompt_asks_for_a_decision_rather_than_a_referral() -> None:
    """操作员明确要求「不用保守不用转人工」：每条都要判，别堆待办。

    代码层的两道闸不受这条影响——白名单和「亚马逊配送」事实照旧强制。
    """

    assert "每条都要给出一个原因" in SYSTEM_PROMPT
    assert "宁可交给人工" not in SYSTEM_PROMPT
    # 亚马逊真实提供的 11 个叶子必须都在提示词里，否则模型会去猜编号。
    for code in ("101", "102", "103", "104", "105", "202", "203", "204", "301"):
        assert code in SYSTEM_PROMPT


# --------------------------------------------------------------------------
# 从 Codex 配置解析端点
# --------------------------------------------------------------------------
def write_codex(root: Path, *, config: str, auth: str | None = '{"OPENAI_API_KEY": "sk-x"}') -> Path:
    home = root / "home"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(config, encoding="utf-8")
    if auth is not None:
        (home / ".codex" / "auth.json").write_text(auth, encoding="utf-8")
    return home


COMPLETE = """
model = "gpt-5.6-sol"
model_provider = "custom"
[model_providers.custom]
base_url = "http://198.51.100.7:8979/v1"
wire_api = "responses"
"""


def test_the_active_provider_is_resolved(tmp_path: Path) -> None:
    endpoint = resolve_codex_endpoint(write_codex(tmp_path, config=COMPLETE))

    assert endpoint is not None
    assert endpoint.base_url == "http://198.51.100.7:8979/v1"
    # Codex says gpt-5.6-sol; judgement deliberately uses our own choice.
    assert endpoint.model == CLASSIFIER_MODEL != "gpt-5.6-sol"
    assert endpoint.is_plaintext is True
    # The describe() payload feeds the diagnostics page and must not leak it.
    assert "sk-x" not in json.dumps(endpoint.describe())


@pytest.mark.parametrize(
    "config, auth",
    [
        (COMPLETE, None),                                     # no auth.json
        (COMPLETE, "{}"),                                     # no key in it
        ('model_provider = "custom"\n[model_providers.custom]\nbase_url = "http://x/v1"', None),
        ('model = "m"\nmodel_provider = "missing"', '{"OPENAI_API_KEY": "sk-x"}'),
        ("this is not toml {{{", '{"OPENAI_API_KEY": "sk-x"}'),
    ],
    ids=["no-auth", "no-key", "no-model", "provider-absent", "broken-toml"],
)
def test_an_incomplete_config_resolves_to_nothing_not_half_an_endpoint(
    tmp_path: Path, config: str, auth: str | None
) -> None:
    """半个端点每次调用都会失败；调用方已经把「没有端点」当成转人工。"""

    assert resolve_codex_endpoint(write_codex(tmp_path, config=config, auth=auth)) is None


def test_a_missing_codex_install_is_not_an_error(tmp_path: Path) -> None:
    assert resolve_codex_endpoint(tmp_path / "nowhere") is None


def test_the_request_carries_the_configured_model_and_effort() -> None:
    """模型和推理强度是本流程自己定的，不跟着 Codex 写代码用的模型走。"""

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    import asyncio

    classifier = FeedbackReasonClassifier(
        lambda: ENDPOINT, transport=httpx.MockTransport(handler)
    )
    asyncio.run(classifier.classify(comment="x", rating=1))

    assert seen["model"] == CLASSIFIER_MODEL == "gpt-5.6-luna"
    assert seen["reasoning_effort"] == CLASSIFIER_REASONING_EFFORT == "high"
    # disable_response_storage 在 Codex 里是开的，请求要尊重它。
    assert seen["store"] is False
