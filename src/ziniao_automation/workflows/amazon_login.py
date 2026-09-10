"""Narrow, reusable Amazon Seller Central sign-in progression.

This component deliberately knows only four Ziniao-assisted screens:

* an already-filled e-mail/phone field followed by one Continue button;
* one already-filled password field followed by the exact ``#signInSubmit``
  button when Ziniao's managed dialog has handed control back to Amazon;
* an already-filled, plausible OTP field followed by one Sign in button.
* Ziniao's exact managed-account Passkey dialog, rendered inside a closed
  shadow root, followed by its one explicit ``使用该Passkey登录`` button.

It never reads field values into Python and never types credentials/codes.  A
password form that is empty or ambiguous, CAPTCHA, native Passkey prompt,
account selection or error page is still returned to the existing human-auth
lease as :class:`HumanAuthRequired`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import random
import re
from typing import Any, Callable, Literal
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from .errors import HumanAuthRequired


_SELLER_CENTRAL_HOSTS = frozenset(
    {
        "sellercentral.amazon.ca",
        "sellercentral.amazon.co.uk",
        "sellercentral.amazon.com.au",
    }
)
# ``/ap/mfa/new-otp`` is the two-step-verification METHOD CHOOSER Amazon started
# showing when it cannot SMS the seller's number: three radios (WhatsApp, phone
# call, authenticator app) and one submit.  It is listed explicitly rather than
# by prefix — an unknown ``/ap/mfa/<something>`` must keep going to a human
# instead of being auto-clicked on the strength of a path guess.
_LOGIN_PATHS = frozenset({"/ap/signin", "/ap/mfa", "/ap/mfa/new-otp"})
_OTP_METHOD_PATH = "/ap/mfa/new-otp"
# Destinations that count as "the login is over, we are back on business".
#
# The statements page belongs here because the read-back navigates to it: after
# a payout is submitted, ``reconcile`` goes to ``/payments/allstatements/`` to
# look for the platform's record, and Amazon's ``max_auth_age`` step-up can
# interrupt exactly that navigation.  Leaving it out meant the advancer could
# complete the OTP successfully, watch the browser land on the statements page,
# and still never recognise it as a destination — so it burned its whole budget
# and reported "需要人工验证" against a page that was already correct.
# In an anonymised field fixture, OTP succeeded and a payment record was already
# visible on the first row, yet the run still parked shortly afterwards.
#
# It is safe to whitelist unconditionally, unlike ``/payments/disburse/details``
# below: the statements page is a read-only list of past payments and carries no
# control that moves money.  That is why the details path stays behind an
# explicit opt-in and this one does not.
_BUSINESS_EXACT_PATHS = frozenset(
    {
        "/home",
        "/payments/dashboard/index.html",
        "/payments/allstatements/index.html",
        # Feedback Manager.  A business page missing from this set makes a
        # successful login look like "needs a human" — that has already cost
        # one debugging session when the statements page was left out.  The
        # Angular app appends a "#/" fragment, which urlparse keeps out of the
        # path, so the exact match still holds.
        "/feedback-manager/index.html",
    }
)
_PAYMENT_DETAILS_EXACT_PATH = "/payments/disburse/details"
_OTP_METHOD_OPTIONS = """
() => Array.from(document.querySelectorAll('input[type="radio"]')).map((el, index) => {
    const byFor = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
    const wrap = el.closest('label');
    const node = byFor || wrap || el.parentElement;
    return {
        index: index,
        id: el.id || '',
        name: el.getAttribute('name') || '',
        value: el.getAttribute('value') || '',
        checked: el.checked === true,
        disabled: el.disabled === true,
        label: node ? (node.innerText || node.textContent || '').trim().slice(0, 160) : '',
    };
})
"""

_SELECT_OTP_METHOD = """
(index) => {
    const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
    const target = radios[index];
    if (!target || target.disabled) return false;
    target.click();
    return target.checked === true;
}
"""


_LoginAction = Literal[
    "continue",
    "password_signin",
    "otp_signin",
    "managed_passkey",
    # Pick "authenticator app" on the method chooser so the flow reaches the
    # ordinary OTP screen Ziniao already fills.
    "otp_method_choice",
]
_DispatchOutcome = Literal["dispatched", "not_dispatched", "uncertain"]
_BusinessBaseline = tuple[tuple[Any, str], ...]


@dataclass(frozen=True, slots=True)
class AmazonLoginAdvanceResult:
    """Non-sensitive result of one bounded progression attempt."""

    status: Literal["not_login", "advanced"]
    actions: tuple[_LoginAction, ...] = ()
    # A successful Amazon SSO/OTP handoff can open the authenticated business
    # destination in a new tab while leaving the source tab on ``/ap/signin``.
    # Keep the selected Playwright Page out of equality/repr so results and logs
    # remain deterministic and never expose a page URL.
    page: Any | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class _ManagedPasskeyCandidate:
    """One exact managed-Passkey button from a single closed shadow root."""

    node_id: int
    # ``backendNodeId`` is stable for the lifetime of the real DOM node and is
    # replaced when Ziniao injects a genuinely new challenge into the same
    # document.  Lightweight fixtures may omit it and fall back to ``nodeId``.
    backend_node_id: int | None = None


class _DispatchLimitReached(RuntimeError):
    """The bounded automatic click budget for one document stage is spent."""


class _AlreadyOnBusinessPage(RuntimeError):
    """The route reached an allowed business destination before the click.

    Amazon can commit the login during the deliberate pre-click pause, so a
    pre-click validator can find the page already off ``/ap/signin``.  That is
    a *successful* login, and the post-click path already treats it as one.
    This sentinel lets the pre-click validators report the same thing instead
    of raising ``HumanAuthRequired`` and turning a success into a failure.
    """


_logger = logging.getLogger(__name__)


class AmazonLoginAdvancer:
    """Advance only unambiguous, Ziniao-pre-filled Seller Central login UI."""

    _managed_passkey_poll_interval_ms = 250
    # Amazon/Ziniao can remove the OTP form before the authenticated business
    # tab is registered in ``BrowserContext.pages``.  This window must outlast
    # the slowest normal destination, not the typical one.
    #
    # 3_000 was sized against the dashboard, which commits 1.7-1.9 s after a
    # click.  The disbursement details route is slower because reaching it runs
    # a full OpenID re-auth chain (Amazon now states the rule on the page: the
    # session expires after 5 minutes of inactivity).  Field-measured
    # 2026-08-18: a managed-Passkey click dispatched at 03:14:47.769 was
    # abandoned at 03:14:50.881 and the details page committed at 03:14:51 —
    # the run was parked for human verification roughly 150 ms too early.
    #
    # Raising this is close to free: ``_wait_for_business_page`` polls and
    # returns the moment the destination appears, so a longer ceiling costs
    # nothing on the success path and only lets a slow-but-normal navigation
    # finish instead of being misreported as a human-auth challenge.
    _business_handoff_wait_timeout_ms = 12_000
    # How long a post-click page may stay unclassifiable before a human is asked
    # for.  Sized from the field: the managed-Passkey to OTP hop measured 7.1 s
    # and 8.2 s on 2026-08-19, against a hard 3.0 s verdict that dropped a site
    # holding real money.  Like the ceiling above, raising it is close to free —
    # the poll returns the instant the page becomes readable, so this only ever
    # costs time on a login that was going to need a human anyway.
    _transition_settle_timeout_ms = 15_000
    # Raw CDP operations bypass Playwright's normal page timeout machinery.
    # Keep every probe bounded, and treat session detach as best-effort cleanup
    # so a renderer transition cannot strand the whole setup task after a click.
    _managed_passkey_cdp_timeout_seconds = 2.0
    _managed_passkey_detach_timeout_seconds = 1.0
    # Ziniao fills the six OTP digits asynchronously.  A syntactically valid
    # value is therefore not enough: it must remain unchanged in the browser
    # for this whole interval before the Amazon submit control is considered.
    _otp_stable_duration_ms = 2_500
    _otp_max_age_ms = 10_000
    _otp_stability_poll_interval_ms = 250

    _identifier_selector = (
        '#ap_email, input[name="email"], input[type="email"], '
        'input[name="username"], input[autocomplete="username"]'
    )
    _otp_selector = (
        '#auth-mfa-otpcode, input[name="otpCode"], input[name="code"], '
        'input[id*="otp" i], input[autocomplete="one-time-code"]'
    )
    _password_selector = (
        'input[type="password"], input[name="password"], input[name="passwordCheck"]'
    )
    _captcha_selector = (
        '[data-testid="captcha"], [id="auth-captcha-image"], '
        'img[src*="captcha" i], iframe[title*="captcha" i], '
        'iframe[src*="captcha" i]'
    )
    _passkey_selector = (
        '[data-testid="passkey-challenge"], [data-testid="webauthn-challenge"], '
        '[data-testid="new-device-verification"]'
    )
    _account_choice_selector = (
        '[data-testid="account-switcher"], [data-testid="select-account"], '
        '[class*="account-switcher"], [class*="account-selection"]'
    )
    _error_selector = (
        '#auth-error-message-box, .a-alert-error, '
        '[data-testid="auth-error"]'
    )
    _continue_control_selector = (
        'input#continue, button#continue, #continue input[type="submit"], '
        'input[name="continue"], button[name="continue"], '
        'button, input[type="submit"], input[type="button"], [role="button"]'
    )
    _signin_control_selector = (
        'input#auth-signin-button, button#auth-signin-button, '
        '#auth-signin-button input[type="submit"], '
        'input[name="signIn"], button[name="signIn"], '
        'button, input[type="submit"], input[type="button"], [role="button"]'
    )
    # Password fallback is intentionally narrower than the localized MFA
    # submit lookup.  Amazon's standard password form has one exact, stable
    # ``#signInSubmit`` control; accepting a generic ``name=signIn`` element on
    # this route could target an unrelated or newly rendered action.
    _password_signin_control_selector = "#signInSubmit"

    # The one option that keeps the code inside the browser Ziniao controls.
    _otp_method_authenticator = re.compile(
        r"认证器|認證器|验证器|驗證器|身份验证器应用|"
        r"authenticat(?:or|ion)\s*app|one[-\s]?time\s*password.*app|"
        r"totp|auth[_-]?app",
        re.IGNORECASE,
    )
    # ⚠️ The options that reach the seller's real phone.  Submitting with one of
    # these selected sends them a WhatsApp message or rings them.  This pattern
    # is used as a NEGATIVE assertion immediately before the submit click: even
    # if the positive match above were wrong, the dangerous branch is refused.
    # Deliberately broad: over-matching only sends the page to a human, while
    # under-matching is what actually messages the seller.
    _otp_method_delivery = re.compile(
        r"whatsapp|短信|简讯|簡訊|sms|text\s*(?:me|message)|"
        r"打电话|打電話|来电|來電|拨打|撥打|phone\s*call|call\s+me|voice|"
        r"发送(?:到|至)|send.*(?:to\s+my|code\s+to)|"
        r"邮件|邮箱|電子郵件|e-?mail",
        re.IGNORECASE,
    )
    _otp_method_submit_selector = (
        '#auth-send-code, #auth-continue, input#continue, button#continue, '
        'button[type="submit"], input[type="submit"], button[type="button"]'
    )
    _otp_method_send_label = re.compile(
        r"(?:\u53d1\u9001|\u83b7\u53d6|\u7ee7\u7eed).*?(?:\u4e00\u6b21\u6027\u5bc6\u7801|\u9a8c\u8bc1\u7801)|"
        r"(?:send|request|get|continue).*?(?:one[-\s]?time|otp|verification)\s*(?:password|code)?",
        re.IGNORECASE,
    )

    _continue_label = re.compile(r"^(?:continue|继续|繼續)$", re.IGNORECASE)
    _signin_label = re.compile(
        r"^(?:sign\s*in|log\s*in|登录|登入|登錄)$", re.IGNORECASE
    )
    _unsupported_text = re.compile(
        r"captcha|passkey|security\s*key|choose\s+(?:an?\s+)?account|"
        # Do not reject the generic Chinese word for "password" here.  The
        # normal Chinese MFA prompt calls its OTP an ``一次性密码``; matching
        # ``密码`` therefore diverted an already-filled, supported OTP screen
        # into the human-auth lease before the field could be classified.
        # A real password screen is still rejected above by its password input
        # selector, without reading or typing that field.
        r"select\s+(?:an?\s+)?account|验证码图片|人机验证|安全密钥|"
        r"选择账户|選擇帳戶|发生错误|發生錯誤|there\s+was\s+(?:an?\s+)?error",
        re.IGNORECASE,
    )
    # A normal Amazon password form can contain a secondary "sign in with a
    # passkey/security key" link.  That text alone is not the active challenge
    # and must not hide the exact prefilled-password fallback.  Text that
    # clearly represents an error, CAPTCHA, or account choice remains blocking;
    # structural Passkey/challenge selectors are checked separately above.
    _password_fallback_blocking_text = re.compile(
        r"captcha|choose\s+(?:an?\s+)?account|select\s+(?:an?\s+)?account|"
        r"验证码图片|人机验证|选择账户|選擇帳戶|发生错误|發生錯誤|"
        r"there\s+was\s+(?:an?\s+)?error",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        max_steps: int = 3,
        max_action_attempts: int = 3,
        max_validation_rounds: int = 3,
        pacing_range_ms: tuple[int, int] = (2_000, 3_000),
        pacing_random: Callable[[float, float], float] = random.uniform,
        prefill_wait_timeout_ms: int = 15_000,
        otp_prefill_wait_timeout_ms: int = 60_000,
        managed_passkey_wait_timeout_ms: int = 5_000,
    ) -> None:
        if not 1 <= max_steps <= 3:
            raise ValueError("Amazon 登录自动推进最多只能配置 1 到 3 步")
        if not 1 <= max_action_attempts <= 3:
            raise ValueError("Amazon 每个登录页面最多只能配置 1 到 3 次自动点击")
        if not 1 <= max_validation_rounds <= 3:
            raise ValueError("Amazon 登录验证最多只能配置 1 到 3 轮")
        low, high = pacing_range_ms
        if low < 0 or high < low:
            raise ValueError("登录推进停顿范围必须是有序的非负毫秒数")
        if prefill_wait_timeout_ms < 0:
            raise ValueError("紫鸟账号自动填充等待时间不能为负数")
        if otp_prefill_wait_timeout_ms < 0:
            raise ValueError("紫鸟 OTP 自动填充等待时间不能为负数")
        if managed_passkey_wait_timeout_ms < 0:
            raise ValueError("紫鸟托管 Passkey 弹窗等待时间不能为负数")
        self.max_steps = int(max_steps)
        self._max_action_attempts = int(max_action_attempts)
        self._max_validation_rounds = int(max_validation_rounds)
        self._pacing_range_ms = (int(low), int(high))
        self._pacing_random = pacing_random
        self._prefill_wait_timeout_ms = int(prefill_wait_timeout_ms)
        self._otp_prefill_wait_timeout_ms = int(otp_prefill_wait_timeout_ms)
        self._managed_passkey_wait_timeout_ms = int(
            managed_passkey_wait_timeout_ms
        )
        # Keep guards attached to the actual Playwright Page object rather than
        # its numeric ``id``.  A long-running batch destroys many Page wrappers;
        # CPython may then reuse an old id for the next Ziniao store and the old
        # implementation incorrectly treated that new store as already clicked.
        # Weak keys also release every guard as soon as its Page is gone.
        self._dispatch_attempts: WeakKeyDictionary[
            Any, dict[tuple[str, str, str], int]
        ] = WeakKeyDictionary()
        # The first before-click Page snapshot belongs to the whole live auth
        # lease, not merely one HTTP/worker invocation.  If a business tab
        # appears just after a bounded poll and the operator presses Continue,
        # reusing this baseline lets us adopt that tab while still excluding
        # dashboards that predated the original login attempt.
        self._business_baselines: WeakKeyDictionary[
            Any, dict[str, _BusinessBaseline | None]
        ] = WeakKeyDictionary()

    async def advance(
        self,
        page: Any,
        *,
        expected_host: str | None = None,
        allow_payment_details_handoff: bool = False,
    ) -> AmazonLoginAdvanceResult:
        """Advance a bounded number of assisted-login validation rounds."""

        url = str(getattr(page, "url", "") or "")
        if not self.is_supported_login_url(url, expected_host=expected_host):
            return AmazonLoginAdvanceResult("not_login")

        actions: list[_LoginAction] = []
        business_baseline = self._business_baseline_for(
            page,
            expected_host=expected_host,
        )
        if business_baseline is None:
            raise HumanAuthRequired(
                "Amazon 登录前无法建立可信标签页快照，已停止自动点击",
                kind="sign_in",
            )
        business_page = self.resolve_business_page(
            page,
            expected_host=expected_host,
            baseline=business_baseline,
            allow_payment_details_handoff=allow_payment_details_handoff,
        )
        if business_page is not None:
            _logger.info("Amazon assisted login adopted unique business tab")
            return self._advanced_result(
                page,
                expected_host=expected_host,
                business_page=business_page,
            )
        if self._page_is_closed(page) or self._has_pending_handoff_page(
            page,
            expected_host=expected_host,
            baseline=business_baseline,
            allow_payment_details_handoff=allow_payment_details_handoff,
        ):
            business_page = await self._wait_for_business_page(
                page,
                expected_host=expected_host,
                baseline=business_baseline,
                timeout_ms=self._business_handoff_wait_timeout_ms,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            if business_page is not None:
                return self._advanced_result(
                    page,
                    expected_host=expected_host,
                    business_page=business_page,
                )
            if self._page_is_closed(page):
                raise HumanAuthRequired(
                    "Amazon 原登录标签页已关闭且未找到唯一业务标签页，已停止自动点击",
                    kind="sign_in",
                )
        pending_action: _LoginAction | None = None
        validation_rounds = 0
        round_needs_start = True
        # One round can legitimately traverse managed Passkey → password
        # fallback → managed Passkey → OTP.  Keep a separate hard transition
        # ceiling so three complete rounds are possible but loops stay finite.
        transition_limit = 1 + (self.max_steps + 1) * self._max_validation_rounds
        for _ in range(transition_limit):
            business_page = self.resolve_business_page(
                page,
                expected_host=expected_host,
                baseline=business_baseline,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            if business_page is not None:
                _logger.info("Amazon assisted login adopted unique business tab")
                return self._advanced_result(
                    page,
                    expected_host=expected_host,
                    actions=tuple(actions),
                    business_page=business_page,
                )
            if self._page_is_closed(page) or self._has_pending_handoff_page(
                page,
                expected_host=expected_host,
                baseline=business_baseline,
                allow_payment_details_handoff=allow_payment_details_handoff,
            ):
                business_page = await self._wait_for_business_page(
                    page,
                    expected_host=expected_host,
                    baseline=business_baseline,
                    timeout_ms=self._business_handoff_wait_timeout_ms,
                    allow_payment_details_handoff=allow_payment_details_handoff,
                )
                if business_page is not None:
                    return self._advanced_result(
                        page,
                        expected_host=expected_host,
                        actions=tuple(actions),
                        business_page=business_page,
                    )
                if self._page_is_closed(page):
                    raise HumanAuthRequired(
                        "Amazon 原登录标签页已关闭且未找到唯一业务标签页，已停止自动点击",
                        kind="sign_in",
                    )
            if pending_action is None:
                action = await self._classify(page, expected_host=expected_host)
            else:
                # ``_advance_action_with_retries`` has already classified this
                # exact next stage after the preceding click.  Reuse it once so
                # a revealed password fallback is paced for 2-3 seconds instead
                # of starting another managed-Passkey polling window.  Every
                # control and route is still revalidated immediately before its
                # click, so this is not a stale-element shortcut.
                action = pending_action
                pending_action = None
            if action is None:
                # A successful click can already have navigated away from the
                # login route.  That is a normal handoff to the page adapter.
                current_url = str(getattr(page, "url", "") or "")
                if self.is_supported_business_url(
                    current_url,
                    expected_host=self._business_host(page, expected_host),
                    allow_payment_details_handoff=allow_payment_details_handoff,
                ):
                    return self._advanced_result(
                        page,
                        expected_host=expected_host,
                        actions=tuple(actions),
                        business_page=page,
                    )
                raise HumanAuthRequired(
                    "Amazon 登录页面不属于可自动推进的受支持步骤",
                    kind="sign_in",
                )

            if action in {
                "password_signin",
                "managed_passkey",
                "otp_signin",
            } and round_needs_start:
                validation_rounds += 1
                round_needs_start = False
            if validation_rounds > self._max_validation_rounds:
                raise HumanAuthRequired(
                    "Amazon 登录验证已达到最多三轮，请人工检查当前页面",
                    kind="challenge",
                )

            (
                dispatched,
                pending_action,
                business_page,
            ) = await self._advance_action_with_retries(
                page,
                action,
                expected_host=expected_host,
                business_baseline=business_baseline,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            if dispatched:
                actions.append(action)
            if business_page is not None:
                return self._advanced_result(
                    page,
                    expected_host=expected_host,
                    actions=tuple(actions),
                    business_page=business_page,
                )
            if action == "otp_signin":
                # Returning to a login route after OTP means the next assisted
                # password/Passkey/OTP action starts a fresh validation round.
                round_needs_start = True

            current_url = str(getattr(page, "url", "") or "")
            if not self.is_supported_login_url(
                current_url,
                expected_host=expected_host,
            ):
                if not self.is_supported_business_url(
                    current_url,
                    expected_host=self._business_host(page, expected_host),
                    allow_payment_details_handoff=allow_payment_details_handoff,
                ):
                    raise HumanAuthRequired(
                        "Amazon 登录后进入了未列入白名单的页面，已停止自动接管",
                        kind="sign_in",
                    )
                return self._advanced_result(
                    page,
                    expected_host=expected_host,
                    actions=tuple(actions),
                    business_page=page,
                )

        # Never exceed the configured bound even if Amazon renders another
        # supported-looking page.  The normal auth lease decides what follows.
        raise HumanAuthRequired(
            "Amazon 登录自动推进已达到有限轮次，请人工检查当前页面",
            kind="sign_in",
        )

    def _business_baseline_for(
        self,
        source_page: Any,
        *,
        expected_host: str | None,
    ) -> _BusinessBaseline | None:
        host = self._business_host(source_page, expected_host)
        try:
            values = self._business_baselines.get(source_page)
            if values is None:
                values = {}
                self._business_baselines[source_page] = values
        except TypeError:
            attribute = "_ziniao_assisted_login_business_baselines"
            values = getattr(source_page, attribute, None)
            if not isinstance(values, dict):
                values = {}
                setattr(source_page, attribute, values)
        if host not in values:
            values[host] = self._context_business_snapshot(
                source_page,
                expected_host=expected_host,
            )
        return values[host]

    def _release_business_baseline(
        self,
        source_page: Any,
        *,
        expected_host: str | None,
    ) -> None:
        host = self._business_host(source_page, expected_host)
        try:
            values = self._business_baselines.get(source_page)
        except TypeError:
            values = getattr(
                source_page,
                "_ziniao_assisted_login_business_baselines",
                None,
            )
        if isinstance(values, dict):
            values.pop(host, None)

    def _advanced_result(
        self,
        source_page: Any,
        *,
        expected_host: str | None,
        business_page: Any,
        actions: tuple[_LoginAction, ...] = (),
    ) -> AmazonLoginAdvanceResult:
        self._release_business_baseline(
            source_page,
            expected_host=expected_host,
        )
        return AmazonLoginAdvanceResult("advanced", actions, business_page)

    async def _advance_action_with_retries(
        self,
        page: Any,
        action: _LoginAction,
        *,
        expected_host: str | None,
        business_baseline: _BusinessBaseline | None,
        allow_payment_details_handoff: bool = False,
    ) -> tuple[bool, _LoginAction | None, Any | None]:
        """Dispatch one logical login stage with bounded automatic retries.

        A retry never consumes another ``max_steps`` slot.  After each click we
        first reclassify the live page: leaving the login route or seeing the
        next supported action is success, while an explicit Amazon error,
        CAPTCHA, account choice, changed/expired OTP, or ambiguous control is
        handed to the existing human-auth lease.  Only the unchanged exact
        action is eligible for another click, up to the configured limit.
        """

        # The first managed Passkey page (and the optional identifier Continue
        # page before it) is left visible for a human-like 2–3 seconds.  OTP has
        # already spent 2.5 seconds proving that the same six digits are stable,
        # so adding another initial pause would needlessly consume its lifetime.
        if action != "otp_signin":
            await self._paced_pause(page)

        if action == "otp_signin" and not await self._capture_otp_retry_challenge(
            page
        ):
            raise HumanAuthRequired(
                "Amazon OTP 在首次提交前发生变化、过期或尚未稳定",
                kind="challenge",
            )

        dispatched_any = False
        last_error: BaseException | None = None
        for local_attempt in range(1, self._max_action_attempts + 1):
            _logger.info(
                "Amazon assisted login action attempt: action=%s attempt=%d/%d",
                action,
                local_attempt,
                self._max_action_attempts,
            )
            try:
                outcome, error = await self._dispatch_action_once(
                    page,
                    action,
                    expected_host=expected_host,
                    allow_payment_details_handoff=allow_payment_details_handoff,
                )
            except _DispatchLimitReached as exc:
                _logger.warning(
                    "Amazon assisted login retry exhausted: action=%s attempts=%d",
                    action,
                    self._max_action_attempts,
                )
                raise HumanAuthRequired(
                    "Amazon 登录按钮已完成有限自动重试，页面仍未进入下一步",
                    kind="challenge" if action == "otp_signin" else "sign_in",
                ) from exc

            if outcome in {"dispatched", "uncertain"}:
                dispatched_any = True
                _logger.info(
                    "Amazon assisted login post-dispatch reclassification started: "
                    "action=%s outcome=%s",
                    action,
                    outcome,
                )
            if error is not None:
                last_error = error

            # Give click-triggered URL/DOM mutations one event-loop turn before
            # asking whether the logical action is still present.
            await asyncio.sleep(0)
            business_page = self.resolve_business_page(
                page,
                expected_host=expected_host,
                baseline=business_baseline,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            if business_page is not None:
                _logger.info(
                    "Amazon assisted login page transition: from=%s to=business_tab",
                    action,
                )
                return dispatched_any, None, business_page
            if self._page_is_closed(page) or self._has_pending_handoff_page(
                page,
                expected_host=expected_host,
                baseline=business_baseline,
                allow_payment_details_handoff=allow_payment_details_handoff,
            ):
                business_page = await self._wait_for_business_page(
                    page,
                    expected_host=expected_host,
                    baseline=business_baseline,
                    timeout_ms=self._business_handoff_wait_timeout_ms,
                    allow_payment_details_handoff=allow_payment_details_handoff,
                )
                if business_page is not None:
                    _logger.info(
                        "Amazon assisted login page transition: from=%s to=%s",
                        action,
                        "business" if business_page is page else "business_tab",
                    )
                    return dispatched_any, None, business_page
                if self._page_is_closed(page):
                    raise HumanAuthRequired(
                        "Amazon 原登录标签页已关闭且未找到唯一业务标签页，已停止自动点击",
                        kind="sign_in",
                    )
            current_url = self._safe_page_url(page)
            if not self.is_supported_login_url(
                current_url,
                expected_host=expected_host,
            ):
                if not self.is_supported_business_url(
                    current_url,
                    expected_host=self._business_host(page, expected_host),
                    allow_payment_details_handoff=allow_payment_details_handoff,
                ):
                    raise HumanAuthRequired(
                        "Amazon 登录后进入了未列入白名单的页面，已停止自动接管",
                        kind="sign_in",
                    )
                _logger.info(
                    "Amazon assisted login page transition: from=%s to=business",
                    action,
                )
                return dispatched_any, None, None
            if (
                action == "otp_signin"
                and urlparse(self._safe_page_url(page))
                .path.rstrip("/").lower()
                != "/ap/mfa"
            ):
                _logger.info(
                    "Amazon assisted login page transition: from=%s to=signin",
                    action,
                )
                return dispatched_any, None, None

            if action == "otp_signin" and not await self._otp_retry_challenge_unchanged(
                page
            ):
                business_page = await self._wait_for_business_page(
                    page,
                    expected_host=expected_host,
                    baseline=business_baseline,
                    timeout_ms=self._business_handoff_wait_timeout_ms,
                    allow_payment_details_handoff=allow_payment_details_handoff,
                )
                if business_page is not None:
                    _logger.info(
                        "Amazon assisted login page transition: "
                        "from=otp_signin to=%s",
                        "business" if business_page is page else "business_tab",
                    )
                    return dispatched_any, None, business_page
                raise HumanAuthRequired(
                    "Amazon OTP 已变化、过期或更换输入框，本次不自动重试",
                    kind="challenge",
                )
            next_action = await self._classify(
                page,
                expected_host=expected_host,
                wait_for_managed_passkey=False,
            )
            if next_action is not None and next_action != action:
                _logger.info(
                    "Amazon assisted login page transition: from=%s to=%s",
                    action,
                    next_action,
                )
                return dispatched_any, next_action, None
            if next_action is None:
                _logger.info(
                    "Amazon assisted login transition pending: from=%s",
                    action,
                )

            # The exact same login stage survived the first attempt.  Wait a
            # further 2–3 seconds so delayed navigation or Ziniao DOM updates
            # can finish before the final recheck and possible retry.
            await self._paced_pause(page)
            business_page = self.resolve_business_page(
                page,
                expected_host=expected_host,
                baseline=business_baseline,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            if business_page is not None:
                _logger.info(
                    "Amazon assisted login page transition: from=%s to=business_tab",
                    action,
                )
                return dispatched_any, None, business_page
            if self._page_is_closed(page) or self._has_pending_handoff_page(
                page,
                expected_host=expected_host,
                baseline=business_baseline,
                allow_payment_details_handoff=allow_payment_details_handoff,
            ):
                business_page = await self._wait_for_business_page(
                    page,
                    expected_host=expected_host,
                    baseline=business_baseline,
                    timeout_ms=self._business_handoff_wait_timeout_ms,
                    allow_payment_details_handoff=allow_payment_details_handoff,
                )
                if business_page is not None:
                    _logger.info(
                        "Amazon assisted login page transition: from=%s to=%s",
                        action,
                        "business" if business_page is page else "business_tab",
                    )
                    return dispatched_any, None, business_page
                if self._page_is_closed(page):
                    raise HumanAuthRequired(
                        "Amazon 原登录标签页已关闭且未找到唯一业务标签页，已停止自动点击",
                        kind="sign_in",
                    )
            current_url = self._safe_page_url(page)
            if not self.is_supported_login_url(
                current_url,
                expected_host=expected_host,
            ):
                if not self.is_supported_business_url(
                    current_url,
                    expected_host=self._business_host(page, expected_host),
                    allow_payment_details_handoff=allow_payment_details_handoff,
                ):
                    raise HumanAuthRequired(
                        "Amazon 登录后进入了未列入白名单的页面，已停止自动接管",
                        kind="sign_in",
                    )
                _logger.info(
                    "Amazon assisted login page transition: from=%s to=business",
                    action,
                )
                return dispatched_any, None, None
            if (
                action == "otp_signin"
                and urlparse(self._safe_page_url(page))
                .path.rstrip("/").lower()
                != "/ap/mfa"
            ):
                _logger.info(
                    "Amazon assisted login page transition: from=%s to=signin",
                    action,
                )
                return dispatched_any, None, None
            if action == "otp_signin" and not await self._otp_retry_challenge_unchanged(
                page
            ):
                business_page = await self._wait_for_business_page(
                    page,
                    expected_host=expected_host,
                    baseline=business_baseline,
                    timeout_ms=self._business_handoff_wait_timeout_ms,
                    allow_payment_details_handoff=allow_payment_details_handoff,
                )
                if business_page is not None:
                    _logger.info(
                        "Amazon assisted login page transition: "
                        "from=otp_signin to=%s",
                        "business" if business_page is page else "business_tab",
                    )
                    return dispatched_any, None, business_page
                raise HumanAuthRequired(
                    "Amazon OTP 已变化、过期或更换输入框，本次不自动重试",
                    kind="challenge",
                )
            next_action, left_login_flow = await self._settled_classification(
                page,
                action,
                expected_host=expected_host,
            )
            if left_login_flow:
                if not self.is_supported_business_url(
                    self._safe_page_url(page),
                    expected_host=self._business_host(page, expected_host),
                    allow_payment_details_handoff=allow_payment_details_handoff,
                ):
                    raise HumanAuthRequired(
                        "Amazon 登录后进入了未列入白名单的页面，已停止自动接管",
                        kind="sign_in",
                    )
                _logger.info(
                    "Amazon assisted login page transition: from=%s to=business",
                    action,
                )
                return dispatched_any, None, None
            if next_action is not None and next_action != action:
                _logger.info(
                    "Amazon assisted login page transition: from=%s to=%s",
                    action,
                    next_action,
                )
                return dispatched_any, next_action, None
            if next_action is None:
                _logger.warning(
                    "Amazon assisted login transition unresolved: from=%s",
                    action,
                )
                raise HumanAuthRequired(
                    "Amazon 登录点击后页面处于无法确认的过渡状态，请人工检查",
                    kind="challenge" if action == "otp_signin" else "sign_in",
                )

            if local_attempt < self._max_action_attempts:
                _logger.info(
                    "Amazon assisted login action retrying: action=%s attempt=%d",
                    action,
                    local_attempt + 1,
                )
                continue

            label = {
                "managed_passkey": "紫鸟托管 Passkey",
                "password_signin": "密码登录",
                "otp_signin": "OTP 登录",
                "continue": "邮箱 Continue",
            }[action]
            _logger.warning(
                "Amazon assisted login retry exhausted: action=%s attempts=%d",
                action,
                self._max_action_attempts,
            )
            failure = HumanAuthRequired(
                f"Amazon {label}自动重试后页面仍未进入下一步，请人工检查",
                kind="challenge" if action == "otp_signin" else "sign_in",
            )
            if last_error is not None:
                raise failure from last_error
            raise failure

        return dispatched_any, None, None

    async def _settled_classification(
        self,
        page: Any,
        action: _LoginAction,
        *,
        expected_host: str | None,
    ) -> tuple[_LoginAction | None, bool]:
        """Poll until the post-click page settles, or the budget runs out.

        Returns ``(classification, left_login_flow)``.

        ``_classify`` returning ``None`` at this point has exactly one meaning:
        the URL is still inside the login flow, but the document shows no
        identifier, OTP or password control at all.  That is what a page mid-
        navigation looks like — and it was being treated as "unresolvable, hand
        this to a human", ending the site.

        Field measurement (2026-08-19) says that verdict was simply premature.
        Amazon's managed-Passkey to OTP hop took **7.1 s and 8.2 s** on the two
        runs that succeeded, while the caller gave up 3.0 s after dispatch — so
        a site with an available balance was dropped for "验证超时" on a login that was
        merely still painting.  The operator's account of it is exact: the click
        happened, the page had not finished loading, and the automation had
        already moved to the next marketplace.

        This poll never clicks and never navigates; it only re-reads.  A genuine
        problem still surfaces immediately, because ``_classify`` raises
        :class:`HumanAuthRequired` for CAPTCHA, account choosers, ambiguous
        fields and unfilled credentials, and those propagate straight out of
        here rather than being retried into the timeout.
        """

        elapsed_ms = 0
        while True:
            if not self.is_supported_login_url(
                self._safe_page_url(page), expected_host=expected_host
            ):
                return None, True
            classification = await self._classify(
                page,
                expected_host=expected_host,
                wait_for_managed_passkey=False,
            )
            if classification is not None:
                if elapsed_ms:
                    _logger.info(
                        "Amazon assisted login transition settled: "
                        "from=%s to=%s waited_ms=%s",
                        action,
                        classification,
                        elapsed_ms,
                    )
                return classification, False
            if elapsed_ms >= self._transition_settle_timeout_ms:
                return None, False
            wait = getattr(page, "wait_for_timeout", None)
            if not callable(wait):
                # Fixture DOMs have no clock; do not spin on them.
                return None, False
            pause_ms = min(500, self._transition_settle_timeout_ms - elapsed_ms)
            if pause_ms <= 0:
                return None, False
            try:
                await wait(pause_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                return None, False
            elapsed_ms += pause_ms

    async def _dispatch_action_once(
        self,
        page: Any,
        action: _LoginAction,
        *,
        expected_host: str | None,
        allow_payment_details_handoff: bool = False,
    ) -> tuple[_DispatchOutcome, BaseException | None]:
        """Perform one fully revalidated click attempt without settling it."""

        if action == "otp_method_choice":
            try:
                clicked = await self._choose_authenticator_otp_method(page)
            except HumanAuthRequired:
                raise
            except _DispatchLimitReached:
                raise
            except Exception as exc:
                _logger.warning(
                    "OTP method choice was uncertain; bounded retry may follow"
                )
                return "uncertain", exc
            return ("dispatched" if clicked else "not_dispatched"), None

        if action == "managed_passkey":
            try:
                clicked = await self._with_managed_passkey_button(
                    page,
                    click=True,
                    expected_host=expected_host,
                )
            except _DispatchLimitReached:
                raise
            except Exception as exc:
                _logger.warning(
                    "Managed Passkey click attempt was uncertain; bounded retry may follow"
                )
                return "uncertain", exc
            if not clicked:
                return "not_dispatched", None
            return "dispatched", None

        try:
            control = await self._unique_action_control(
                page,
                action,
                expected_host=expected_host,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            await self._revalidate_standard_action_route(
                page,
                action,
                expected_host=expected_host,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
        except _AlreadyOnBusinessPage:
            # Amazon committed the login during the pre-click pause.  Report a
            # non-dispatch so the caller's existing post-click reclassification
            # adopts the business page, exactly as it does when the same thing
            # happens one moment later.  No click is attempted.
            _logger.info(
                "Amazon assisted login reached the destination before clicking: "
                "action=%s",
                action,
            )
            return "not_dispatched", None
        await self._reserve_dispatch(page, action)
        try:
            await control.click(no_wait_after=True)
        except Exception as exc:
            _logger.warning(
                "Amazon assisted login click attempt was uncertain: action=%s",
                action,
            )
            return "uncertain", exc
        _logger.info("Amazon assisted login action dispatched: %s", action)
        return "dispatched", None

    async def _revalidate_standard_action_route(
        self,
        page: Any,
        action: Literal["continue", "password_signin", "otp_signin"],
        *,
        expected_host: str | None,
        allow_payment_details_handoff: bool = False,
    ) -> None:
        """Recheck host, stage and renderer state immediately before click."""

        current_url = str(getattr(page, "url", "") or "")
        expected_path = "/ap/mfa" if action == "otp_signin" else "/ap/signin"
        if (
            not self.is_supported_login_url(
                current_url, expected_host=expected_host
            )
            or urlparse(current_url).path.rstrip("/").lower() != expected_path
        ):
            self._raise_if_already_on_business(
                page,
                current_url,
                expected_host=expected_host,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            raise HumanAuthRequired(
                "Amazon 登录页面在点击前已切换，已停止本次自动点击",
                kind="challenge" if action == "otp_signin" else "sign_in",
            )
        if await self._visible_unsupported_auth_content(page):
            raise HumanAuthRequired(
                "Amazon 登录页面在点击前出现其他验证、账户选择或明确错误",
                kind="challenge" if action == "otp_signin" else "sign_in",
            )

        if action == "otp_signin":
            if not await self._otp_retry_challenge_unchanged(page):
                raise HumanAuthRequired(
                    "Amazon OTP 在点击前发生变化、过期或更换输入框",
                    kind="challenge",
                )
            return

        if action == "password_signin":
            identifiers = await self._visible_elements(
                page.locator(self._identifier_selector)
            )
            passwords = await self._visible_elements(
                page.locator(self._password_selector)
            )
            otps = await self._visible_elements(page.locator(self._otp_selector))
            if (
                identifiers
                or len(passwords) != 1
                or otps
                or not await self._password_is_ready(passwords[0])
                or await self._managed_passkey_available(
                    page, expected_host=expected_host
                )
            ):
                raise HumanAuthRequired(
                    "Amazon 密码登录页在点击前已变化，已停止本次自动点击",
                    kind="sign_in",
                )
            # Resolve the exact control again after all field and overlay
            # checks.  This catches a duplicate/replaced/disabled submit button
            # before the previously selected locator is dispatched.
            await self._unique_action_control(
                page,
                "password_signin",
                expected_host=expected_host,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            return

        identifiers = await self._visible_elements(
            page.locator(self._identifier_selector)
        )
        passwords = await self._visible_elements(
            page.locator(self._password_selector)
        )
        otps = await self._visible_elements(page.locator(self._otp_selector))
        if (
            len(identifiers) != 1
            or passwords
            or otps
            or not await self._identifier_is_ready(identifiers[0])
            or await self._managed_passkey_available(
                page, expected_host=expected_host
            )
        ):
            raise HumanAuthRequired(
                "Amazon 邮箱登录页在点击前已变化，已停止本次自动点击",
                kind="sign_in",
            )

    async def advance_or_raise(
        self,
        page: Any,
        *,
        expected_host: str | None = None,
        allow_payment_details_handoff: bool = False,
    ) -> Any | None:
        """Return the exact Page that advanced to a supported business URL.

        Unsupported, incomplete or ambiguous login pages raise immediately.
        A successful same-tab transition returns the original Page; a unique,
        same-context new-tab transition returns that new Page.  The caller must
        continue all host, seller-identity and business-page checks on the
        returned object in the same invocation.
        """

        result = await self.advance(
            page,
            expected_host=expected_host,
            allow_payment_details_handoff=allow_payment_details_handoff,
        )
        return result.page if result.status == "advanced" else None

    @classmethod
    def resolve_business_page(
        cls,
        source_page: Any,
        *,
        expected_host: str | None = None,
        baseline: _BusinessBaseline | None = None,
        allow_payment_details_handoff: bool = False,
    ) -> Any | None:
        """Resolve one exact authenticated Seller Central tab in this context.

        Amazon can finish SSO/OTP in a newly created tab and deliberately leave
        the source tab parked on ``/ap/signin``.  Only pages from the source
        Page's existing BrowserContext participate.  A candidate must use
        HTTPS, match the exact marketplace host, and be a known Seller Central
        business route.  Multiple candidates are deliberately treated as
        ambiguous rather than choosing by tab order or focus.
        """

        # A failed baseline enumeration is materially different from a valid
        # empty context.  Without a trustworthy before-click snapshot there is
        # no evidence that another business tab belongs to this login attempt.
        if baseline is None:
            return None

        source_url = cls._safe_page_url(source_page)
        parsed_source = urlparse(source_url)
        host = (expected_host or parsed_source.hostname or "").lower().rstrip(".")
        if host not in _SELLER_CENTRAL_HOSTS:
            return None

        try:
            context = getattr(source_page, "context", None)
            if context is None:
                return None
            pages = getattr(context, "pages", None)
            if pages is None:
                return None
            context_pages = list(pages)
        except Exception as exc:
            raise HumanAuthRequired(
                "Amazon 登录标签页列表暂时不可读取，已停止自动点击",
                kind="sign_in",
            ) from exc

        candidates: list[Any] = []
        pending_pages: list[Any] = []
        for candidate in context_pages:
            if candidate is source_page:
                continue
            is_closed = getattr(candidate, "is_closed", None)
            if callable(is_closed):
                try:
                    if bool(is_closed()):
                        continue
                except Exception:
                    continue
            candidate_url = cls._safe_page_url(candidate)
            baseline_url: str | None = None
            was_present = False
            for baseline_page, saved_url in baseline:
                if baseline_page is candidate:
                    was_present = True
                    baseline_url = saved_url
                    break

            # A tab that was already a valid business destination before this
            # login attempt is deliberately ignored even if its query string or
            # title changes while another tab is authenticating.
            if was_present and cls.is_supported_business_url(
                baseline_url or "",
                expected_host=host,
                allow_payment_details_handoff=allow_payment_details_handoff,
            ):
                continue

            changed = not was_present or candidate_url != (baseline_url or "")
            if not changed:
                continue
            is_business = cls.is_supported_business_url(
                candidate_url,
                expected_host=host,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            # During one automatic login invocation, adopt only a newly opened
            # Page or an existing Page that changed from a non-business route.
            # A Ziniao startup tab that was already on a dashboard before this
            # login attempt is not proof that the current attempt succeeded.
            # Keep the actual Page wrappers in the short-lived baseline and
            # compare by identity.  A numeric ``id`` can be reused after a tab
            # closes while this bounded login attempt is still polling, which
            # could otherwise hide a genuinely new authenticated tab.
            if is_business:
                candidates.append(candidate)
                continue

            # A just-opened tab commonly remains about:blank for a short time.
            # Treat it as an in-flight handoff, never as permission to click the
            # old login button again.  Any other changed/new destination is an
            # explicit conflict (including a wrong marketplace or unknown
            # Seller Central path) and is handed to the auth lease immediately.
            if cls._is_pending_page_url(candidate_url):
                pending_pages.append(candidate)
                continue
            raise HumanAuthRequired(
                "Amazon 登录后新标签页的站点或页面不属于当前白名单，已停止自动接管",
                kind="sign_in",
            )

        if (
            len(candidates) > 1
            or len(pending_pages) > 1
            or (candidates and pending_pages)
        ):
            raise HumanAuthRequired(
                "同一紫鸟环境出现多个可用的 Seller Central 业务标签页，已停止自动接管",
                kind="sign_in",
            )
        return candidates[0] if candidates else None

    @classmethod
    def _has_pending_handoff_page(
        cls,
        source_page: Any,
        *,
        expected_host: str | None,
        baseline: _BusinessBaseline | None,
        allow_payment_details_handoff: bool = False,
    ) -> bool:
        if baseline is None:
            return False
        source_url = cls._safe_page_url(source_page)
        host = (
            expected_host or urlparse(source_url).hostname or ""
        ).lower().rstrip(".")
        if host not in _SELLER_CENTRAL_HOSTS:
            return False
        try:
            context = getattr(source_page, "context", None)
            if context is None:
                return False
            raw_pages = getattr(context, "pages", None)
            if raw_pages is None:
                return False
            pages = list(raw_pages)
        except Exception as exc:
            raise HumanAuthRequired(
                "Amazon 登录标签页列表暂时不可读取，已停止自动点击",
                kind="sign_in",
            ) from exc
        pending = 0
        for candidate in pages:
            if candidate is source_page or cls._page_is_closed(candidate):
                continue
            saved_url: str | None = None
            was_present = False
            for baseline_page, baseline_url in baseline:
                if baseline_page is candidate:
                    was_present = True
                    saved_url = baseline_url
                    break
            if was_present and cls.is_supported_business_url(
                saved_url or "",
                expected_host=host,
                allow_payment_details_handoff=allow_payment_details_handoff,
            ):
                continue
            current_url = cls._safe_page_url(candidate)
            if was_present and current_url == (saved_url or ""):
                continue
            if cls._is_pending_page_url(current_url):
                pending += 1
        if pending > 1:
            raise HumanAuthRequired(
                "同一紫鸟环境出现多个尚未完成跳转的新标签页，已停止自动接管",
                kind="sign_in",
            )
        return pending == 1

    @staticmethod
    def _business_host(source_page: Any, expected_host: str | None) -> str:
        source_url = AmazonLoginAdvancer._safe_page_url(source_page)
        parsed_source = urlparse(source_url)
        return (expected_host or parsed_source.hostname or "").lower().rstrip(".")

    @staticmethod
    def _safe_page_url(page: Any) -> str:
        try:
            return str(getattr(page, "url", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _is_pending_page_url(url: str) -> bool:
        normalized = str(url or "").strip().casefold().rstrip("/")
        return normalized in {"", "about:blank", "chrome://newtab"}

    @staticmethod
    def _page_is_closed(page: Any) -> bool:
        is_closed = getattr(page, "is_closed", None)
        if not callable(is_closed):
            return False
        try:
            return bool(is_closed())
        except Exception:
            return True

    @classmethod
    def _context_business_snapshot(
        cls,
        source_page: Any,
        *,
        expected_host: str | None,
    ) -> _BusinessBaseline | None:
        source_url = cls._safe_page_url(source_page)
        parsed_source = urlparse(source_url)
        host = (expected_host or parsed_source.hostname or "").lower().rstrip(".")
        try:
            context = getattr(source_page, "context", None)
        except Exception:
            return None
        # Lightweight parser fixtures have no BrowserContext.  They can still
        # exercise safe same-tab progression, while a real context whose pages
        # property raises is treated as a failed security snapshot below.
        if context is None:
            return ()
        try:
            pages = getattr(context, "pages", None)
        except Exception:
            return None
        if pages is None:
            return ()
        if host not in _SELLER_CENTRAL_HOSTS:
            return ()
        try:
            context_pages = list(pages)
        except Exception:
            return None
        return tuple(
            (
                candidate,
                cls._safe_page_url(candidate),
            )
            for candidate in context_pages
        )

    @classmethod
    def is_supported_business_url(
        cls,
        url: str,
        *,
        expected_host: str,
        allow_payment_details_handoff: bool = False,
    ) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        normalized_expected = (expected_host or "").lower().rstrip(".")
        raw_path = parsed.path.lower() or "/"
        path = raw_path.rstrip("/") or "/"
        payment_details_allowed = (
            allow_payment_details_handoff
            and raw_path == _PAYMENT_DETAILS_EXACT_PATH
            and not parsed.params
        )
        return (
            parsed.scheme.lower() == "https"
            and normalized_expected in _SELLER_CENTRAL_HOSTS
            and host == normalized_expected
            and (path in _BUSINESS_EXACT_PATHS or payment_details_allowed)
        )

    async def _wait_for_business_page(
        self,
        source_page: Any,
        *,
        expected_host: str | None,
        baseline: _BusinessBaseline | None,
        timeout_ms: int,
        allow_payment_details_handoff: bool = False,
    ) -> Any | None:
        """Briefly poll for Amazon's delayed login destination.

        Seller Central does not use one stable handoff shape.  It may open a
        new tab and leave ``source_page`` on ``/ap/mfa``, or it may keep the
        same Page on ``/ap/mfa`` for about a second before navigating that Page
        to the Payments dashboard.  ``resolve_business_page`` intentionally
        inspects only *other* context pages, so the same-tab case must be
        checked here on every poll.  If both shapes appear together, do not
        guess which destination belongs to this login attempt.
        """

        elapsed_ms = 0
        while True:
            other_candidate = self.resolve_business_page(
                source_page,
                expected_host=expected_host,
                baseline=baseline,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            source_is_business = self.is_supported_business_url(
                self._safe_page_url(source_page),
                expected_host=self._business_host(source_page, expected_host),
                allow_payment_details_handoff=allow_payment_details_handoff,
            ) and not self._page_is_closed(source_page)
            pending_other = self._has_pending_handoff_page(
                source_page,
                expected_host=expected_host,
                baseline=baseline,
                allow_payment_details_handoff=allow_payment_details_handoff,
            )
            if source_is_business and (
                other_candidate is not None or pending_other
            ):
                raise HumanAuthRequired(
                    "Amazon 登录后同时出现同标签页和新标签页业务入口，已停止自动接管",
                    kind="sign_in",
                )
            if source_is_business:
                return source_page
            if other_candidate is not None:
                return other_candidate
            if elapsed_ms >= timeout_ms:
                if pending_other:
                    raise HumanAuthRequired(
                        "Amazon 新标签页在限定时间内仍未完成跳转，已停止重复点击",
                        kind="sign_in",
                    )
                return None
            pause_ms = min(250, timeout_ms - elapsed_ms)
            if pause_ms <= 0:
                return None
            await self._managed_passkey_poll_pause(source_page, pause_ms)
            elapsed_ms += pause_ms

    @classmethod
    def is_supported_login_url(
        cls, url: str, *, expected_host: str | None = None
    ) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        normalized_expected = (expected_host or "").lower().rstrip(".")
        return (
            parsed.scheme.lower() == "https"
            and host in _SELLER_CENTRAL_HOSTS
            and (not normalized_expected or host == normalized_expected)
            and parsed.path.rstrip("/").lower() in _LOGIN_PATHS
        )

    async def _classify(
        self,
        page: Any,
        *,
        expected_host: str | None = None,
        wait_for_managed_passkey: bool = True,
    ) -> _LoginAction | None:
        current_url = str(getattr(page, "url", "") or "")
        if not self.is_supported_login_url(current_url, expected_host=expected_host):
            return None
        path = urlparse(current_url).path.rstrip("/").lower()
        auth_kind = "challenge" if path == "/ap/mfa" else "sign_in"

        # CAPTCHA, ordinary Passkey challenges, account choices and errors
        # always win over the managed helper.  The managed helper itself is in
        # a closed shadow root, so none of these page locators can match it.
        if await self._visible_unsupported_auth_content(page):
            raise HumanAuthRequired(
                "Amazon 登录页包含密码以外的验证、账户选择或异常内容",
                kind=auth_kind,
            )

        # Ziniao renders its managed-account Passkey chooser in a *closed*
        # shadow root.  Playwright locators intentionally cannot see it, so a
        # narrowly validated CDP tree probe runs before the generic password /
        # Passkey rejection below.  No account label or credential is returned.
        if await self._managed_passkey_available(
            page, expected_host=expected_host
        ):
            return "managed_passkey"

        # The managed helper can be injected shortly after Amazon has already
        # painted its password form.  Initial classification gives it a short
        # bounded wait; post-click retry classification is deliberately quick.
        visible_passwords = await self._visible_elements(
            page.locator(self._password_selector)
        )
        if (
            wait_for_managed_passkey
            and path == "/ap/signin"
            and len(visible_passwords) == 1
            and self._managed_passkey_wait_timeout_ms > 0
            and await self._wait_for_managed_passkey(
                page, expected_host=expected_host
            )
        ):
            return "managed_passkey"

        current_url = str(getattr(page, "url", "") or "")
        if not self.is_supported_login_url(
            current_url, expected_host=expected_host
        ):
            return None
        path = urlparse(current_url).path.rstrip("/").lower()
        auth_kind = "challenge" if path == "/ap/mfa" else "sign_in"

        body = await self._body_text(page)

        # The method chooser has no field to fill and nothing prefilled, so it
        # is classified purely by route.  Its dispatch does the safety work.
        if path == _OTP_METHOD_PATH:
            _logger.info(
                "Amazon assisted login reached the OTP method chooser: "
                "action=otp_method_choice"
            )
            return "otp_method_choice"

        # If the managed helper disappeared after a failed attempt, Ziniao can
        # leave Amazon's one prefilled password form behind.  Clicking only its
        # exact standard submit control re-triggers the assisted verification;
        # Python receives merely a readiness boolean and never the password.
        visible_passwords = await self._visible_elements(
            page.locator(self._password_selector)
        )
        if path == "/ap/signin" and visible_passwords:
            if self._password_fallback_blocking_text.search(body[:100_000]):
                raise HumanAuthRequired(
                    "Amazon 密码登录页包含验证码、账户选择或明确错误",
                    kind="sign_in",
                )
            identifiers = await self._visible_elements(
                page.locator(self._identifier_selector)
            )
            otps = await self._visible_elements(page.locator(self._otp_selector))
            if len(visible_passwords) != 1 or identifiers or otps:
                raise HumanAuthRequired(
                    "Amazon 密码登录页输入框数量或类型存在歧义",
                    kind="sign_in",
                )
            if not await self._wait_for_prefill(
                page,
                visible_passwords[0],
                selector=self._password_selector,
                kind="password",
            ):
                raise HumanAuthRequired(
                    "紫鸟尚未填好 Amazon 登录密码",
                    kind="sign_in",
                )
            _logger.info(
                "Amazon assisted login password fallback ready: action=password_signin"
            )
            return "password_signin"

        if self._unsupported_text.search(body[:100_000]):
            raise HumanAuthRequired(
                "Amazon 登录页包含不支持的人工处理内容",
                kind=auth_kind,
            )

        # Anything outside the explicitly modelled screens returns to a
        # person.  These checks happen before inspecting field readiness.
        for selector in (
            self._captcha_selector,
            self._passkey_selector,
            self._account_choice_selector,
            self._error_selector,
        ):
            if await self._visible_elements(page.locator(selector)):
                raise HumanAuthRequired(
                    "Amazon 登录页包含密码、验证、账户选择或异常内容",
                    kind="sign_in",
                )
        # Amazon commonly retains hidden inputs and alert containers from a
        # previous auth stage.  Only controls actually presented to the user
        # participate in classification; multiple visible controls still
        # fail closed as ambiguous.
        identifiers = await self._visible_elements(
            page.locator(self._identifier_selector)
        )
        otps = await self._visible_elements(page.locator(self._otp_selector))
        identifier_count = len(identifiers)
        otp_count = len(otps)
        path = urlparse(str(page.url)).path.rstrip("/").lower()
        if path == "/ap/signin" and identifier_count == 1 and otp_count == 0:
            if not await self._wait_for_prefill(
                page,
                identifiers[0],
                selector=self._identifier_selector,
                kind="identifier",
            ):
                raise HumanAuthRequired(
                    "紫鸟尚未填好邮箱或手机号",
                    kind="sign_in",
                )
            return "continue"
        if path == "/ap/mfa" and otp_count == 1 and identifier_count == 0:
            if not await self._wait_for_stable_otp(
                page, expected_host=expected_host
            ):
                raise HumanAuthRequired(
                    "紫鸟尚未填好并稳定保持一次性验证码",
                    kind="challenge",
                )
            return "otp_signin"
        if identifier_count or otp_count:
            raise HumanAuthRequired(
                "Amazon 登录输入框数量或类型存在歧义",
                kind="sign_in",
            )
        return None

    async def _identifier_is_ready(self, field: Any) -> bool:
        if not await self._is_visible(field):
            return False
        return await self._boolean_evaluate(
            field,
            """element => {
                const value = String(element.value || '').trim();
                const email = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(value);
                const phone = /^\\+?[0-9][0-9 ()-]{5,24}$/.test(value);
                return !element.disabled && !element.readOnly && (email || phone);
            }""",
        )

    async def _otp_is_ready(self, field: Any) -> bool:
        if not await self._is_visible(field):
            return False
        return await self._boolean_evaluate(
            field,
            """element => {
                const value = String(element.value || '').trim();
                return !element.disabled && !element.readOnly
                    && /^[0-9]{6}$/.test(value);
            }""",
        )

    async def _password_is_ready(self, field: Any) -> bool:
        """Return only whether Ziniao has populated one usable password field."""

        if not await self._is_visible(field):
            return False
        return await self._boolean_evaluate(
            field,
            """element => {
                const value = String(element.value || '');
                return element.isConnected
                    && !element.disabled && !element.readOnly
                    && value.length > 0;
            }""",
        )

    async def _wait_for_stable_otp(
        self, page: Any, *, expected_host: str | None
    ) -> bool:
        """Wait for one six-digit OTP to remain unchanged for 2.5 seconds.

        The exact OTP stays inside the renderer.  Python receives only a
        boolean from the page-scoped stability tracker, which also binds the
        observation to the same DOM input element.  A partial fill, a changed
        value, a re-rendered input, navigation, or timeout all fail closed.
        """

        timeout_ms = self._otp_prefill_wait_timeout_ms
        elapsed_ms = 0
        while True:
            current_url = str(getattr(page, "url", "") or "")
            if (
                not self.is_supported_login_url(
                    current_url, expected_host=expected_host
                )
                or urlparse(current_url).path.rstrip("/").lower() != "/ap/mfa"
            ):
                return False
            if await self._visible_unsupported_auth_content(page):
                raise HumanAuthRequired(
                    "Amazon OTP 等待期间出现验证码、账户选择或明确错误",
                    kind="challenge",
                )

            otp_fields = await self._visible_elements(
                page.locator(self._otp_selector)
            )
            if len(otp_fields) != 1:
                return False
            if await self._otp_stability_probe(otp_fields[0]):
                return True
            if elapsed_ms >= timeout_ms:
                return False

            pause_ms = min(
                self._otp_stability_poll_interval_ms,
                timeout_ms - elapsed_ms,
            )
            if pause_ms <= 0:
                return False
            try:
                await self._otp_stability_pause(page, pause_ms)
            except Exception:
                return False
            elapsed_ms += pause_ms

    async def _otp_stability_probe(self, field: Any) -> bool:
        """Return whether the renderer-bound OTP has qualified as stable."""

        if not await self._is_visible(field):
            return False
        return await self._boolean_evaluate(
            field,
            f"""element => {{
                const stateKey = '__ziniaoAutomationOtpStabilityV1';
                const value = String(element.value || '').trim();
                const ready = element.isConnected
                    && !element.disabled && !element.readOnly
                    && /^[0-9]{{6}}$/.test(value);
                if (!ready) {{
                    delete globalThis[stateKey];
                    return false;
                }}
                const now = performance.now();
                const previous = globalThis[stateKey];
                if (!previous || previous.element !== element
                        || previous.value !== value) {{
                    globalThis[stateKey] = {{element, value, since: now}};
                    return false;
                }}
                const age = now - previous.since;
                if (previous.expired || age >= {self._otp_max_age_ms}) {{
                    previous.expired = true;
                    return false;
                }}
                return age >= {self._otp_stable_duration_ms};
            }}""",
        )

    async def _otp_is_still_stable(self, field: Any) -> bool:
        """Recheck the qualified value immediately before resolving submit."""

        if not await self._is_visible(field):
            return False
        return await self._boolean_evaluate(
            field,
            f"""element => {{
                const stateKey = '__ziniaoAutomationOtpStabilityV1';
                const value = String(element.value || '').trim();
                const previous = globalThis[stateKey];
                return element.isConnected
                    && !element.disabled && !element.readOnly
                    && /^[0-9]{{6}}$/.test(value)
                    && previous && previous.element === element
                    && previous.value === value
                    && !previous.expired
                    && performance.now() - previous.since
                        >= {self._otp_stable_duration_ms}
                    && performance.now() - previous.since
                        < {self._otp_max_age_ms};
            }}""",
        )

    async def _capture_otp_retry_challenge(self, page: Any) -> bool:
        """Snapshot one qualified OTP inside the renderer for retry checks.

        The field reference, value and stability timestamp never leave the
        page.  Python receives only a readiness boolean; the renderer retains
        the snapshot so a changed value, replaced input, or new stability epoch
        can never be submitted by the automatic retry.
        """

        fields = await self._visible_elements(page.locator(self._otp_selector))
        if len(fields) != 1:
            return False
        return await self._boolean_evaluate(
            fields[0],
            f"""element => {{
                const stateKey = '__ziniaoAutomationOtpStabilityV1';
                const retryKey = '__ziniaoAutomationOtpRetryV1';
                const value = String(element.value || '').trim();
                const stable = globalThis[stateKey];
                const age = stable ? performance.now() - stable.since : Infinity;
                const ready = element.isConnected
                    && !element.disabled && !element.readOnly
                    && /^[0-9]{{6}}$/.test(value)
                    && stable && stable.element === element
                    && stable.value === value && !stable.expired
                    && age >= {self._otp_stable_duration_ms}
                    && age < {self._otp_max_age_ms};
                if (!ready) {{
                    delete globalThis[retryKey];
                    return false;
                }}
                globalThis[retryKey] = {{
                    element,
                    value,
                    since: stable.since,
                }};
                return true;
            }}""",
        )

    async def _otp_retry_challenge_unchanged(self, page: Any) -> bool:
        """Return only whether the retry still targets the captured OTP."""

        fields = await self._visible_elements(page.locator(self._otp_selector))
        if len(fields) != 1:
            return False
        return await self._boolean_evaluate(
            fields[0],
            f"""element => {{
                const stateKey = '__ziniaoAutomationOtpStabilityV1';
                const retryKey = '__ziniaoAutomationOtpRetryV1';
                const value = String(element.value || '').trim();
                const stable = globalThis[stateKey];
                const retry = globalThis[retryKey];
                const age = stable ? performance.now() - stable.since : Infinity;
                return element.isConnected
                    && !element.disabled && !element.readOnly
                    && /^[0-9]{{6}}$/.test(value)
                    && stable && retry
                    && stable.element === element
                    && retry.element === element
                    && stable.value === value
                    && retry.value === value
                    && retry.since === stable.since
                    && !stable.expired
                    && age >= {self._otp_stable_duration_ms}
                    && age < {self._otp_max_age_ms};
            }}""",
        )

    async def _otp_stability_pause(self, page: Any, milliseconds: int) -> None:
        wait = getattr(page, "wait_for_timeout", None)
        if callable(wait):
            await wait(milliseconds)
            return
        await asyncio.sleep(milliseconds / 1000)

    async def _wait_for_prefill(
        self,
        page: Any,
        field: Any,
        *,
        selector: str,
        kind: Literal["identifier", "password", "otp"],
    ) -> bool:
        """Wait briefly for Ziniao to populate the page-owned input.

        The browser evaluates only a readiness boolean.  The identifier or
        OTP itself never crosses the page boundary into Python.
        """

        readiness = {
            "identifier": self._identifier_is_ready,
            "password": self._password_is_ready,
            "otp": self._otp_is_ready,
        }[kind]
        timeout_ms = (
            self._otp_prefill_wait_timeout_ms
            if kind == "otp"
            else self._prefill_wait_timeout_ms
        )
        if await readiness(field):
            return True
        wait_for_function = getattr(page, "wait_for_function", None)
        if not callable(wait_for_function) or timeout_ms == 0:
            return False
        try:
            await wait_for_function(
                """argument => {
                    const fields = Array.from(document.querySelectorAll(argument.selector))
                        .filter(field => {
                            const style = window.getComputedStyle(field);
                            const rect = field.getBoundingClientRect();
                            return field.type !== 'hidden'
                                && style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && style.visibility !== 'collapse'
                                && (rect.width > 0 || rect.height > 0);
                        });
                    if (fields.length !== 1) return false;
                    const field = fields[0];
                    if (field.disabled || field.readOnly) return false;
                    const value = String(field.value || '').trim();
                    if (argument.kind === 'otp') return /^[0-9]{6}$/.test(value);
                    if (argument.kind === 'password') return value.length > 0;
                    const email = /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(value);
                    const phone = /^\\+?[0-9][0-9 ()-]{5,24}$/.test(value);
                    return email || phone;
                }""",
                {"selector": selector, "kind": kind},
                polling=250,
                timeout=timeout_ms,
            )
        except Exception:
            return False
        return await readiness(field)

    async def _visible_elements(self, locator: Any) -> list[Any]:
        """Return visible matches without reading any field or account value."""

        visible: list[Any] = []
        for index in range(await locator.count()):
            element = locator.nth(index)
            if await self._is_visible(element):
                visible.append(element)
        return visible

    def _raise_if_already_on_business(
        self,
        page: Any,
        current_url: str,
        *,
        expected_host: str | None,
        allow_payment_details_handoff: bool,
    ) -> None:
        """Signal a pre-click success when the route is an allowed destination.

        This is the exact predicate the post-click path already uses to decide
        that a login finished (see ``advance`` and
        ``_advance_action_with_retries``).  It never widens what counts as a
        destination and never authorises a click — it only stops the pre-click
        validators from reporting a completed login as a failure.
        """

        if self.is_supported_business_url(
            current_url,
            expected_host=self._business_host(page, expected_host),
            allow_payment_details_handoff=allow_payment_details_handoff,
        ):
            raise _AlreadyOnBusinessPage()

    async def _unique_action_control(
        self,
        page: Any,
        action: Literal["continue", "password_signin", "otp_signin"],
        *,
        expected_host: str | None = None,
        allow_payment_details_handoff: bool = False,
    ) -> Any:
        pattern = self._continue_label if action == "continue" else self._signin_label
        selector = {
            "continue": self._continue_control_selector,
            "password_signin": self._password_signin_control_selector,
            "otp_signin": self._signin_control_selector,
        }[action]
        button_wait_elapsed_ms = 0
        while True:
            if action == "otp_signin":
                current_url = str(getattr(page, "url", "") or "")
                if (
                    not self.is_supported_login_url(
                        current_url, expected_host=expected_host
                    )
                    or urlparse(current_url).path.rstrip("/").lower() != "/ap/mfa"
                ):
                    self._raise_if_already_on_business(
                        page,
                        current_url,
                        expected_host=expected_host,
                        allow_payment_details_handoff=allow_payment_details_handoff,
                    )
                    raise HumanAuthRequired(
                        "Amazon OTP 页面已变化，请人工检查当前页面",
                        kind="challenge",
                    )
                if await self._visible_unsupported_auth_content(page):
                    raise HumanAuthRequired(
                        "Amazon OTP 提交前出现验证码、账户选择或明确错误",
                        kind="challenge",
                    )
                otp_fields = await self._visible_elements(
                    page.locator(self._otp_selector)
                )
                if len(otp_fields) != 1 or not await self._otp_is_still_stable(
                    otp_fields[0]
                ):
                    raise HumanAuthRequired(
                        "Amazon OTP 在提交前发生变化、过期或尚未稳定",
                        kind="challenge",
                    )

            candidates = page.locator(selector)
            matched: list[Any] = []
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                label = await self._control_label(candidate)
                if not pattern.fullmatch(label):
                    continue
                if not await self._is_enabled(candidate):
                    continue
                matched.append(candidate)
            if len(matched) == 1:
                return matched[0]
            if len(matched) > 1:
                break
            if action == "otp_signin":
                structural = await self._unique_structural_otp_signin_control(
                    page, expected_host=expected_host
                )
                if structural is not None:
                    return structural
                # Ziniao/Amazon can enable the submit control just after the
                # final digit appears.  Poll only inside the same OTP's strict
                # validity window; selectors and structural checks stay exact.
                if button_wait_elapsed_ms < self._otp_max_age_ms:
                    await self._otp_stability_pause(
                        page, self._otp_stability_poll_interval_ms
                    )
                    button_wait_elapsed_ms += self._otp_stability_poll_interval_ms
                    continue
            break
        # The control can be "missing" simply because Amazon already replaced
        # the login document with the destination during the pre-click pause.
        self._raise_if_already_on_business(
            page,
            str(getattr(page, "url", "") or ""),
            expected_host=expected_host,
            allow_payment_details_handoff=allow_payment_details_handoff,
        )
        raise HumanAuthRequired(
            "Amazon 登录操作按钮缺失、禁用或不唯一",
            kind="challenge" if action == "otp_signin" else "sign_in",
        )

    async def _unique_structural_otp_signin_control(
        self, page: Any, *, expected_host: str | None
    ) -> Any | None:
        """Recognise Amazon's localized MFA submit input without its label.

        Some Seller Central localizations expose a garbled display value while
        retaining one stable MFA-only input structure.  This fallback is
        deliberately unavailable on ``/ap/signin`` and accepts exactly one
        enabled, visible input plus its nearest primary ``.a-button`` wrapper.
        It never reads the OTP field or broadens ordinary password submission.
        """

        current_url = str(getattr(page, "url", "") or "")
        if not self.is_supported_login_url(
            current_url, expected_host=expected_host
        ) or urlparse(current_url).path.rstrip("/").lower() != "/ap/mfa":
            return None

        # Recheck the already-qualified OTP immediately before returning a
        # submit control so a cleared/re-rendered/changed code can never be
        # submitted from stale state.  Only a boolean crosses the page boundary.
        otp_fields = await self._visible_elements(page.locator(self._otp_selector))
        if len(otp_fields) != 1 or not await self._otp_is_still_stable(
            otp_fields[0]
        ):
            return None

        candidates = page.locator("input#auth-signin-button")
        if await candidates.count() != 1:
            return None
        candidate = candidates.nth(0)
        if not await self._is_enabled(candidate):
            return None

        attributes: dict[str, str] = {}
        for name in ("id", "name", "type", "class", "aria-labelledby"):
            try:
                value = await candidate.get_attribute(name)
            except Exception:
                return None
            attributes[name] = str(value or "").strip()
        if (
            attributes["id"] != "auth-signin-button"
            or attributes["name"] != "mfaSubmit"
            or attributes["type"].casefold() != "submit"
            or set(attributes["class"].split()) != {"a-button-input"}
            or not attributes["aria-labelledby"]
        ):
            return None

        wrapper_is_primary = await self._boolean_evaluate(
            candidate,
            """element => {
                const wrapper = element.closest('.a-button');
                if (!wrapper || !wrapper.classList.contains('a-button-primary')) {
                    return false;
                }
                const style = window.getComputedStyle(wrapper);
                const rect = wrapper.getBoundingClientRect();
                return wrapper.isConnected
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && style.visibility !== 'collapse'
                    && rect.width > 0 && rect.height > 0;
            }""",
        )
        if not wrapper_is_primary:
            return None
        return candidate

    async def _managed_passkey_available(
        self, page: Any, *, expected_host: str | None
    ) -> bool:
        """Recognise one exact visible Ziniao managed-Passkey dialog.

        The dialog is injected into a closed shadow root, so the check uses a
        short page-scoped CDP session.  Only structural facts and a boolean
        leave the renderer; account text, password values, cookies and tokens
        are never selected or returned.
        """

        try:
            return bool(
                await self._with_managed_passkey_button(
                    page, click=False, expected_host=expected_host
                )
            )
        except Exception:
            # Falling back to the normal unsupported-Passkey branch is the
            # fail-closed result when CDP structure or visibility is unclear.
            return False

    async def _wait_for_managed_passkey(
        self, page: Any, *, expected_host: str | None
    ) -> bool:
        """Poll briefly for Ziniao's late-injected exact managed dialog.

        This path is called only after one visible password field has been
        observed on ``/ap/signin``.  It never reads that field, never finds the
        ordinary Amazon submit control, and never clicks anything.  Every CDP
        probe remains the same fail-closed structural/visibility check used by
        the immediate managed-Passkey path.
        """

        elapsed_ms = 0
        timeout_ms = self._managed_passkey_wait_timeout_ms
        while elapsed_ms < timeout_ms:
            current_url = str(getattr(page, "url", "") or "")
            if (
                not self.is_supported_login_url(
                    current_url, expected_host=expected_host
                )
                or urlparse(current_url).path.rstrip("/").lower() != "/ap/signin"
            ):
                return False
            if await self._visible_unsupported_auth_content(page):
                raise HumanAuthRequired(
                    "Amazon 登录等待期间出现其他验证、账户选择或异常内容",
                    kind="sign_in",
                )

            pause_ms = min(
                self._managed_passkey_poll_interval_ms,
                timeout_ms - elapsed_ms,
            )
            await self._managed_passkey_poll_pause(page, pause_ms)
            elapsed_ms += pause_ms

            # A Ziniao helper can finish the password/Passkey transition on
            # its own.  Stop the Passkey poll immediately when MFA appears so
            # its short-lived OTP is classified instead of wasting 30 seconds.
            current_url = str(getattr(page, "url", "") or "")
            if (
                not self.is_supported_login_url(
                    current_url, expected_host=expected_host
                )
                or urlparse(current_url).path.rstrip("/").lower() != "/ap/signin"
            ):
                return False

            # Check blockers before the managed helper so simultaneous CAPTCHA
            # or error UI can never be hidden by a matching injected dialog.
            if await self._visible_unsupported_auth_content(page):
                raise HumanAuthRequired(
                    "Amazon 登录等待期间出现其他验证、账户选择或异常内容",
                    kind="sign_in",
                )
            if await self._managed_passkey_available(
                page, expected_host=expected_host
            ):
                return True
        return False

    async def _visible_unsupported_auth_content(self, page: Any) -> bool:
        """Return only a boolean for visible non-password auth blockers."""

        for selector in (
            self._captcha_selector,
            self._passkey_selector,
            self._account_choice_selector,
            self._error_selector,
        ):
            if await self._visible_elements(page.locator(selector)):
                return True
        return False

    async def _managed_passkey_poll_pause(self, page: Any, milliseconds: int) -> None:
        wait = getattr(page, "wait_for_timeout", None)
        if callable(wait):
            try:
                await wait(milliseconds)
                return
            except Exception:
                # The source login tab may close while Amazon is creating the
                # authenticated destination.  Polling the BrowserContext must
                # continue without leaking Playwright's TargetClosedError.
                pass
        await asyncio.sleep(milliseconds / 1000)

    async def _choose_authenticator_otp_method(self, page: Any) -> bool:
        """Select "authenticator app" on the chooser, then submit.

        ⚠️ The first option is preselected by Amazon and sends a WhatsApp
        message to the seller's real phone; the second rings them.  So the
        order here is load-bearing and every step is verified against the live
        DOM before the next one:

        1. read the radios with their label text
        2. require EXACTLY ONE to look like the authenticator option
        3. click it, then re-read and confirm it is the only checked one
        4. refuse outright if the checked option looks like a delivery channel
           — a negative assertion on the harmful outcome, so a wrong positive
           match in step 2 still cannot ring the seller
        5. only then click the single submit control

        Anything ambiguous raises ``HumanAuthRequired``; the page is left
        untouched for a person.
        """

        options = await page.evaluate(_OTP_METHOD_OPTIONS)
        if not isinstance(options, list) or not options:
            raise HumanAuthRequired(
                "Amazon 两步验证方式页面没有读到任何选项",
                kind="challenge",
            )

        def _text(option: Any) -> str:
            return " ".join(
                str(option.get(key) or "")
                for key in ("label", "value", "id", "name")
            )

        wanted = [
            option
            for option in options
            if self._otp_method_authenticator.search(_text(option))
        ]
        if len(wanted) != 1:
            raise HumanAuthRequired(
                "Amazon 两步验证方式页面无法唯一确定「认证器应用」选项，"
                f"匹配到 {len(wanted)} 项，已停止自动点击",
                kind="challenge",
            )
        target_index = int(wanted[0].get("index", -1))
        if target_index < 0:
            raise HumanAuthRequired(
                "Amazon 两步验证方式选项缺少可定位的序号", kind="challenge"
            )

        if not await page.evaluate(_SELECT_OTP_METHOD, target_index):
            raise HumanAuthRequired(
                "Amazon 两步验证方式选项无法选中", kind="challenge"
            )
        await self._paced_pause(page)

        after = await page.evaluate(_OTP_METHOD_OPTIONS)
        checked = [
            option for option in after if option.get("checked") is True
        ]
        if len(checked) != 1 or int(checked[0].get("index", -1)) != target_index:
            raise HumanAuthRequired(
                "Amazon 两步验证方式选项的选中状态与预期不符，已停止自动点击",
                kind="challenge",
            )
        # ⚠️ The check that actually protects the seller's phone.
        if self._otp_method_delivery.search(_text(checked[0])):
            raise HumanAuthRequired(
                "Amazon 两步验证当前选中的是短信/WhatsApp/电话方式，"
                "自动化不会替卖家发起，请人工处理",
                kind="challenge",
            )

        candidates = await self._visible_elements(
            page.locator(self._otp_method_submit_selector)
        )
        submits: list[Any] = []
        for candidate in candidates:
            try:
                text = " ".join(str(value or "") for value in (
                    await candidate.inner_text(),
                    await candidate.get_attribute("value"),
                    await candidate.get_attribute("aria-label"),
                    await candidate.get_attribute("id"),
                ))
            except Exception:
                text = ""
            if self._otp_method_send_label.search(text):
                submits.append(candidate)
        if len(submits) != 1:
            raise HumanAuthRequired(
                "Amazon ??????????????????????"
                f"???? {len(candidates)} ?????? {len(labelled)} ?????????",
                kind="challenge",
            )
        await submits[0].click()
        _logger.info("Amazon assisted login selected authenticator and sent OTP request")
        return True

    async def _dispatch_key(
        self,
        page: Any,
        action: _LoginAction,
        *,
        discriminator: str = "",
    ) -> tuple[str, str, str]:
        """Bind a one-click guard to one Page, document and exact action.

        ``performance.timeOrigin`` changes when the main document is replaced,
        including a same-URL reload, while remaining stable for DOM updates in
        that document.  Only that non-sensitive number crosses from the page.
        Lightweight fixtures without ``Page.evaluate`` fall back to the exact
        in-memory URL; Page-object isolation still prevents cross-store reuse.
        """

        generation = ""
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            try:
                value = await evaluate(
                    """() => {
                        const value = Number(globalThis.performance?.timeOrigin);
                        return Number.isFinite(value) && value > 0
                            ? String(value) : '';
                    }"""
                )
                if isinstance(value, str) and value.strip():
                    generation = f"time-origin:{value.strip()}"
            except Exception:
                generation = ""
        if not generation:
            generation = f"url:{str(getattr(page, 'url', '') or '')}"
        current_url = str(getattr(page, "url", "") or "")
        parsed = urlparse(current_url)
        route = (
            f"{(parsed.hostname or '').lower()}"
            f"{parsed.path.rstrip('/').lower()}"
        )
        # Marketplace host/path is an explicit part of the budget.  Therefore
        # attempts used on CA can never consume UK/AU attempts, even if one
        # Playwright Page wrapper is reused while switching sites.
        scoped_generation = f"{generation}|route:{route}"
        return scoped_generation, str(action), str(discriminator)

    def _page_dispatch_attempts(
        self, page: Any
    ) -> dict[tuple[str, str, str], int]:
        """Return lifecycle-bound click counts for a Playwright Page wrapper."""

        try:
            current = self._dispatch_attempts.get(page)
            if current is None:
                current = {}
                self._dispatch_attempts[page] = current
            return current
        except TypeError:
            # Third-party/fake Page wrappers can be non-weak-referenceable.
            # Keeping the dictionary on that object still gives correct lifetime
            # semantics and never falls back to a reusable numeric id.
            attribute = "_ziniao_assisted_login_dispatch_attempts"
            current = getattr(page, attribute, None)
            if not isinstance(current, dict):
                current = {}
                setattr(page, attribute, current)
            return current

    async def _reserve_dispatch(
        self,
        page: Any,
        action: _LoginAction,
        *,
        discriminator: str = "",
    ) -> tuple[tuple[str, str, str], int]:
        """Atomically consume one of the stage's bounded click attempts."""

        key = await self._dispatch_key(
            page,
            action,
            discriminator=discriminator,
        )
        attempts = self._page_dispatch_attempts(page)
        current = int(attempts.get(key, 0))
        if current >= self._max_action_attempts:
            raise _DispatchLimitReached(action)
        reserved = current + 1
        attempts[key] = reserved
        return key, reserved

    def _release_dispatch(
        self,
        page: Any,
        reservation: tuple[tuple[str, str, str], int],
    ) -> None:
        """Return a slot only when the renderer proved no click was emitted."""

        key, reserved = reservation
        attempts = self._page_dispatch_attempts(page)
        if int(attempts.get(key, 0)) != int(reserved):
            return
        if reserved <= 1:
            attempts.pop(key, None)
        else:
            attempts[key] = reserved - 1

    async def _with_managed_passkey_button(
        self,
        page: Any,
        *,
        click: bool,
        expected_host: str | None,
    ) -> bool:
        """Validate (and optionally click) the exact closed-shadow button."""

        current_url = str(getattr(page, "url", "") or "")
        if not self.is_supported_login_url(
            current_url, expected_host=expected_host
        ):
            return False
        parsed = urlparse(current_url)
        if parsed.path.rstrip("/").lower() != "/ap/signin":
            return False
        context = getattr(page, "context", None)
        new_session = getattr(context, "new_cdp_session", None)
        if not callable(new_session):
            return False
        session = await self._managed_passkey_cdp_call(
            new_session(page),
            operation="new_session",
        )
        try:
            await self._managed_passkey_cdp_call(
                session.send("DOM.enable"),
                operation="dom_enable",
            )
            document = await self._managed_passkey_cdp_call(
                session.send(
                    "DOM.getDocument", {"depth": -1, "pierce": True}
                ),
                operation="dom_get_document",
            )
            candidates = _managed_passkey_candidates(document.get("root") or {})
            if len(candidates) != 1:
                return False
            candidate = candidates[0]
            node_id = int(candidate.node_id)
            resolved = await self._managed_passkey_cdp_call(
                session.send("DOM.resolveNode", {"nodeId": node_id}),
                operation="dom_resolve_node",
            )
            object_id = str((resolved.get("object") or {}).get("objectId") or "")
            if not object_id:
                return False
            if candidate.backend_node_id is not None:
                described = await self._managed_passkey_cdp_call(
                    session.send(
                        "DOM.describeNode",
                        {"objectId": object_id, "depth": 0, "pierce": True},
                    ),
                    operation="dom_describe_node",
                )
                described_backend_id = (described.get("node") or {}).get(
                    "backendNodeId"
                )
                if described_backend_id != candidate.backend_node_id:
                    return False
            reservation: tuple[tuple[str, str, str], int] | None = None
            if click:
                # Reserve only after the exact current shadow button resolves.
                # An RPC exception has an uncertain dispatch result and keeps
                # the slot consumed; renderer ``false`` proves this.click() was
                # not reached and releases it for the bounded retry.
                # Two managed dialogs can legitimately appear in one Amazon
                # document (before and after the strict password fallback).
                # A stable backend node id gives each real dialog its own
                # three-attempt page budget.  Older helpers that omit the id
                # conservatively keep the shared document/action budget.
                discriminator = (
                    f"backend-node:{candidate.backend_node_id}"
                    if candidate.backend_node_id is not None
                    else ""
                )
                reservation = await self._reserve_dispatch(
                    page,
                    "managed_passkey",
                    discriminator=discriminator,
                )
            function = (
                """function() {
                    const label = String(this.innerText || this.textContent || '')
                        .replace(/\\s+/g, ' ').trim();
                    const style = window.getComputedStyle(this);
                    const rect = this.getBoundingClientRect();
                    const root = this.getRootNode();
                    const host = root && root.host;
                    const dialog = this.closest('div.dialog');
                    const container = this.closest('div#button-container');
                    const titles = dialog
                        ? Array.from(dialog.querySelectorAll('h2')).filter(item =>
                            String(item.innerText || item.textContent || '')
                                .replace(/\\s+/g, ' ').trim()
                                === '已托管账号Passkey')
                        : [];
                    const hostStyle = host ? window.getComputedStyle(host) : null;
                    const hostRect = host ? host.getBoundingClientRect() : null;
                    const exact = this.tagName === 'BUTTON'
                        && this.id === 'dialog-btn-0'
                        && this.classList.contains('custom-btn')
                        && this.classList.contains('primary')
                        && label === '使用该Passkey登录';
                    const structure = root instanceof ShadowRoot
                        && root.mode === 'closed'
                        && host && host.tagName === 'DIV' && host.isConnected
                        && dialog && container
                        && dialog.contains(container) && container.contains(this)
                        && titles.length === 1
                        && container.querySelectorAll(
                            'button#dialog-btn-0.custom-btn.primary'
                        ).length === 1
                        && hostStyle && hostRect
                        && hostStyle.display !== 'none'
                        && hostStyle.visibility !== 'hidden'
                        && hostStyle.visibility !== 'collapse'
                        && hostStyle.pointerEvents !== 'none'
                        && hostRect.width >= window.innerWidth * 0.9
                        && hostRect.height >= window.innerHeight * 0.9;
                    const ready = exact && structure
                        && this.isConnected && !this.disabled
                        && this.getAttribute('aria-disabled') !== 'true'
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && style.visibility !== 'collapse'
                        && rect.width > 0 && rect.height > 0;
                    if (!ready) return false;
                    if (ARGUMENT_CLICK) this.click();
                    return true;
                }""".replace("ARGUMENT_CLICK", "true" if click else "false")
            )
            try:
                result = await self._managed_passkey_cdp_call(
                    session.send(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": object_id,
                            "functionDeclaration": function,
                            "returnByValue": True,
                            "awaitPromise": True,
                        },
                    ),
                    operation="runtime_call_function",
                )
            except Exception:
                if click:
                    _logger.warning(
                        "Managed Passkey dispatch result is uncertain; retry guard retained"
                    )
                raise
            ready = (result.get("result") or {}).get("value") is True
            if click and not ready and reservation is not None:
                self._release_dispatch(page, reservation)
                _logger.info(
                    "Managed Passkey changed before dispatch; retry remains available"
                )
            elif click and ready:
                _logger.info("Amazon assisted login action dispatched: managed_passkey")
            return ready
        finally:
            await self._detach_managed_passkey_session(session)

    async def _managed_passkey_cdp_call(
        self,
        awaitable: Any,
        *,
        operation: str,
    ) -> Any:
        """Run one raw managed-Passkey CDP operation with a hard bound."""

        try:
            return await asyncio.wait_for(
                awaitable,
                timeout=self._managed_passkey_cdp_timeout_seconds,
            )
        except TimeoutError:
            _logger.warning(
                "Managed Passkey CDP operation timed out: operation=%s",
                operation,
            )
            raise

    async def _detach_managed_passkey_session(self, session: Any) -> None:
        """Detach a short-lived CDP session without blocking login progress."""

        detach = getattr(session, "detach", None)
        if not callable(detach):
            return
        _logger.info("Managed Passkey CDP detach started")
        try:
            await asyncio.wait_for(
                detach(),
                timeout=self._managed_passkey_detach_timeout_seconds,
            )
        except TimeoutError:
            _logger.warning(
                "Managed Passkey CDP detach timed out; login progression continues"
            )
        except Exception:
            _logger.warning(
                "Managed Passkey CDP detach failed; login progression continues"
            )
        else:
            _logger.info("Managed Passkey CDP detach completed")

    async def _control_label(self, element: Any) -> str:
        values: list[str] = []
        for attribute in ("label", "value", "aria-label"):
            try:
                value = await element.get_attribute(attribute)
            except Exception:
                value = None
            if value:
                values.append(str(value))
        try:
            text = await element.inner_text()
        except Exception:
            text = ""
        if text:
            values.append(str(text))
        unique = {" ".join(value.split()).strip() for value in values if value.strip()}
        if len(unique) != 1:
            return ""
        return next(iter(unique))

    async def _boolean_evaluate(self, element: Any, expression: str) -> bool:
        try:
            # The boolean never crosses an account/code value into Python.
            return bool(await element.evaluate(expression))
        except Exception:
            return False

    async def _is_enabled(self, element: Any) -> bool:
        try:
            if not await self._is_visible(element):
                return False
            if await element.get_attribute("disabled") is not None:
                return False
            if str(await element.get_attribute("aria-disabled")).lower() == "true":
                return False
            return bool(await element.is_enabled())
        except Exception:
            return False

    async def _is_visible(self, element: Any) -> bool:
        visible = getattr(element, "is_visible", None)
        if not callable(visible):
            # Real Playwright locators always provide is_visible.  Lightweight
            # fixture locators may omit it and remain usable for parser tests.
            return True
        try:
            return bool(await visible())
        except Exception:
            return False

    async def _paced_pause(self, page: Any) -> None:
        wait = getattr(page, "wait_for_timeout", None)
        if not callable(wait):
            return
        low, high = self._pacing_range_ms
        milliseconds = int(self._pacing_random(float(low), float(high)))
        try:
            await wait(milliseconds)
            return
        except Exception:
            pass
        if milliseconds > 0:
            await asyncio.sleep(milliseconds / 1000)

    async def _body_text(self, page: Any) -> str:
        try:
            return "\n".join(
                line.strip()
                for line in str(
                    await page.locator("body").inner_text(timeout=3_000)
                ).splitlines()
                if line.strip()
            )
        except Exception:
            return ""


