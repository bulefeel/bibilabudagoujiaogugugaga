"""Feishu App notifier with a fixed, privacy-preserving card schema."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import re
from time import monotonic
from typing import Any, TYPE_CHECKING

import httpx
from sqlalchemy.orm import Session, sessionmaker

from ..models import SystemSetting
from ..ziniao.credentials import read_generic_credential
from .dto import (
    NotificationKind,
    SafeFeedbackNotice,
    SafeRunNotice,
    SafeSiteNotice,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..workflows.types import WorkflowReport

logger = logging.getLogger(__name__)

FEISHU_API = "https://open.feishu.cn/open-apis"
_MAX_TEXT = 300
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+.!|~-])")
_WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[a-z]:\\|file:///)[^\s,;，；]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(cookie|token|authorization|app[_-]?secret|secret|password|passwd|otp)"
    r"\s*[:=]\s*[^\s,;，；]+"
)
_CHINESE_SECRET_ASSIGNMENT = re.compile(
    r"(验证码|密码|令牌|密钥)\s*[:：=]\s*[^\s,;，；]+"
)
_SELLER_ASSIGNMENT = re.compile(
    r"(?i)\b(seller[_ -]?id|merchant[_ -]?id)\s*[:=]\s*[^\s,;，；]+"
)
_LONG_HEX = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])")
_JWT = re.compile(r"(?i)\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}(?:\.[a-z0-9_-]+)?")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{7,}(?!\d)")


@dataclass(frozen=True, slots=True)
class FeishuCredentials:
    app_id: str
    app_secret: str
    chat_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, str]) -> "FeishuCredentials":
        result = cls(
            app_id=str(value.get("app_id", "")).strip(),
            app_secret=str(value.get("app_secret", "")).strip(),
            chat_id=str(value.get("chat_id", "")).strip(),
        )
        if not all((result.app_id, result.app_secret, result.chat_id)):
            raise ValueError("Feishu App ID, App Secret and Chat ID are all required")
        return result


class DatabaseFeishuCredentialProvider:
    """Resolve the currently enabled Feishu configuration for every send.

    The process-wide notification service is constructed before an operator
    may have completed first-run setup.  Reading both SQLite metadata and the
    opaque Windows credential reference here means saving from the web console
    affects the very next real workflow notification, without rebuilding the
    runtime or retaining an App Secret in SQLite.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        credential_reader: Callable[[str], Mapping[str, str]] = read_generic_credential,
    ) -> None:
        self._session_factory = session_factory
        self._credential_reader = credential_reader

    def __call__(self) -> dict[str, str] | None:
        with self._session_factory() as session:
            row = session.get(SystemSetting, "feishu")
            metadata = dict(row.value) if row is not None and row.value else {}

        if not metadata.get("enabled", False):
            return None
        reference = str(metadata.get("credential_ref", "") or "").strip()
        if not reference:
            return None
        try:
            secret = self._credential_reader(reference)
        except Exception:
            # A delivery failure is persisted by NotificationDeliveryService
            # and can be retried after Credential Manager becomes readable.
            # Keep the exception and reference out of logs because either may
            # contain local identifiers.
            logger.error("Feishu credential reference could not be resolved")
            return None

        return {
            "app_id": str(secret.get("app_id") or metadata.get("app_id") or ""),
            "app_secret": str(secret.get("app_secret") or ""),
            "chat_id": str(secret.get("chat_id") or metadata.get("chat_id") or ""),
        }


