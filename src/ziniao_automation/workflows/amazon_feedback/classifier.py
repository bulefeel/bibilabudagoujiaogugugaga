"""Cloud classifier that picks the removal reason from the buyer's wording.

This is the one part of the service that talks to the internet.  It is
deliberately fail-closed: every uncertain, malformed, rate-limited or errored
response yields ``None``, and ``None`` means "do not submit — queue for a
human".  Submitting a request is irreversible and one-shot per feedback, so a
guessed reason costs the seller that feedback's only chance of removal.

Buyer wording is sent to the model and stored in SQLite for the operator, but
must never reach the JSONL logs — see :mod:`ziniao_automation.logging_setup`.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any, Callable, Mapping

import httpx

from .config import CATEGORY_LABELS, REASON_CATALOG, is_known_reason

logger = logging.getLogger(__name__)

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"


@dataclass(frozen=True, slots=True)
class ReasonDecision:
    category: str
    reason_code: str
    note: str


def _catalog_prompt() -> str:
    lines: list[str] = []
    for category, reasons in REASON_CATALOG.items():
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

判断规则：
1. 只有当这条反馈**明确落入**上述某一类时才给出原因。
2. 拿不准、信息不足、或只是买家单纯不满意，一律返回 confident=false。
   宁可交给人工，也不要猜——提交机会只有一次，用错就没了。
3. 不要因为评分低就认为可以删除。低分本身不是删除理由。

只输出一个 JSON 对象，不要有别的文字：
{{"confident": true/false, "category": "<slug 或 null>", "reason_code": "<码 或 null>", "note": "<一句中文理由>"}}"""


class FeedbackReasonClassifier:
    """Ask a cloud model for a reason, or return ``None``.

    ``credential_resolver`` is called per request rather than snapshotted at
    construction, so re-saving the key in the console takes effect without a
    service restart.
    """

    def __init__(
        self,
        credential_resolver: Callable[[], Mapping[str, str]],
        *,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
        endpoint: str = ANTHROPIC_MESSAGES_URL,
    ) -> None:
        self._resolve = credential_resolver
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._endpoint = endpoint

    async def classify(
        self, *, comment: str, rating: int, order_date: str | None = None
    ) -> ReasonDecision | None:
        text = (comment or "").strip()
        if not text:
            # Nothing to reason about; a human can still pick a reason.
            return None

        try:
            api_key = str(self._resolve().get("api_key") or "").strip()
        except Exception:
            logger.exception("Feedback classifier credential is unreadable")
            return None
        if not api_key:
            logger.warning("Feedback classifier has no API key configured")
            return None

        user_prompt = (
            f"买家评分：{rating} 星\n"
            f"订单日期：{order_date or '未知'}\n"
            f"买家留言：\n{text}"
        )
        payload = {
            "model": self.model,
            "max_tokens": 512,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(
                    self._endpoint, json=payload, headers=headers
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
    text = _first_text_block(body)
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
    note = str(parsed.get("note") or "").strip()[:400]
    return ReasonDecision(category=category, reason_code=reason_code, note=note)


def _first_text_block(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


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
