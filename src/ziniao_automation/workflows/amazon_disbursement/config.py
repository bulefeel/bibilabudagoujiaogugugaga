"""Marketplace allow-list and observed Amazon Payments DOM contract."""

from __future__ import annotations

from dataclasses import dataclass
import re


ALLOWED_MARKETPLACES: dict[str, tuple[str, str]] = {
    "CA": ("sellercentral.amazon.ca", "CAD"),
    "UK": ("sellercentral.amazon.co.uk", "GBP"),
    "AU": ("sellercentral.amazon.com.au", "AUD"),
}


@dataclass(frozen=True, slots=True)
class AmazonDomContract:
    """Selectors verified against the classic Seller Central payments UI."""

    version: str = "payments-classic-v2"

    # Stable seller identity may be a machine ID or the exact account display
    # name recorded by the administrator.  Multiple candidates are ambiguous.
    identity_candidates: str = (
        'meta[name="merchant-id"], meta[name="seller-id"], '
        '[data-merchant-id], [data-seller-id], '
        '[data-testid="seller-id"], [data-testid="merchant-id"], '
        '[data-testid="account-switcher"], '
        '.partner-info, .partner-name, .merchant-name, '
        'kat-dropdown[name="account-switcher"]'
    )
    balance_rows: str = '[class*="multi-row-card-row"]'
    # Compatibility alias used by lightweight row fakes; production row
    # classification reads the row's complete visible/attribute-aware text.
    balance_label: str = '[data-testid="balance-type"]'
    payout_buttons: str = "kat-button"
    amount_elements: str = "kat-link[label], kat-label[label], [label]"
    alerts: str = "kat-alert"

    # Statements use cards/rows rather than test IDs.  The parser deliberately
    # accepts several observed class fragments but still requires exact label
    # pairs inside one row.
    # ``.settlement-groups-summary`` is the observed per-record container on the
    # live zh_CN all-statements page (2026-08-18: 7 records, 7 matches).  It is
    # an exact class token, so it does not also match the nested
    # ``settlement-groups-summary-metadata`` / ``-column`` / ``-search-results``
    # elements, and it wraps BOTH the metadata column (settlement period) and
    # the funds-transfer column (status + amount) — a tighter container such as
    # ``.fund-transfer-box`` or ``.payout-status`` would drop the period and
    # break ``require_today``.  The legacy selectors after it matched zero
    # elements in the field and are kept only for other locales; keep this list
    # free of nested containers so one record can never yield two candidates.
    payment_rows: str = (
        '.settlement-groups-summary, '
        '.disbursement-card, .payout-card, [class*="disbursement-row"], '
        '[class*="payout-row"], [class*="statement-row"]'
    )
    payment_status: str = ".payout-status kat-statusindicator, kat-statusindicator[label]"

    # Real details page is not a dialog.  Labels are bilingual table rows and
    # the final action is a kat-button whose innerText can be empty.
    details_content: str = "main, #content, .a-container, body"
    signin_marker: str = (
        'input[name="email"], input[name="password"], '
        'form[name="signIn"], [data-testid="sign-in-page"]'
    )
    auth_marker: str = (
        '[data-testid="captcha"], [id="auth-captcha-image"], img[src*="captcha" i], '
        'iframe[title*="captcha" i], iframe[src*="captcha" i], '
        'input[name="otpCode"], input[name="code"], '
        'input[id*="otp" i], input[autocomplete="one-time-code"], '
        '[data-testid="passkey-challenge"], [data-testid="webauthn-challenge"], '
        '[data-testid="new-device-verification"], '
        'form[action*="mfa"], form[action*="MFA"], '
        'form[action*="verify"], form[action*="Verify"]'
    )
    auth_text_pattern: re.Pattern[str] = re.compile(
        r"captcha|passkey|security key|verification code|one[- ]time (?:password|code)|"
        r"authenticator app|approve (?:the )?(?:notification|sign[- ]in)|"
        r"验证码|校验码|动态口令|一次性(?:密码|代码)|人机验证|安全验证|身份验证|"
        r"新设备|使用该\s*Passkey\s*登录|批准(?:登录|通知)",
        re.IGNORECASE,
    )
    payable_pattern: re.Pattern[str] = re.compile(
        r"(?:标准订单|Standard\s+Orders?|Standard\s+Order)", re.IGNORECASE
    )
    deferred_pattern: re.Pattern[str] = re.compile(
        r"(?:延迟交易|Deferred\s+Transactions?|Deferred)", re.IGNORECASE
    )
    all_accounts_pattern: re.Pattern[str] = re.compile(
        r"(?:所有账户|All\s+Accounts?)", re.IGNORECASE
    )
    payout_button_pattern: re.Pattern[str] = re.compile(
        r"(?:请求付款|Request\s+disbursement)", re.IGNORECASE
    )
    no_data_pattern: re.Pattern[str] = re.compile(
        r"(?:无付款数据|No\s+payments\s+data)", re.IGNORECASE
    )
    no_data_description_pattern: re.Pattern[str] = re.compile(
        r"(?:您在此商城中没有任何可用的付款数据|You\s+do\s+not\s+have\s+any\s+payments\s+data\s+available\s+in\s+this\s+marketplace)",
        re.IGNORECASE,
    )
    settlement_pattern: re.Pattern[str] = re.compile(
        r"(?:结算周期|Settlement\s+(?:period|ID))\s*[:：]?\s*([A-Za-z0-9_-]{4,})",
        re.IGNORECASE,
    )
    payout_status_pattern: re.Pattern[str] = re.compile(
        r"(?:付款状态|Disbursement\s+status|Payout\s+status)\s*[:：]?\s*([^\n]+)",
        re.IGNORECASE,
    )
    payout_amount_pattern: re.Pattern[str] = re.compile(
        r"(?:付款金额|Disbursement\s+amount|Payout\s+amount)\s*[:：]?\s*([^\n]+)",
        re.IGNORECASE,
    )
    details_amount_pattern: re.Pattern[str] = re.compile(
        r"(?:当前结算金额|Current\s+settlement\s+amount)\s*[:：]?\s*([^\n]+)",
        re.IGNORECASE,
    )
    details_account_pattern: re.Pattern[str] = re.compile(
        r"(?:转入以下尾号的账户|Transfer\s+to\s+(?:the\s+)?account\s+ending\s+in|Account\s+ending\s+in)\s*[:：]?\s*([*•xX\-\s]*\d{2,8})",
        re.IGNORECASE,
    )
    # Amazon throttles on-demand disbursement to once per rolling 24 hours and
    # says so ONLY on the details page — the dashboard's Request disbursement
    # button stays enabled, so no amount of reading the dashboard predicts it.
    # Observed live on 2026-08-18 (AU):
    #   「目前，该账户不符合“按需付款”要求。24 小时内仅限一次。 22 hrs 49 mins 后再次请求。」
    payout_rate_limited_pattern: re.Pattern[str] = re.compile(
        r"(?:24\s*小时内仅限一次|不符合[“\"']?按需付款[”\"']?要求|"
        r"only\s+once\s+(?:every|per|in)\s+24\s*hours|"
        r"not\s+(?:currently\s+)?eligible\s+for\s+on[-\s]?demand)",
        re.IGNORECASE,
    )
    # The remaining wait, quoted back to the operator verbatim.
    payout_retry_after_pattern: re.Pattern[str] = re.compile(
        r"(\d+\s*(?:hrs?|hours?|小时)[^。\n]{0,24}?)(?:后再次请求|before\s+requesting\s+again|later)",
        re.IGNORECASE,
    )
    success_pattern: re.Pattern[str] = re.compile(
        r"(?:已成功启动金额转账|transfer\s+of\s+funds\s+(?:has\s+been\s+)?successfully\s+initiated)",
        re.IGNORECASE,
    )
    confirmed_statuses: tuple[str, ...] = (
        "initiated",
        "in progress",
        "processing started",
        "已开始",
        "已开始处理",
    )