class FeishuNotifier:
    """Send a safe workflow card through Feishu OpenAPI.

    ``WorkflowReport`` remains accepted during migration, but it is first
    converted to :class:`SafeRunNotice`.  A successful run without a confirmed
    payment is sent as a neutral completion card, never as a green payment
    confirmation.
    """

    def __init__(
        self,
        credential_provider: Callable[[], Mapping[str, str] | None],
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        api_root: str = FEISHU_API,
    ) -> None:
        self._provider = credential_provider
        self._timeout = timeout_seconds
        self._transport = transport
        self._api_root = api_root.rstrip("/")
        self._token: str | None = None
        self._token_deadline = 0.0
        self._token_credential_key: bytes | None = None
        self._token_lock = asyncio.Lock()

    async def send(self, notice: SafeRunNotice | "WorkflowReport") -> None:
        safe = _as_safe_notice(notice)
        if safe is None:
            logger.info("Feishu notification skipped for a silent workflow result")
            return
        raw = self._provider()
        if not raw:
            # DeliveryService must see a failure so the persistent receipt is
            # marked FAILED and remains retryable after credentials are fixed.
            # Returning normally here would incorrectly record an unsent card
            # as SENT.
            raise RuntimeError("飞书凭据未配置或不可读取")
        credentials = FeishuCredentials.from_mapping(raw)
        token = await self._tenant_token(credentials)
        payload = {
            "receive_id": credentials.chat_id,
            "msg_type": "interactive",
            "content": _card_content(safe),
        }
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self._api_root}/im/v1/messages",
                    params={"receive_id_type": "chat_id"},
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
                response.raise_for_status()
                decoded: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # URLs, credentials and response bodies may contain identifiers.
            raise RuntimeError("飞书通知请求失败") from exc
        if not isinstance(decoded, dict) or int(decoded.get("code", -1)) != 0:
            raise RuntimeError("飞书通知返回失败状态")

    async def _tenant_token(self, credentials: FeishuCredentials) -> str:
        credential_key = _token_credential_key(credentials)
        if (
            self._token
            and self._token_credential_key == credential_key
            and monotonic() < self._token_deadline
        ):
            return self._token
        async with self._token_lock:
            if (
                self._token
                and self._token_credential_key == credential_key
                and monotonic() < self._token_deadline
            ):
                return self._token
            try:
                async with self._client() as client:
                    response = await client.post(
                        f"{self._api_root}/auth/v3/tenant_access_token/internal",
                        json={
                            "app_id": credentials.app_id,
                            "app_secret": credentials.app_secret,
                        },
                    )
                    response.raise_for_status()
                    decoded: Any = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise RuntimeError("飞书访问令牌获取失败") from exc
            if (
                not isinstance(decoded, dict)
                or int(decoded.get("code", -1)) != 0
                or not decoded.get("tenant_access_token")
            ):
                raise RuntimeError("飞书访问令牌返回失败状态")
            self._token = str(decoded["tenant_access_token"])
            self._token_credential_key = credential_key
            lifetime = max(60, int(decoded.get("expire", 7200)))
            self._token_deadline = monotonic() + max(30, lifetime - 300)
            return self._token

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            trust_env=False,
            transport=self._transport,
        )


def _token_credential_key(credentials: FeishuCredentials) -> bytes:
    """Key the token cache without retaining the App Secret itself."""

    digest = hashlib.sha256()
    digest.update(credentials.app_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(credentials.app_secret.encode("utf-8"))
    return digest.digest()


def _as_safe_notice(
    value: SafeRunNotice | "WorkflowReport",
) -> SafeRunNotice | None:
    if isinstance(value, SafeRunNotice):
        return value
    # Avoid accepting arbitrary duck-typed objects or mappings.  The concrete
    # legacy class is checked here and all of its free-form fields then pass
    # through SafeRunNotice's explicit allow-list converter.
    from ..workflows.types import WorkflowReport

    if not isinstance(value, WorkflowReport):
        raise TypeError("FeishuNotifier.send accepts SafeRunNotice or WorkflowReport")
    return SafeRunNotice.from_workflow_report(value)


def _card_content(notice: SafeRunNotice) -> str:
    """Return Feishu's JSON-in-JSON interactive-card content."""

    card = {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "template": _card_colour(notice.kind),
            "title": {
                "tag": "plain_text",
                "content": _plain(notice.title, fallback=_default_title(notice.kind)),
            },
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": _message(notice)}}
        ],
    }
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))


