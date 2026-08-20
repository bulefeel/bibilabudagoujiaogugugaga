from datetime import datetime, timezone
from decimal import Decimal
import json

import httpx
import pytest

from ziniao_automation.notifications import (
    NotificationKind,
    SafeRunNotice,
    SafeSiteNotice,
)
from ziniao_automation.notifications.feishu import FeishuNotifier
from ziniao_automation.workflows.types import RunStatus, WorkflowReport


def notice(
    kind: NotificationKind = NotificationKind.PAYMENT_CONFIRMED,
    **overrides,
) -> SafeRunNotice:
    values = {
        "kind": kind,
        "title": "提现结果已确认",
        "summary": "平台付款记录已回读确认",
        "run_short_id": "run-1234",
        "store_name": "示例店铺",
        "mode": "approval",
        "trigger": "schedule",
        "schedule_name": "工作日余额检查",
        "scheduled_for": datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
        "started_at": datetime(2026, 8, 13, 1, 8, tzinfo=timezone.utc),
        "queue_delay_minutes": 8,
        "sites": (
            SafeSiteNotice(
                code="CA",
                currency="CAD",
                payable=Decimal("197.63"),
                delayed=Decimal("2295.78"),
                outcome="CONFIRMED",
                account_tail="BANK-12345678",
                reference_suffix="PAYMENT-ABC98765",
            ),
        ),
    }
    values.update(overrides)
    return SafeRunNotice(**values)


@pytest.mark.asyncio
async def test_feishu_fetches_token_then_sends_detailed_safe_card() -> None:
    requests: list[httpx.Request] = []
    cards: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            payload = json.loads(request.content)
            assert payload == {"app_id": "APP", "app_secret": "SECRET"}
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "TOKEN", "expire": 7200},
            )
        assert request.headers["authorization"] == "Bearer TOKEN"
        assert request.url.params["receive_id_type"] == "chat_id"
        payload = json.loads(request.content)
        assert payload["receive_id"] == "oc_chat"
        assert payload["msg_type"] == "interactive"
        cards.append(json.loads(payload["content"]))
        return httpx.Response(200, json={"code": 0})

    notifier = FeishuNotifier(
        lambda: {"app_id": "APP", "app_secret": "SECRET", "chat_id": "oc_chat"},
        transport=httpx.MockTransport(handler),
    )
    await notifier.send(notice())
    await notifier.send(notice())

    token_calls = [r for r in requests if r.url.path.endswith("tenant_access_token/internal")]
    assert len(token_calls) == 1
    assert len(requests) == 3
    assert cards[0]["header"]["template"] == "green"
    text = cards[0]["elements"][0]["text"]["content"]
    assert "工作日余额检查" in text
    assert "CAD 197.63" in text
    assert "延迟资金 CAD 2295.78" in text
    assert "•••• 5678" in text
    assert "••••8765" in text
    assert "12345678" not in text
    assert "ABC98765" not in text


@pytest.mark.asyncio
async def test_feishu_error_text_does_not_expose_secret() -> None:
    notifier = FeishuNotifier(
        lambda: {"app_id": "APP", "app_secret": "VERY_SECRET", "chat_id": "oc_chat"},
        transport=httpx.MockTransport(lambda _: httpx.Response(500, text="VERY_SECRET")),
    )
    with pytest.raises(RuntimeError) as caught:
        await notifier.send(notice(NotificationKind.RUN_FAILED))
    assert "VERY_SECRET" not in str(caught.value)


@pytest.mark.asyncio
async def test_generic_success_without_confirmed_operation_sends_neutral_card() -> None:
    calls = 0
    cards: list[dict] = []

    def provider() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"app_id": "APP", "app_secret": "SECRET", "chat_id": "CHAT"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "TOKEN", "expire": 7200},
            )
        cards.append(json.loads(json.loads(request.content)["content"]))
        return httpx.Response(200, json={"code": 0})

    report = WorkflowReport(
        "run-1",
        RunStatus.SUCCEEDED,
        "任务完成",
        "无付款数据",
        {
            "store": "店铺",
            "mode": "dry_run",
            "trigger": "manual",
            "operations": [],
        },
    )
    await FeishuNotifier(provider, transport=httpx.MockTransport(handler)).send(report)
    assert calls == 1
    assert len(cards) == 1
    assert cards[0]["header"]["template"] == "blue"
    text = cards[0]["elements"][0]["text"]["content"]
    assert "任务检查已完成（非提现确认）" in text
    assert "这不代表提现成功" in text
    assert "只读检查" in text


@pytest.mark.asyncio
async def test_missing_credentials_is_a_retryable_delivery_error() -> None:
    notifier = FeishuNotifier(lambda: None)
    with pytest.raises(RuntimeError, match="凭据未配置或不可读取"):
        await notifier.send(
            notice(
                NotificationKind.RUN_COMPLETED,
                title="任务检查已完成（非提现确认）",
                summary="只读检查已完成；这不代表提现成功。",
            )
        )