def _managed_passkey_candidates(
    root: dict[str, Any],
) -> list[_ManagedPasskeyCandidate]:
    """Return exact button identities from unique closed-shadow dialogs.

    This parser intentionally ignores every account row and input attribute.
    It accepts only the structure observed in Ziniao's managed-account helper:
    a recognised Ziniao overlay ``div`` host, one closed shadow root, one
    exact title and one exact primary action button in that same root.
    """

    candidates: list[_ManagedPasskeyCandidate] = []

    def walk(node: dict[str, Any]) -> None:
        for shadow in node.get("shadowRoots") or ():
            if (
                str(node.get("nodeName") or "").upper() == "DIV"
                and _is_managed_passkey_shadow_host(node)
                and str(shadow.get("shadowRootType") or "").lower() == "closed"
            ):
                matching_dialogs: list[dict[str, Any]] = []
                for dialog in _cdp_descendants(shadow):
                    dialog_attributes = _cdp_attributes(dialog)
                    if (
                        str(dialog.get("nodeName") or "").upper() != "DIV"
                        or "dialog" not in set(
                            dialog_attributes.get("class", "").split()
                        )
                    ):
                        continue
                    descendants = _cdp_descendants(dialog)
                    titles = [
                        item
                        for item in descendants
                        if str(item.get("nodeName") or "").upper() == "H2"
                        and _cdp_node_text(item) == "已托管账号Passkey"
                    ]
                    containers = [
                        item
                        for item in descendants
                        if str(item.get("nodeName") or "").upper() == "DIV"
                        and _cdp_attributes(item).get("id") == "button-container"
                    ]
                    if len(titles) != 1 or len(containers) != 1:
                        continue
                    buttons: list[dict[str, Any]] = []
                    for button in _cdp_descendants(containers[0]):
                        if (
                            str(button.get("nodeName") or "").upper() != "BUTTON"
                            or _cdp_node_text(button) != "使用该Passkey登录"
                        ):
                            continue
                        attributes = _cdp_attributes(button)
                        classes = set(attributes.get("class", "").split())
                        if (
                            attributes.get("id") == "dialog-btn-0"
                            and {"custom-btn", "primary"}.issubset(classes)
                            and "disabled" not in attributes
                            and attributes.get("aria-disabled", "").lower()
                            != "true"
                        ):
                            buttons.append(button)
                    if len(buttons) == 1:
                        matching_dialogs.append(buttons[0])
                if len(matching_dialogs) == 1:
                    button = matching_dialogs[0]
                    node_id = button.get("nodeId")
                    if isinstance(node_id, int) and node_id > 0:
                        raw_backend_id = button.get("backendNodeId")
                        backend_node_id = (
                            raw_backend_id
                            if isinstance(raw_backend_id, int)
                            and raw_backend_id > 0
                            else None
                        )
                        candidates.append(
                            _ManagedPasskeyCandidate(
                                node_id=node_id,
                                backend_node_id=backend_node_id,
                            )
                        )
            walk(shadow)
        for child in node.get("children") or ():
            walk(child)
        content = node.get("contentDocument")
        if isinstance(content, dict):
            walk(content)

    walk(root)
    return candidates