def _message(notice: SafeRunNotice) -> str:
    lines = [
        f"**任务：** `{_code(notice.run_short_id or '未知')}`",
        f"**店铺：** {_md(notice.store_name or '未知店铺')}",
        f"**状态：** {_md(_kind_label(notice.kind, notice.workflow, notice.mode))}",
    ]
    if notice.schedule_name:
        lines.append(f"**排期：** {_md(notice.schedule_name)}")
    context = " · ".join(
        item
        for item in (
            _mode_label(notice.mode),
            _trigger_label(notice.trigger),
        )
        if item
    )
    if context:
        lines.append(f"**运行：** {_md(context)}")
    if notice.scheduled_for is not None:
        lines.append(f"**计划时间：** {_md(_format_datetime(notice.scheduled_for))}")
    if notice.started_at is not None:
        lines.append(f"**实际开始：** {_md(_format_datetime(notice.started_at))}")
    if notice.queue_delay_minutes is not None:
        lines.append(f"**排队等待：** `{notice.queue_delay_minutes}` 分钟")
    if notice.deadline_at is not None:
        lines.append(f"**处理截止：** {_md(_format_datetime(notice.deadline_at))}")
    if notice.summary:
        lines.extend(("", f"**说明：** {_md(notice.summary)}"))
    if notice.inventory_gpsr_counts is not None:
        counts = notice.inventory_gpsr_counts
        if counts.requirements:
            lines.extend(("", "**库存合规分项汇总**",
                f"- 候选商品 {counts.scanned} 件 ｜ 处理要求 {counts.requirements} 项",
                f"- 安全证明已提交、审核中 {counts.safety_confirmed} 项 ｜ 负责人已提交、审核中 {counts.responsible_confirmed} 项 ｜ 制造商已提交、审核中 {counts.manufacturer_confirmed} 项",
                f"- 已有关联 {counts.already_linked} 项 ｜ 多记录待确认 {counts.multiple_records} 项",
                f"- 无关联记录 {counts.no_records} 项 ｜ 记录失效 {counts.invalid_record} 项 ｜ 非本项要求 {counts.not_applicable} 项",
                f"- 可处理 {counts.eligible} 项 ｜ 尚未检查 {counts.pending} 项 ｜ 此前审核中 {counts.already_submitted} 项",
                f"- 无违规入口 {counts.no_violation_entry} 项 ｜ 无安全证明 {counts.no_attestation} 项 ｜ 未找到 {counts.missing} 项",
                f"- 失败 {counts.failed} 项 ｜ 历史待核对 {counts.blocked} 项 ｜ 结果不明 {counts.uncertain} 项",
                "已有选中项不计为本次提交成功；本次提交确认表示已提交、审核中。"))
        else:
            lines.extend(_legacy_inventory_lines(counts))
    if notice.sites:
        lines.extend(("", "**站点明细**"))
        lines.extend(_site_line(site, notice.workflow) for site in notice.sites)
    if notice.feedback_items:
        heading = (
            "**本次将提交（尚未发出）**"
            if str(notice.mode).lower() == "dry_run"
            else "**本次已提交**"
        )
        lines.extend(("", heading))
        lines.extend(_feedback_line(item) for item in notice.feedback_items)
        if notice.feedback_omitted:
            lines.append(f"- 另有 {notice.feedback_omitted} 条，见本地后台「反馈处理台」")
    action = notice.next_action or _default_next_action(
        notice.kind, notice.workflow, notice.mode
    )
    if action:
        lines.extend(("", f"**下一步：** {_md(action)}"))
    return "\n".join(lines)


def _legacy_inventory_lines(counts):
    return (
        "", "**库存安全证明汇总**",
        f"- 候选扫描 {counts.scanned} 件 ｜ 尚未检查 {counts.pending} 件",
        f"- 历史记录待核对 {counts.blocked} 件 ｜ 已核验可处理 {counts.eligible} 件",
        f"- 本轮提交确认 {counts.confirmed} 件 ｜ 此前审核中 {counts.already_submitted} 件",
        f"- 无违规入口 {counts.no_violation_entry} 件 ｜ 无安全证明 {counts.no_attestation} 件",
        f"- 清单商品未找到 {counts.missing} 件 ｜ 失败 {counts.failed} 件 ｜ 本次提交结果不明 {counts.uncertain} 件",
        "候选扫描仅建立列表清单，不代表已打开侧栏逐件检查；本轮提交确认表示已提交、审核中。",
    )


# Guard states mean a payout was actually requested for that site, so the
# figure alongside them is the amount Amazon was asked to transfer — the
# database builder already prefers ``guard.amount`` over the planned balance.
# 金额前缀跟着「发没发出去」走。ARMED/UNCERTAIN 说不清是否已发出,写「已请求提现」
# 就和同一行的「已锁定（未确认发出）」自相矛盾(09-23 渲染真卡片时读到的)。
_REQUESTED_OUTCOMES = frozenset({"SUBMITTED", "CONFIRMED"})
_LOCKED_OUTCOMES = frozenset({"ARMED", "UNCERTAIN"})


