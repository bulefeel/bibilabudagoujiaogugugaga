"""Closed, non-sensitive diagnostics shared by login and setup probes.

Only enums and bounded counters cross into a public setup response. No exception
text, URL, DOM, credential or user-supplied dictionary is serialized here.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

REASON_MESSAGES = {
    "ZINIAO_LAUNCH_FAILED": "紫鸟店铺环境启动未成功，该店建档未执行；请检查紫鸟启动状态后重试",
    "ZINIAO_NETWORK_ERROR": "紫鸟启动接口连续返回网络异常，该店本轮未打开页面、未核验站点；保留原建档并继续下一家",
    "ZINIAO_DEVICE_UNBOUND": "紫鸟提示该账号未绑定设备（104），本次未启动店铺窗口；请先在紫鸟绑定设备，再重试该店",
    "IDENTITY_PAGE_NOT_READY": "当前页面的身份候选未在限定时间内稳定就绪，该项核验未通过",
    "IDENTITY_CURRENT_NOT_FOUND": "当前页面未显示可确认的卖家身份，该项核验未通过",
    "IDENTITY_AMBIGUOUS": "页面显示多个不同卖家身份，已停止该店核验",
    "IDENTITY_LAYOUT_UNKNOWN": "身份页面结构尚未验证，需要采集脱敏样本后适配",
    "IDENTITY_CANDIDATE_LIMIT": "身份候选超过读取上限，未按截断结果判断身份",
    "IDENTITY_MISMATCH": "当前卖家身份与建档不一致，已停止该店核验",
    "SITE_ACCESS_UNPROVEN": "缺少独立的站点访问证据，付款页面能力尚待核验",
    "INVENTORY_ACCESS_UNPROVEN": "卖家身份已读取，但实际商城或库存页面访问尚未核验通过；可重试该站点",
    "UNKNOWN_PAGE": "页面不是已确认的目标业务页面，本次核验未通过",
    "SESSION_CLOSED": "当前店铺页面或会话已关闭，已停止该店核验",
    "AUTH_UNKNOWN": "自动登录已停止，当前没有足够信息确定具体原因",
    "AUTH_OTP_NOT_FILLED": "亚马逊要求验证码，但限定时间内未获得可用的自动填充；本次已跳过，请检查紫鸟二步验证配置或手动登录后重试",
    "AUTH_INPUT_NOT_READY": "紫鸟自动填充尚未就绪或输入尚未稳定，已停止本次推进",
    "AUTH_RETRY_EXHAUSTED": "自动登录已达到本阶段的尝试上限，页面仍未完成验证",
    "AUTH_CHALLENGE": "页面出现额外验证、账户选择或错误提示，已停止自动点击",
    "AUTH_ACTION_AMBIGUOUS": "登录控件或账号选项不唯一，已停止自动点击",
    "AUTH_ACTION_UNAVAILABLE": "登录操作控件缺失、禁用或选中状态不符",
    "AUTH_PAGE_CHANGED": "登录页面或输入已变化，原操作已停止",
    "AUTH_PAGE_CLOSED": "原登录标签页已关闭，且没有唯一可信的接管页面",
    "AUTH_HANDOFF_UNTRUSTED": "登录后的页面不符合当前接管规则，已停止自动接管",
    "AUTH_HANDOFF_AMBIGUOUS": "发现多个可接管页面，已停止自动接管",
    "AUTH_HANDOFF_TIMEOUT": "新业务标签页在限定时间内仍未完成跳转",
    "AUTH_SNAPSHOT_UNAVAILABLE": "当前标签页快照暂不可读取，已停止自动点击",
    "AUTH_PAGE_UNSUPPORTED": "当前登录步骤尚未被识别，已停止自动点击",
    "MARKETPLACE_NOT_OFFERED": "未匹配到可用站点入口，开通情况待确认",
    "MARKETPLACE_LIST_NOT_READY": "站点列表尚未稳定加载，未判断该站点是否可用",
    "MARKETPLACE_AMBIGUOUS": "目标站点入口不唯一，未自动选择",
    "MARKETPLACE_ACTION_UNAVAILABLE": "站点选择控件缺失、被遮挡或状态不符",
    "MARKETPLACE_TARGET_UNKNOWN": "选站后的目标路径尚不明确，未自动选择",
    "MARKETPLACE_LAYOUT_UNKNOWN": "站点选择页结构或状态标记尚未验证，未自动选择",
    "MARKETPLACE_CANDIDATE_LIMIT": "站点候选超过读取上限，未按截断列表选择站点",
    "REGISTRATION_REQUIRED": "目标站点明确显示待注册，本次未自动选择",
    "MARKETPLACE_UNAVAILABLE": "目标站点明确不可用或已禁用，本次未自动选择",
    "MARKETPLACE_UNKNOWN": "站点切换未完成，具体原因待进一步核验",
    "INACTIVE_ACCOUNT": "已进入账户未激活或状态处理页面，本次站点核验未通过",
    "DOMAIN_MISMATCH": "页面域名或连接地址与目标站点不一致，已停止该店核验",
    "NAVIGATION_TIMEOUT": "站点切换在限定时间内未到达目标页面",
}

STAGES = frozenset({
    "unknown", "classify", "session_snapshot", "business_handoff",
    "continue", "password_signin", "otp_signin", "managed_passkey", "saved_account_choice",
    "otp_method_choice", "marketplace_switch", "identity_read", "site_access",
})
ACTIONS = frozenset({
    "continue", "password_signin", "otp_signin", "managed_passkey", "saved_account_choice",
    "otp_method_choice", "select_marketplace",
})
PAGE_CATEGORIES = frozenset({
    "UNKNOWN_PAGE", "LOGIN", "BUSINESS_HANDOFF", "SITE_SWITCHER",
    "INACTIVE_ACCOUNT", "TARGET_PAGE", "DOMAIN_MISMATCH", "CLOSED_PAGE",
})


@dataclass(frozen=True, slots=True)
class SetupDiagnostic:
    reason_code: str = "AUTH_UNKNOWN"
    stage: str = "unknown"
    action: str | None = None
    attempts: int | None = None
    candidate_count: int | None = None
    page_category: str = "UNKNOWN_PAGE"

    def __post_init__(self) -> None:
        # Unknown values never reach logs, even if an older caller passes a URL
        # or an exception message in place of a supported identifier.
        if type(self.reason_code) is not str or self.reason_code not in REASON_MESSAGES:
            object.__setattr__(self, "reason_code", "AUTH_UNKNOWN")
        if type(self.stage) is not str or self.stage not in STAGES:
            object.__setattr__(self, "stage", "unknown")
        if type(self.action) is not str or self.action not in ACTIONS:
            object.__setattr__(self, "action", None)
        if type(self.page_category) is not str or self.page_category not in PAGE_CATEGORIES:
            object.__setattr__(self, "page_category", "UNKNOWN_PAGE")
        for field, maximum in (("attempts", 3), ("candidate_count", 1000)):
            value = getattr(self, field)
            if type(value) is not int or not 0 <= value <= maximum:
                object.__setattr__(self, field, None)

    @property
    def message(self) -> str:
        return REASON_MESSAGES[self.reason_code]

    def to_payload(self) -> dict[str, str | int | None]:
        return {
            "reason_code": self.reason_code,
            "stage": self.stage,
            "action": self.action,
            "attempts": self.attempts,
            "candidate_count": self.candidate_count,
            "page_category": self.page_category,
        }


_SWITCH_REASONS = {
    "not_offered": "MARKETPLACE_NOT_OFFERED",
    "list_not_loaded": "MARKETPLACE_LIST_NOT_READY",
    "layout_unknown": "MARKETPLACE_LAYOUT_UNKNOWN",
    "candidate_limit": "MARKETPLACE_CANDIDATE_LIMIT",
    "registration_required": "REGISTRATION_REQUIRED",
    "unavailable": "MARKETPLACE_UNAVAILABLE",
    "account_mismatch": "IDENTITY_MISMATCH",
    "ambiguous": "MARKETPLACE_AMBIGUOUS",
    "inactive_account": "INACTIVE_ACCOUNT",
    "domain_mismatch": "DOMAIN_MISMATCH",
    "timeout": "NAVIGATION_TIMEOUT",
    "unknown_marketplace": "MARKETPLACE_TARGET_UNKNOWN",
    "target_unknown": "MARKETPLACE_TARGET_UNKNOWN",
    "list_changed": "MARKETPLACE_ACTION_UNAVAILABLE",
    "obstructed": "MARKETPLACE_ACTION_UNAVAILABLE",
    "confirm_not_unique": "MARKETPLACE_AMBIGUOUS",
    "confirm_disabled": "MARKETPLACE_ACTION_UNAVAILABLE",
    "selection_not_registered": "MARKETPLACE_ACTION_UNAVAILABLE",
    "auth_required": "AUTH_CHALLENGE",
}


def switch_diagnostic(reason: str, *, offered: int = 0,
                      page_category: str = "SITE_SWITCHER") -> SetupDiagnostic:
    return SetupDiagnostic(
        reason_code=_SWITCH_REASONS.get(reason, "MARKETPLACE_UNKNOWN"),
        stage="marketplace_switch", action="select_marketplace",
        candidate_count=offered, page_category=page_category,
    )


def classify_switch_landing(url: str, *, domain: str,
                            expected_path: str) -> str:
    """Classify an observed URL, never an instruction to follow it.

    This is deliberately exact for the host and inactive-account path. The
    existing target route's trailing-slash compatibility is kept unchanged.
    """
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != domain.lower()
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443)):
            return "DOMAIN_MISMATCH"
    except (ValueError, TypeError):
        return "DOMAIN_MISMATCH"
    if parts.path == "/ap" or parts.path.startswith("/ap/"):
        return "LOGIN"
    if parts.path == "/inactiveAccount":
        return "INACTIVE_ACCOUNT"
    if parts.path.rstrip("/") == expected_path.rstrip("/"):
        return "TARGET_PAGE"
    prefix = "/account-switcher"
    if parts.path == prefix or parts.path.startswith(prefix + "/"):
        return "SITE_SWITCHER"
    return "UNKNOWN_PAGE"