@pytest.mark.asyncio
async def test_legacy_report_uses_allowlist_and_never_forwards_unknown_mapping() -> None:
    message_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "TOKEN", "expire": 7200},
            )
        message_requests.append(request)
        return httpx.Response(200, json={"code": 0})

    report = WorkflowReport(
        "1234567890",
        RunStatus.WAITING_APPROVAL,
        "待审核",
        "请核对金额",
        {
            "store": "允许的店铺名",
            "mode": "approval",
            "marketplaces": ["CA"],
            "cookie": "COOKIE_SHOULD_NEVER_LEAVE",
            "token": "TOKEN_SHOULD_NEVER_LEAVE",
            "seller_id": "FULL_SELLER_ID_SHOULD_NEVER_LEAVE",
            "screenshot_path": r"D:\secret\raw.png",
            "nested": {"app_secret": "SECRET_SHOULD_NEVER_LEAVE"},
        },
    )
    notifier = FeishuNotifier(
        lambda: {"app_id": "APP", "app_secret": "CREDENTIAL", "chat_id": "CHAT"},
        transport=httpx.MockTransport(handler),
    )
    await notifier.send(report)
    body = message_requests[0].content.decode("utf-8")
    for forbidden in (
        "COOKIE_SHOULD_NEVER_LEAVE",
        "TOKEN_SHOULD_NEVER_LEAVE",
        "FULL_SELLER_ID_SHOULD_NEVER_LEAVE",
        r"D:\secret\raw.png",
        "SECRET_SHOULD_NEVER_LEAVE",
    ):
        assert forbidden not in body
    assert "允许的店铺名" in body


@pytest.mark.asyncio
async def test_all_risk_cards_use_expected_colour_and_escape_markdown() -> None:
    cards: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "TOKEN", "expire": 7200},
            )
        cards.append(json.loads(json.loads(request.content)["content"]))
        return httpx.Response(200, json={"code": 0})

    notifier = FeishuNotifier(
        lambda: {"app_id": "APP", "app_secret": "SECRET", "chat_id": "CHAT"},
        transport=httpx.MockTransport(handler),
    )
    await notifier.send(
        notice(
            NotificationKind.WAITING_AUTH,
            title="登录验证",
            summary="[点我](https://evil.invalid)\n@所有人",
            sites=(),
        )
    )
    await notifier.send(notice(NotificationKind.RUN_FAILED))
    await notifier.send(notice(NotificationKind.CROSS_DAY_STARTED))
    # Blue, not red: every site was checked and none had anything to send.
    # Colouring Amazon's ordinary 24-hour cap as an incident trains the
    # operator to ignore the cards that do mean something.
    await notifier.send(notice(NotificationKind.RUN_SKIPPED))

    assert [card["header"]["template"] for card in cards] == [
        "orange",
        "red",
        "blue",
        "blue",
    ]
    text = cards[0]["elements"][0]["text"]["content"]
    assert "\\[点我\\]\\(https://evil\\.invalid\\)" in text
    assert "\n@所有人" not in text


@pytest.mark.asyncio
async def test_safe_notice_free_text_redacts_copied_sensitive_error_fragments() -> None:
    messages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "TOKEN", "expire": 7200},
            )
        messages.append(request.content.decode("utf-8"))
        return httpx.Response(200, json={"code": 0})

    notifier = FeishuNotifier(
        lambda: {"app_id": "APP", "app_secret": "SECRET", "chat_id": "CHAT"},
        transport=httpx.MockTransport(handler),
    )
    await notifier.send(
        notice(
            NotificationKind.RUN_FAILED,
            summary=(
                r"Cookie=COOKIE_VALUE Token:BEARER_VALUE "
                r"seller_id=SELLER_FULL_123 D:\runs\private\evidence.png "
                "0123456789abcdef0123456789abcdef 6222021234567890"
            ),
            run_short_id="0123456789abcdef0123456789abcdef",
            sites=(),
        )
    )
    body = messages[0]
    for forbidden in (
        "COOKIE_VALUE",
        "BEARER_VALUE",
        "SELLER_FULL_123",
        r"D:\\runs\\private\\evidence.png",
        "0123456789abcdef0123456789abcdef",
        "6222021234567890",
    ):
        assert forbidden not in body
    assert "01234567" in body
    assert "••••7890" in body


def test_safe_notice_rejects_arbitrary_site_mappings() -> None:
    with pytest.raises(TypeError, match="SafeSiteNotice"):
        SafeRunNotice(
            kind=NotificationKind.RUN_FAILED,
            title="失败",
            summary="失败",
            run_short_id="run",
            store_name="store",
            sites=({"cookie": "not allowed"},),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_large_financial_amount_is_not_mistaken_for_an_account_number() -> None:
    messages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "TOKEN", "expire": 7200},
            )
        messages.append(request.content.decode("utf-8"))
        return httpx.Response(200, json={"code": 0})

    notifier = FeishuNotifier(
        lambda: {"app_id": "APP", "app_secret": "SECRET", "chat_id": "CHAT"},
        transport=httpx.MockTransport(handler),
    )
    await notifier.send(
        notice(
            sites=(
                SafeSiteNotice(
                    code="UK", currency="GBP", payable=Decimal("12345678.90")
                ),
            )
        )
    )
    assert "GBP 12345678.90" in messages[0]