def _site_line(site: SafeSiteNotice, workflow: str = "") -> str:
    parts = [f"**{_md(site.code.upper() or '未知')}**"]
    is_inventory_gpsr = workflow == _INVENTORY_GPSR_WORKFLOW_KEY
    if site.payable is not None and not is_inventory_gpsr:
        # "可提现" is the dashboard balance and keeps moving as orders settle.
        # Once a payout has been requested, report what was requested instead —
        # that is the number the operator actually cares about.
        outcome_key = str(site.outcome).upper()
        if outcome_key in _REQUESTED_OUTCOMES:
            label = "已请求提现"
        elif outcome_key in _LOCKED_OUTCOMES:
            label = "锁定金额"
        else:
            label = "可提现"
        parts.append(f"{label} {_money(site.currency, site.payable)}")
    if site.delayed is not None and not is_inventory_gpsr:
        parts.append(f"延迟资金 {_money(site.currency, site.delayed)}")
    if site.account_tail and not is_inventory_gpsr:
        changed = "（与上次不同）" if site.account_changed else ""
        parts.append(f"账户 `{_account_mask(site.account_tail)}`{changed}")
    if site.reference_suffix and not is_inventory_gpsr:
        parts.append(f"参考号 `••••{_last_four(site.reference_suffix)}`")
    if site.outcome:
        outcome = _outcome_label(site.outcome, workflow)
        reason = _short_reason(site.reason)
        parts.append(f"结果 {_md(outcome + ('：' + reason if reason else ''))}")
    return "- " + " ｜ ".join(parts)


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Seven or more digits with the usual separators — enough to be a phone number
# or an account, not enough to catch a price or a date.
_CONTACT_DIGITS = re.compile(r"(?:\+?\d[\d()\-. ]{5,}\d)")
_DATE_LIKE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}")
# Amazon order ids are 3-7-7; keep the whole thing so it can be looked up.
_ORDER_REF = re.compile(r"[^0-9A-Za-z-]")


def _order_ref(value: object) -> str:
    return _ORDER_REF.sub("", str(value or ""))[:24] or "unknown"


def _mask_contacts(text: str) -> str:
    """Strip contact details out of buyer wording before it leaves the machine.

    Amazon's own removal reason 103 is 「反馈包含了个人识别信息」, so a comment
    containing a phone number or an email is not an edge case — it is one of
    the categories this workflow exists to act on.  The operator can read the
    untouched text on the local Feedback page; a group chat is the wrong place
    for a buyer's phone number.  The module-wide ``_redact`` only masks runs of
    seven or more digits, which lets a formatted number like 555-0142 through.
    """

    text = _EMAIL.sub("[邮箱已隐藏]", text)

    def _mask(match: re.Match[str]) -> str:
        found = match.group(0)
        # A date is not a contact detail, and buyers write them constantly
        # ("包裹 2026-08-29 才到").  Masking those would mangle the very text
        # the operator asked to see.
        if _DATE_LIKE.fullmatch(found.strip()):
            return found
        return "[号码已隐藏]" if sum(c.isdigit() for c in found) >= 7 else found

    return _CONTACT_DIGITS.sub(_mask, text)


def _feedback_line(item: SafeFeedbackNotice) -> str:
    """One review, as stars + the buyer's own words.

    A count alone cannot be checked against the seller account, which is why
    the operator asked for these.  The comment is already truncated by the
    builder; ``_md`` escapes it and the message-level redaction pass still
    applies.
    """

    rating = max(0, min(5, int(item.rating or 0)))
    parts = [f"{'★' * rating}{'☆' * (5 - rating)}"]
    if item.marketplace_code:
        parts.append(f"**{_md(item.marketplace_code.upper())}**")
    if item.amazon_removed:
        parts.append("亚马逊已剔除")
    if item.order_id:
        parts.append(f"`{_order_ref(item.order_id)}`")
    if item.comment:
        parts.append(f"「{_md(_mask_contacts(item.comment))}」")
    if item.reason_label:
        parts.append(f"原因：{_md(item.reason_label)}")
    if str(item.state).upper() == "UNCERTAIN":
        parts.append("**页面未确认**")
    return "- " + " ｜ ".join(parts)