def _is_managed_passkey_shadow_host(node: dict[str, Any]) -> bool:
    """Accept only Ziniao's two observed closed-shadow overlay hosts.

    Older Ziniao builds used an anonymous ``div`` with no attributes.  The
    current build gives that same host one inline overlay ``style`` attribute.
    No other DOM attribute is accepted, and the security-relevant declarations
    must retain their exact full-screen, top-layer and pointer-enabled values.
    Cosmetic declarations may coexist with this required set.
    """

    raw_attributes = node.get("attributes") or ()
    if not raw_attributes:
        return True
    if (
        len(raw_attributes) != 2
        or str(raw_attributes[0]).strip().casefold() != "style"
    ):
        return False

    declarations: dict[str, str] = {}
    for declaration in str(raw_attributes[1] or "").split(";"):
        declaration = declaration.strip()
        if not declaration:
            continue
        if ":" not in declaration:
            return False
        name, value = declaration.split(":", 1)
        normalized_name = name.strip().casefold()
        normalized_value = " ".join(value.strip().casefold().split())
        if not normalized_name or not normalized_value or normalized_name in declarations:
            return False
        declarations[normalized_name] = normalized_value

    return (
        declarations.get("position") == "fixed"
        and declarations.get("inset") in {"0", "0px"}
        and declarations.get("z-index") == "2147483646"
        and declarations.get("display") == "flex"
        and declarations.get("pointer-events") == "auto"
    )


def _cdp_descendants(node: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def collect(current: dict[str, Any]) -> None:
        values.append(current)
        for child in current.get("children") or ():
            collect(child)
        for shadow in current.get("shadowRoots") or ():
            collect(shadow)

    collect(node)
    return values


def _cdp_node_text(node: dict[str, Any]) -> str:
    parts: list[str] = []

    def collect(current: dict[str, Any]) -> None:
        if str(current.get("nodeName") or "") == "#text":
            value = str(current.get("nodeValue") or "").strip()
            if value:
                parts.append(value)
        for child in current.get("children") or ():
            collect(child)

    collect(node)
    return " ".join(" ".join(parts).split())


def _cdp_attributes(node: dict[str, Any]) -> dict[str, str]:
    raw = list(node.get("attributes") or ())
    return {
        str(raw[index]): str(raw[index + 1])
        for index in range(0, len(raw) - 1, 2)
    }


__all__ = ["AmazonLoginAdvanceResult", "AmazonLoginAdvancer"]