def test_every_site_outcome_renders_in_chinese_with_its_reason() -> None:
    """The card is read by people who never see the code.

    Three completely different situations used to render as the same word:
    「结果 SKIPPED」 for a zero balance, for Amazon throttling us, and for a
    site blocked by an unfinished money record.  The operator could not tell
    them apart, and a card whose only English word is the outcome invites the
    question "what does SKIPPED mean?" every single day.
    """

    from ziniao_automation.notifications.feishu import _message

    card = _message(
        notice(
            kind=NotificationKind.RUN_PARTIAL,
            sites=(
                SafeSiteNotice(
                    code="AU",
                    currency="AUD",
                    payable=Decimal("76.89"),
                    outcome="SKIPPED",
                    reason="亚马逊限制该账户 24 小时内仅可请求一次提现，约 6 小时 47 分钟后可再次请求",
                ),
                SafeSiteNotice(
                    code="CA",
                    currency="CAD",
                    payable=Decimal("0.00"),
                    outcome="SKIPPED",
                    reason="标准订单可用资金为 0",
                ),
                SafeSiteNotice(
                    code="UK",
                    currency="GBP",
                    payable=Decimal("1710.23"),
                    outcome="FAILED",
                    reason="确认页收款账户与建档基线不一致：建档 尾号 003，确认页 尾号 402",
                ),
            ),
        )
    )

    assert "已跳过：亚马逊限制该账户 24 小时内仅可请求一次提现" in card
    assert "已跳过：标准订单可用资金为 0" in card
    assert "执行失败：确认页收款账户与建档基线不一致" in card
    # No raw enum names anywhere in what the operator reads.
    for token in ("SKIPPED", "FAILED", "ARMED", "SUBMITTED", "UNCERTAIN"):
        assert token not in card


def test_guard_states_say_whether_the_payout_actually_went_out() -> None:
    """ARMED and SUBMITTED are opposite answers to the only question that matters."""

    from ziniao_automation.notifications.feishu import _message

    card = _message(
        notice(
            kind=NotificationKind.UNCERTAIN_FINANCIAL,
            sites=(
                SafeSiteNotice(code="UK", currency="GBP", payable=Decimal("1.00"), outcome="ARMED"),
                SafeSiteNotice(code="AU", currency="AUD", payable=Decimal("2.00"), outcome="SUBMITTED"),
            ),
        )
    )

    assert "已锁定（未确认发出）" in card
    assert "已发出请求" in card


def test_exception_class_names_never_reach_the_card() -> None:
    """"PreflightRejected:" is an implementation detail, not a message."""

    from ziniao_automation.notifications.feishu import _message, _short_reason

    assert _short_reason("PreflightRejected: 确认页收款账户与建档基线不一致").startswith(
        "确认页收款账户"
    )
    assert _short_reason("DomContractError: 确认页找不到当前结算金额") == (
        "确认页找不到当前结算金额"
    )
    card = _message(
        notice(
            kind=NotificationKind.RUN_PARTIAL,
            sites=(
                SafeSiteNotice(
                    code="UK",
                    outcome="FAILED",
                    reason="PreflightRejected: 账户不一致。请到建档页处理；否则请核查安全。",
                ),
            ),
        )
    )
    assert "PreflightRejected" not in card
    # Guidance paragraphs stay out of the bullet; the summary carries them.
    assert "否则请核查安全" not in card


def test_card_reports_the_payout_destination_and_flags_a_change() -> None:
    """The destination is now evidence, so the card has to actually show it.

    Nothing blocks on a changed payout account any more — Amazon owns it and
    this automation cannot alter it — but the operator should still be able to
    notice, after the fact, that the money went somewhere new.
    """

    from ziniao_automation.notifications.feishu import _message

    card = _message(
        notice(
            kind=NotificationKind.UNCERTAIN_FINANCIAL,
            sites=(
                SafeSiteNotice(
                    code="UK",
                    currency="GBP",
                    payable=Decimal("1710.23"),
                    outcome="SUBMITTED",
                    account_tail="402",
                    account_changed=True,
                ),
                SafeSiteNotice(
                    code="AU",
                    currency="AUD",
                    payable=Decimal("76.89"),
                    outcome="SUBMITTED",
                    account_tail="465",
                ),
            ),
        )
    )

    assert "（与上次不同）" in card
    # Only the site that actually changed carries the note.
    assert card.count("（与上次不同）") == 1
    # And it is a note, not a status: both sites still报告为已发出.
    assert card.count("已发出请求") == 2