def _card_colour(kind: NotificationKind) -> str:
    if kind is NotificationKind.PAYMENT_SUBMITTED:
        return "green"
    if kind in (NotificationKind.WAITING_APPROVAL, NotificationKind.WAITING_AUTH, NotificationKind.INVENTORY_PAGE_PAUSED):
        return "orange"
    if kind in (
        NotificationKind.AUTH_TIMEOUT,
        NotificationKind.RUN_FAILED,
        NotificationKind.RUN_PARTIAL,
        NotificationKind.UNCERTAIN_FINANCIAL,
        NotificationKind.INVENTORY_SUBMISSION_UNCERTAIN,
    ):
        return "red"
    # RUN_SKIPPED stays blue on purpose.  Nothing went wrong: every site was
    # checked and none had anything to send.  A red card would train the
    # operator to treat Amazon's ordinary 24-hour cap as an incident.
    return "blue"


def _default_title(kind: NotificationKind) -> str:
    return {
        NotificationKind.WAITING_APPROVAL: "提现任务待审核",
        NotificationKind.WAITING_AUTH: "亚马逊自动登录已转人工处理",
        # 不说「超时」:服务重启让验证作废也走这张卡。说明行会讲清是哪一种。
        NotificationKind.AUTH_TIMEOUT: "登录验证未完成",
        NotificationKind.PAYMENT_SUBMITTED: "提现已提交",
        NotificationKind.RUN_COMPLETED: "任务已完成（本次未发出提现）",
        NotificationKind.RUN_FAILED: "提现任务失败",
        NotificationKind.RUN_PARTIAL: "提现任务部分失败",
        NotificationKind.RUN_SKIPPED: "本次没有发起提现",
        NotificationKind.UNCERTAIN_FINANCIAL: "资金结果待人工核对",
        NotificationKind.INVENTORY_SUBMISSION_UNCERTAIN: "安全证明提交结果待核对",
        NotificationKind.INVENTORY_PAGE_PAUSED: "库存安全证明已暂停，等待页面恢复",
        NotificationKind.CROSS_DAY_STARTED: "排队任务跨日开始",
    }[kind]


# A feedback run never reads a balance, so the payout wording below would be a
# false statement about it on three separate lines of the card.
_FEEDBACK_WORKFLOW_KEY = "amazon_feedback_removal"

_FEEDBACK_KIND_LABELS: dict[NotificationKind, str] = {
    NotificationKind.RUN_COMPLETED: "反馈处理已完成",
    NotificationKind.RUN_FAILED: "反馈处理失败",
    NotificationKind.RUN_PARTIAL: "反馈处理部分失败",
    NotificationKind.RUN_SKIPPED: "本次没有可处理的反馈",
}

_FEEDBACK_NEXT_ACTIONS: dict[NotificationKind, str] = {
    NotificationKind.RUN_COMPLETED: (
        "无需操作。逐条结果在本地后台的「反馈处理台」；"
        "标为「待人工」的需要你选一个请求原因，下次运行才会提交。"
    ),
    NotificationKind.RUN_FAILED: "请进入本地后台查看已清洗的失败原因。",
    NotificationKind.RUN_PARTIAL: "请进入本地后台的「反馈处理台」查看哪些站点没跑完。",
    # 全跳过的反馈运行也可能刚记下了「待人工」—— 正文里会列出积压数,这里不能说「无需操作」。
    NotificationKind.RUN_SKIPPED: "本次没有可提交的新反馈；若说明里列出「待人工」，请到反馈处理台选原因。",
}


# GPSR 同样一分钱都不碰，下面的提现措辞对它也是假话。
_GPSR_WORKFLOW_KEY = "amazon_gpsr_attestation"
_INVENTORY_GPSR_WORKFLOW_KEY = "amazon_inventory_gpsr_attestation"

_INVENTORY_GPSR_KIND_LABELS: dict[NotificationKind, str] = {
    NotificationKind.INVENTORY_PAGE_PAUSED: "已暂停，等待页面恢复",
    NotificationKind.INVENTORY_SUBMISSION_UNCERTAIN: "任务已取消，合规信息提交结果待核对",
    NotificationKind.RUN_COMPLETED: "库存合规信息处理完成",
    NotificationKind.RUN_FAILED: "库存合规信息处理未完成",
    NotificationKind.RUN_PARTIAL: "库存合规信息部分处理未完成",
    NotificationKind.RUN_SKIPPED: "本次未提交库存合规信息",
}

