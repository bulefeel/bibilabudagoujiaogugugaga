"""Cloud classifier that picks the removal reason from the buyer's wording.

This is the one part of the service that talks to the internet.  It is
deliberately fail-closed: every uncertain, malformed, rate-limited or errored
response yields ``None``, and ``None`` means "do not submit — queue for a
human".  Submitting a request is irreversible and one-shot per feedback, so a
guessed reason costs the seller that feedback's only chance of removal.

The endpoint and key are read from the operator's existing Codex install
(``~/.codex/config.toml`` + ``~/.codex/auth.json``) rather than configured
again here, at their request.  Resolution happens per call, so rotating the key
there takes effect without restarting the service.  The *model* and reasoning
effort are this module's own (see ``CLASSIFIER_MODEL``): changing the model
Codex uses for coding must not silently change what is submitted to Amazon.

Buyer wording is sent to the model and stored in SQLite for the operator, but
must never reach the JSONL logs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import tomllib
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import (
    CATEGORY_LABELS,
    CLASSIFIER_EXCLUDED_CATEGORIES,
    CLASSIFIER_MODEL,
    CLASSIFIER_REASONING_EFFORT,
    REASON_CATALOG,
    is_known_reason,
)

logger = logging.getLogger(__name__)

# Every code inside an excluded category.  Amazon removes FBA delivery
# complaints itself (its note and the strike-through on the page are exactly
# that), and for anything still actionable the page states no fulfilment
# channel — so choosing one would assert a fact nothing supports.
EXCLUDED_REASON_CODES = frozenset(
    code
    for category in CLASSIFIER_EXCLUDED_CATEGORIES
    for code in REASON_CATALOG.get(category, {})
)


@dataclass(frozen=True, slots=True)
class ClassifierEndpoint:
    """Where to ask, resolved from Codex's own configuration."""

    base_url: str
    api_key: str
    # Endpoint and key come from Codex; the model and how hard it thinks are
    # this workflow's own decision, because they change what gets submitted.
    model: str = CLASSIFIER_MODEL
    reasoning_effort: str = CLASSIFIER_REASONING_EFFORT
    provider: str = ""

    @property
    def is_plaintext(self) -> bool:
        return self.base_url.lower().startswith("http://")

    def describe(self) -> dict[str, Any]:
        """Non-secret summary for the diagnostics page."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "provider": self.provider,
            "plaintext": self.is_plaintext,
        }


@dataclass(frozen=True, slots=True)
class ReasonDecision:
    category: str
    reason_code: str
    note: str


def resolve_codex_endpoint(home: Path | None = None) -> ClassifierEndpoint | None:
    """Read the active provider out of the operator's Codex install.

    Returns ``None`` — never a partial endpoint — when anything is missing.
    A half-resolved endpoint would fail on every call anyway, and the caller
    already treats "no endpoint" as "everything waits for a human".
    """

    root = (home or Path.home()) / ".codex"
    try:
        auth = json.loads((root / "auth.json").read_text(encoding="utf-8"))
        config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.info("Codex configuration is unavailable: %s", type(exc).__name__)
        return None

    api_key = str(auth.get("OPENAI_API_KEY") or "").strip()
    provider_key = str(config.get("model_provider") or "").strip()
    provider = (config.get("model_providers") or {}).get(provider_key) or {}
    base_url = str(provider.get("base_url") or "").strip().rstrip("/")

    # Codex's own ``model`` key is deliberately ignored — see CLASSIFIER_MODEL.
    if not (api_key and base_url):
        logger.info("Codex configuration is incomplete; classification stays manual")
        return None
    return ClassifierEndpoint(
        base_url=base_url, api_key=api_key, provider=provider_key
    )


def _catalog_prompt() -> str:
    lines: list[str] = []
    for category, reasons in REASON_CATALOG.items():
        if category in CLASSIFIER_EXCLUDED_CATEGORIES:
            continue
        lines.append(f"- {category} ({CATEGORY_LABELS[category]}):")
        for code, label in reasons.items():
            lines.append(f"    {code} = {label}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""你在帮亚马逊卖家判断一条买家反馈应当用哪个「请求审核」原因提交给亚马逊。

可选的原因只有下面这些。主因用 slug，次因用数字码：

{_catalog_prompt()}

亚马逊**只在以下情况下**会真正受理删除请求：
- 反馈包含淫秽语言
- 反馈包含卖家特定的个人识别信息
- 整条反馈其实是商品评论，而不是对卖家服务的评价
- 该订单由亚马逊配送，且反馈针对配送或客服

⚠️ 「配送相关反馈」这一类**不在你的选项里**，别去猜配送方式：亚马逊对自己配送的订单
会主动把反馈剔除掉，轮不到我们提；而还需要我们提的那些，页面上根本没写配送方式。

判断规则：
1. **每条都要给出一个原因，不要动不动就说判不准。** 卖家要的是把能提的都提掉，
   而不是拿到一堆待办。挑最贴近的那一类，别因为不完美就放弃。
2. 混合内容（既说商品又说卖家没回复）**照样要判**：看哪一方面是主要的就选哪一类。
3. 常见情形照这样归：
   - 收到的东西与描述/图片不符、数量少了、发错款式尺码、质量不行 → 203
   - 未送达、空包、包裹破损、配送延误 → 若买家主要在说收到的东西不对就归 203，
     否则归 not-listed 的 301；**不要**去选配送类
   - 有脏话或人身攻击 → 102；出现电话、邮箱、住址等 → 103
   - 通篇只是情绪、看不出具体问题 → not-listed 的 301
4. 只有在留言完全无法理解（乱码、空白）时才返回 confident=false。
5. 低分本身不是删除理由，但也不要因为分低就更谨慎——按内容判。

只输出一个 JSON 对象，不要有别的文字：
{{"confident": true/false, "category": "<slug 或 null>", "reason_code": "<码 或 null>", "note": "<一句中文理由>"}}"""


