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
from .dto import NotificationKind, SafeRunNotice, SafeSiteNotice

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
        f"**状态：** {_md(_kind_label(notice.kind))}",
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
    if notice.sites:
        lines.extend(("", "**站点明细**"))
        lines.extend(_site_line(site) for site in notice.sites)
    action = notice.next_action or _default_next_action(notice.kind)
    if action:
        lines.extend(("", f"**下一步：** {_md(action)}"))
    return "\n".join(lines)


# Guard states mean a payout was actually requested for that site, so the
# figure alongside them is the amount Amazon was asked to transfer — the
# database builder already prefers ``guard.amount`` over the planned balance.
_REQUESTED_OUTCOMES = frozenset({"ARMED", "SUBMITTED", "CONFIRMED", "UNCERTAIN"})


def _site_line(site: SafeSiteNotice) -> str:
    parts = [f"**{_md(site.code.upper() or '未知')}**"]
    if site.payable is not None:
        # "可提现" is the dashboard balance and keeps moving as orders settle.
        # Once a payout has been requested, report what was requested instead —
        # that is the number the operator actually cares about.
        label = (
            "已请求提现"
            if str(site.outcome).upper() in _REQUESTED_OUTCOMES
            else "可提现"
        )
        parts.append(f"{label} {_money(site.currency, site.payable)}")
    if site.delayed is not None:
        parts.append(f"延迟资金 {_money(site.currency, site.delayed)}")
    if site.account_tail:
        changed = "（与上次不同）" if site.account_changed else ""
        parts.append(f"账户 `{_account_mask(site.account_tail)}`{changed}")
    if site.reference_suffix:
        parts.append(f"参考号 `••••{_last_four(site.reference_suffix)}`")
    if site.outcome:
        outcome = _outcome_label(site.outcome)
        reason = _short_reason(site.reason)
        parts.append(f"结果 {_md(outcome + ('：' + reason if reason else ''))}")
    return "- " + " ｜ ".join(parts)


def _card_colour(kind: NotificationKind) -> str:
    if kind is NotificationKind.PAYMENT_CONFIRMED:
        return "green"
    if kind in (NotificationKind.WAITING_APPROVAL, NotificationKind.WAITING_AUTH):
        return "orange"
    if kind in (
        NotificationKind.AUTH_TIMEOUT,
        NotificationKind.RUN_FAILED,
        NotificationKind.RUN_PARTIAL,
        NotificationKind.UNCERTAIN_FINANCIAL,
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
        NotificationKind.AUTH_TIMEOUT: "登录验证等待超时",
        NotificationKind.PAYMENT_CONFIRMED: "提现结果已确认",
        NotificationKind.RUN_COMPLETED: "任务检查已完成（非提现确认）",
        NotificationKind.RUN_FAILED: "提现任务失败",
        NotificationKind.RUN_PARTIAL: "提现任务部分失败",
        NotificationKind.RUN_SKIPPED: "本次无站点可提现",
        NotificationKind.UNCERTAIN_FINANCIAL: "提现已发出，平台尚未显示结果",
        NotificationKind.CROSS_DAY_STARTED: "排队任务跨日开始",
    }[kind]


def _kind_label(kind: NotificationKind) -> str:
    return _default_title(kind)


def _default_next_action(kind: NotificationKind) -> str:
    return {
        NotificationKind.WAITING_APPROVAL: "请进入本地后台核对金额并批准或取消。",
        NotificationKind.WAITING_AUTH: "自动尝试已停止；请在对应紫鸟店铺窗口处理验证，再回到本地后台继续。",
        NotificationKind.AUTH_TIMEOUT: "请在本地后台重新发起并人工完成登录验证。",
        NotificationKind.PAYMENT_CONFIRMED: "无需操作，可在本地后台查看完整运行证据。",
        NotificationKind.RUN_COMPLETED: "无需操作；如需确认提现结果，请以绿色“提现结果已确认”通知为准。",
        NotificationKind.RUN_FAILED: "请进入本地后台查看已清洗的失败原因。",
        NotificationKind.RUN_PARTIAL: "请进入本地后台检查失败站点，已成功站点不要重复运行。",
        NotificationKind.RUN_SKIPPED: (
            "无需操作。若站点显示被亚马逊限制 24 小时，等提示的时间过后会自动重试；"
            "资金仍在亚马逊账户里继续累积，不会丢失。"
        ),
        NotificationKind.UNCERTAIN_FINANCIAL: (
            "亚马逊通常数小时甚至次日才显示结果；稍后回到本地后台点“只回读，不重提”"
            "即可确认。切勿重新提交。"
        ),
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
        "reconcile": "资金回读",
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
    "NEEDS_HUMAN_AUTH": "验证超时",
    "UNCERTAIN_FINANCIAL": "已发出，平台尚未显示",
    "UNCERTAIN": "已发出，平台尚未显示",
    "FAILED": "执行失败",
    "SKIPPED": "已跳过",
    "CANCELLED": "已取消",
    "PENDING": "待处理",
    "PREFLIGHT": "检查中",
    "PLANNED": "已读取金额",
    "RECONCILING": "回读核对中",
    "DRY_RUN_COMPLETE": "只读检查完成",
    # Guard states.  ARMED means the database recorded the intent but this
    # system never saw the payout click return, which is emphatically not the
    # same as "submitted" — saying so plainly is the whole point.
    "ARMED": "已锁定（未确认发出）",
    "SUBMITTED": "已发出请求",
}


def _outcome_label(value: str) -> str:
    return _OUTCOME_LABELS.get(value.upper(), _plain(value))


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