_INVENTORY_GPSR_NEXT_ACTIONS: dict[NotificationKind, str] = {
    NotificationKind.INVENTORY_PAGE_PAUSED: "在对应紫鸟店铺确认页面恢复后，到本地原运行详情点击「页面已恢复，继续剩余商品」；不会自动重试或重做已完成项。",
    NotificationKind.INVENTORY_SUBMISSION_UNCERTAIN: "请在对应紫鸟店铺核对审核中状态，并查看本地运行详情；未决记录保留，后续任务只回读，不重复保存。",
    NotificationKind.RUN_COMPLETED: "逐商品结果在本地运行详情；已提交的内容正在等待亚马逊审核。",
    NotificationKind.RUN_FAILED: "请查看本地运行详情；结果不明的商品只回读核查，不重复保存。",
    NotificationKind.RUN_PARTIAL: "请查看本地运行详情中的未完成商品；已提交或结果不明的商品不重复保存。",
    NotificationKind.RUN_SKIPPED: "本次没有新提交；跳过原因和商品清单见本地运行详情。",
}

_GPSR_KIND_LABELS: dict[NotificationKind, str] = {
    NotificationKind.RUN_COMPLETED: "安全证明已完成",
    NotificationKind.RUN_FAILED: "安全证明失败",
    NotificationKind.RUN_PARTIAL: "安全证明部分失败",
    NotificationKind.RUN_SKIPPED: "本次没有提交安全证明",
}

_GPSR_NEXT_ACTIONS: dict[NotificationKind, str] = {
    # 「确认判定无误后再把排期切成自动提交」是 supported_modes 只有 DRY_RUN 那阵子
    # 写的。AUTO 2026-09-17 已经放开，这句话挂在一次 auto 运行上就是错的指引。
    NotificationKind.RUN_COMPLETED: "无需操作。逐条明细在本地后台的运行详情时间线里。",
    NotificationKind.RUN_FAILED: "请进入本地后台查看已清洗的失败原因。",
    NotificationKind.RUN_PARTIAL: "请进入本地后台的运行详情查看哪些站点没跑完。",
    NotificationKind.RUN_SKIPPED: "本次没有提交安全证明；逐站原因见下方站点明细。",
}


# 空跑只读取与判定,什么都没提交:状态不能写「已完成」,下一步要指向「核对后再切自动」。
_DRY_RUN_WORKFLOWS = {
    _INVENTORY_GPSR_WORKFLOW_KEY: "空跑未勾选或保存安全证明。核对运行详情中的候选商品与跳过原因，再切到自动提交。",
    _GPSR_WORKFLOW_KEY: "空跑只读取与判定，没有提交任何安全证明。核对候选条目无误后，再把排期切成自动提交。",
    _FEEDBACK_WORKFLOW_KEY: "空跑只读取与判定，没有向亚马逊提交任何请求。核对反馈处理台里的判定无误后，再切到自动提交。",
}


def _is_dry_run_completion(kind: NotificationKind, workflow: str, mode: str) -> bool:
    return (
        kind is NotificationKind.RUN_COMPLETED
        and mode == "dry_run"
        and workflow in _DRY_RUN_WORKFLOWS
    )


def _kind_label(kind: NotificationKind, workflow: str = "", mode: str = "") -> str:
    if _is_dry_run_completion(kind, workflow, mode):
        return "空跑完成（未提交）"
    if workflow == _INVENTORY_GPSR_WORKFLOW_KEY:
        label = _INVENTORY_GPSR_KIND_LABELS.get(kind)
        if label is not None:
            return label
    if workflow == _GPSR_WORKFLOW_KEY:
        label = _GPSR_KIND_LABELS.get(kind)
        if label is not None:
            return label
    if workflow == _FEEDBACK_WORKFLOW_KEY:
        label = _FEEDBACK_KIND_LABELS.get(kind)
        if label is not None:
            return label
    return _default_title(kind)