class FeedbackReasonClassifier:
    """Ask the operator's configured model for a reason, or return ``None``."""

    def __init__(
        self,
        endpoint_resolver: Callable[[], ClassifierEndpoint | None] = resolve_codex_endpoint,
        *,
        timeout_seconds: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolve = endpoint_resolver
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def endpoint(self) -> ClassifierEndpoint | None:
        try:
            return self._resolve()
        except Exception:
            logger.exception("Classifier endpoint could not be resolved")
            return None

    async def classify(
        self,
        *,
        comment: str,
        rating: int,
        order_date: str | None = None,
    ) -> ReasonDecision | None:
        text = (comment or "").strip()
        if not text:
            # Nothing to reason about; a human can still pick a reason.
            return None

        endpoint = self.endpoint()
        if endpoint is None:
            return None

        user_prompt = (
            f"买家评分：{rating} 星\n"
            f"订单日期：{order_date or '未知'}\n"
            f"买家留言：\n{text}"
        )
        payload = {
            "model": endpoint.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            # The operator's Codex sets disable_response_storage; honour it so
            # buyer wording is not retained by the relay any longer than the
            # request itself.
            "store": False,
            "reasoning_effort": endpoint.reasoning_effort,
        }
        headers = {
            "Authorization": f"Bearer {endpoint.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{endpoint.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except Exception:
            # Timeout, DNS, TLS, connection refused — all mean "ask a human".
            logger.exception("Feedback classifier request failed")
            return None

        if response.status_code != 200:
            # Includes 401 (bad key), 402/429 (billing, rate limit), 5xx.
            logger.warning(
                "Feedback classifier returned HTTP %s", response.status_code
            )
            return None

        try:
            body = response.json()
        except ValueError:
            logger.warning("Feedback classifier returned non-JSON body")
            return None

        return _decision_from_body(body)


def _decision_from_body(body: Any) -> ReasonDecision | None:
    text = _first_message(body)
    if not text:
        return None
    parsed = _parse_json_object(text)
    if parsed is None:
        return None

    if parsed.get("confident") is not True:
        return None
    category = str(parsed.get("category") or "").strip()
    reason_code = str(parsed.get("reason_code") or "").strip()
    # The whitelist is the real gate: a model may name a plausible-sounding
    # reason that Amazon does not offer.
    if not is_known_reason(category, reason_code):
        logger.warning("Feedback classifier proposed an unknown reason")
        return None
    if reason_code in EXCLUDED_REASON_CODES:
        # Not offered in the prompt, so this is the model going off-menu.  These
        # assert "Amazon fulfilled this order", which nothing on the page
        # supports for an entry that is still actionable.
        logger.warning("Feedback classifier chose an excluded reason")
        return None
    note = str(parsed.get("note") or "").strip()[:400]
    return ReasonDecision(category=category, reason_code=reason_code, note=note)


def _first_message(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "")


def _parse_json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        _, _, candidate = candidate.partition("\n")
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        decoded = json.loads(candidate[start : end + 1])
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None