def _default_next_action(
    kind: NotificationKind, workflow: str = "", mode: str = ""
) -> str:
    if _is_dry_run_completion(kind, workflow, mode):
        return _DRY_RUN_WORKFLOWS[workflow]
    if workflow == _INVENTORY_GPSR_WORKFLOW_KEY:
        action = _INVENTORY_GPSR_NEXT_ACTIONS.get(kind)
        if action is not None:
            return action
    if workflow == _GPSR_WORKFLOW_KEY:
        action = _GPSR_NEXT_ACTIONS.get(kind)
        if action is not None:
            return action
    if workflow == _FEEDBACK_WORKFLOW_KEY:
        action = _FEEDBACK_NEXT_ACTIONS.get(kind)
        if action is not None:
            return action
    return {
        NotificationKind.WAITING_APPROVAL: "请进入本地后台核对金额并批准或取消。",
        NotificationKind.WAITING_AUTH: "自动尝试已停止；请在对应紫鸟店铺窗口处理验证，再回到本地后台继续。",
        NotificationKind.AUTH_TIMEOUT: "请在本地后台重新发起并人工完成登录验证。",
        NotificationKind.PAYMENT_SUBMITTED: "是否到账请在亚马逊卖家后台核对；本地后台无需操作。",
        NotificationKind.RUN_COMPLETED: "无需操作。本次没有发出提现请求。",
        NotificationKind.RUN_FAILED: "请进入本地后台查看已清洗的失败原因。",
        NotificationKind.RUN_PARTIAL: "请进入本地后台检查失败站点，已成功站点不要重复运行。",
        # 全跳过有两类原因:亚马逊 24 小时限流 / 零余额(无需操作),和读不出站点
        # (需要人看)。卡片只能按 kind 选一句,所以两类都要说到,不能一律「无需操作」。
        NotificationKind.RUN_SKIPPED: (
            "逐站原因见下方。被亚马逊限制 24 小时的，等提示的时间过后会自动重试，"
            "资金仍在亚马逊账户里累积；读取失败的站点请到本地后台查看。"
        ),
        NotificationKind.UNCERTAIN_FINANCIAL: (
            "请到亚马逊卖家后台核对这笔是否已发出，再回本地后台运行详情页选择"
            "「我已在亚马逊核对，结案」或「确认未发出，释放」。切勿手动重新提交。"
        ),
        NotificationKind.INVENTORY_SUBMISSION_UNCERTAIN: "请在对应紫鸟店铺核对安全证明状态，只回读、不重复保存。",
        NotificationKind.CROSS_DAY_STARTED: "任务已按原队列顺序开始，无需重新创建。",
    }[kind]


def _mode_label(value: str) -> str:
    return {
        "dry_run": "只读检查",
        "approval": "人工审核",
        "auto": "自动执行",
    }.get(str(value).lower(), _plain(value))


def _trigger_label(value: str) -> str:
    return {
        "schedule": "定时排期",
        "scheduled": "定时排期",
        "run_now": "立即执行",
        "manual": "手动执行",
        "approval": "审核批准",
        "continue_auth": "验证恢复",
        "reconcile": "资金收口",
        "recovery": "启动恢复",
    }.get(str(value).lower(), _plain(value))


# Every state an operator can see, in Chinese.  Anything missing here used to
# fall through to the raw enum name, so cards routinely showed "SKIPPED" or
# "ARMED" and the people reading them had to ask what that meant.
_OUTCOME_LABELS = {
    "CONFIRMED": "已确认",
    "SUCCEEDED": "检查完成（非提现确认）",
    "PARTIAL": "部分完成",
    # "等待审核" below is the *local* operator's approval queue.  The
    # platform-side wait must not reuse that wording or the operator will
    # look for something to approve that does not exist.
    "WAITING_APPROVAL": "等待审核",
    "WAITING_AUTH": "等待验证",
    # 不写「验证超时」:自动模式是**立刻跳过、根本没等过**,只有真的等满才叫超时。
    # 是哪一种由同一行的原因文字说(2026-09-26 自审)。
    "NEEDS_HUMAN_AUTH": "需人工登录",
    "UNCERTAIN_FINANCIAL": "待人工核对",
    "UNCERTAIN": "待人工核对",
    "FAILED": "执行失败",
    "SKIPPED": "已跳过",
    "CANCELLED": "已取消",
    "PENDING": "待处理",
    "PREFLIGHT": "检查中",
    "PLANNED": "已读取金额",
    "RECONCILING": "收口中",
    "DRY_RUN_COMPLETE": "只读检查完成",
    # Guard states.  ARMED means the database recorded the intent but this
    # system never saw the payout click return, which is emphatically not the
    # same as "submitted" — saying so plainly is the whole point.
    "ARMED": "已锁定（未确认发出）",
    "SUBMITTED": "已发出请求",
}


# 「已跳过」/「已确认」 are payout words.  For a feedback sweep the same two
# states mean "read it, nothing left to send" and "submitted", and the reason
# text beside them says which.
_FEEDBACK_OUTCOME_LABELS = {
    "SKIPPED": "已读取，无需提交",
    "PLANNED": "待提交",
    "CONFIRMED": "已提交请求",
    "PREFLIGHT": "读取中",
}


_GPSR_OUTCOME_LABELS = {
    # 不写「已读取」:跳过也可能是页面根本没读到(停在选择页),原因栏自己会说。
    "SKIPPED": "已跳过",
    "PLANNED": "待提交",
    "CONFIRMED": "已提交安全证明",
    "PREFLIGHT": "读取中",
}


def _outcome_label(value: str, workflow: str = "") -> str:
    key = value.upper()
    if workflow == _INVENTORY_GPSR_WORKFLOW_KEY:
        label = {
            "PAUSED": "已暂停，等待页面恢复",
            "SUCCEEDED": "处理完成",
            "CONFIRMED": "已提交，审核中",
            "SUBMITTED": "等待页面确认",
            "SUBMITTING": "保存结果待核查",
            "UNCERTAIN": "提交结果不明",
            "PLANNED": "检查完成，待处理",
            "PREFLIGHT": "读取中",
        }.get(key)
        if label is not None:
            return label
    if workflow == _GPSR_WORKFLOW_KEY and key in _GPSR_OUTCOME_LABELS:
        return _GPSR_OUTCOME_LABELS[key]
    if workflow == _FEEDBACK_WORKFLOW_KEY and key in _FEEDBACK_OUTCOME_LABELS:
        return _FEEDBACK_OUTCOME_LABELS[key]
    return _OUTCOME_LABELS.get(key, _plain(value))


# Exception class names are an implementation detail; the sentence after them
# is the part written for a human.
_REASON_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:Error|Rejected|Required|Exists|Changed|Dispatched|Limited|Operation)\s*[:：]\s*")


def _short_reason(value: str, *, limit: int = 90) -> str:
    """One scannable clause.  Full text stays in the run-level summary."""

    text = _REASON_PREFIX.sub("", " ".join(str(value or "").split()))
    if not text:
        return ""
    # Cut at the first sentence end so guidance paragraphs do not flood a
    # bullet list, but never mid-number.
    for stop in ("。", "；"):
        head, separator, _ = text.partition(stop)
        if separator and len(head) >= 8:
            text = head
            break
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _money(currency: str, value: Decimal | int | float | str) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
        rendered = format(amount, "f")
    except (InvalidOperation, ValueError):
        rendered = "--"
    code = re.sub(r"[^A-Za-z]", "", str(currency))[:8].upper()
    return f"{code} {rendered}".strip()


def _format_datetime(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def _account_mask(value: str) -> str:
    tail = _last_four(value)
    return f"•••• {tail}" if tail else "••••"


def _last_four(value: str) -> str:
    compact = re.sub(r"\s+", "", str(value))
    suffix = compact[-4:]
    # Identifiers remain code spans, so strip markdown/backtick delimiters.
    return re.sub(r"[^0-9A-Za-z]", "", suffix)[-4:]


def _md(value: object) -> str:
    clean = _plain(value)
    return _MARKDOWN_SPECIAL.sub(r"\\\1", clean)


def _code(value: object) -> str:
    """Small identifiers rendered in a code span without secret heuristics."""

    return re.sub(r"[^0-9A-Za-z_-]", "", str(value))[:8] or "unknown"


def _plain(value: object, *, fallback: str = "") -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    text = _redact(text)
    return text[:_MAX_TEXT] or fallback


def _redact(text: str) -> str:
    """Defence-in-depth for operator-facing free text.

    Builders should already supply a clean summary.  This final renderer-side
    pass prevents common copied error strings from leaking credentials, full
    seller identifiers, local paths, hashes or long account-like numbers.
    """

    text = _WINDOWS_PATH.sub("[本地路径已隐藏]", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)
    text = _CHINESE_SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[已隐藏]", text
    )
    text = _SELLER_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)
    text = _JWT.sub("[令牌已隐藏]", text)
    text = _LONG_HEX.sub("[内部标识已隐藏]", text)
    text = _LONG_DIGITS.sub(lambda match: f"••••{match.group(0)[-4:]}", text)
    return text
