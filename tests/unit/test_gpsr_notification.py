"""GPSR 的飞书卡片不能用提现的措辞。

一张说谎的卡片比没有卡片更糟：这条流程一分钱都不碰，而卡片的标题、状态、下一步
和站点行原本全是围绕金额写的——操作员会看到「EUR 0.00 可提现」，读起来像这家店
提不出钱。

⚠️ 断言必须打在**渲染完的报文**上。只断言 ``notice.title``/``summary`` 会漏掉
feishu.py 自己按 kind 填的那两行（状态、下一步），而那两行正是提现措辞的老巢。
"""

from __future__ import annotations

import json

import pytest

from ziniao_automation.notifications.dto import (
    NotificationKind,
    SafeRunNotice,
    SafeSiteNotice,
)
from ziniao_automation.notifications import feishu as feishu_mod
from ziniao_automation.notifications.database import _summary_for


GPSR = "amazon_gpsr_attestation"
PAYOUT_WORDS = ("提现", "可提现", "资金", "收款", "转账", "金额")


def notice(kind: NotificationKind, *, workflow: str = GPSR, **overrides) -> SafeRunNotice:
    payload = {
        "run_short_id": "gpsr-run",
        "kind": kind,
        "store_name": "秦登友",
        "workflow": workflow,
        "title": "GPSR 安全证明 · 秦登友",
        "summary": "空跑：本次只读取与判定，未提交任何安全证明。",
        "sites": (
            SafeSiteNotice(
                code="DE",
                currency="",
                payable=None,
                delayed=None,
                outcome="SKIPPED",
                account_tail="",
                reference_suffix="",
            ),
        ),
    }
    payload.update(overrides)
    return SafeRunNotice(**payload)


def rendered(value: SafeRunNotice) -> str:
    """整张卡片的可读文本——包含 feishu.py 自己填的状态与下一步两行。"""

    return json.dumps(feishu_mod._message(value), ensure_ascii=False)


@pytest.mark.parametrize(
    "kind",
    [
        NotificationKind.RUN_COMPLETED,
        NotificationKind.RUN_SKIPPED,
        NotificationKind.RUN_FAILED,
        NotificationKind.RUN_PARTIAL,
    ],
)
def test_no_payout_wording_survives_on_any_kind_of_card(kind: NotificationKind) -> None:
    text = rendered(notice(kind))
    for word in PAYOUT_WORDS:
        assert word not in text, f"{kind.name} 的卡片里出现了提现措辞：{word}"


def test_the_same_payload_as_a_payout_run_still_says_payout_things() -> None:
    """反向验证上面那条不是空转：换成提现流程，提现措辞必须回来。

    否则一个把措辞整个删光的改动会让上面四条断言全绿。
    """

    text = rendered(notice(NotificationKind.RUN_COMPLETED, workflow="amazon_disbursement"))
    assert any(word in text for word in PAYOUT_WORDS)


def test_the_card_names_the_workflow_so_two_flows_are_not_confused() -> None:
    text = rendered(notice(NotificationKind.RUN_COMPLETED))
    assert "安全证明" in text


def test_the_next_action_points_at_the_run_timeline_not_the_feedback_desk() -> None:
    """GPSR 没有自己的页面，逐条候选在运行详情的时间线上。

    照抄反馈流程的文案会把操作员指到一个跟它无关的「反馈处理台」。
    """

    text = rendered(notice(NotificationKind.RUN_COMPLETED))
    assert "运行详情" in text
    assert "反馈处理台" not in text


def test_a_site_row_shows_no_money_column() -> None:
    """站点行不能渲染成「EUR 0.00 可提现」。"""

    text = rendered(notice(NotificationKind.RUN_COMPLETED))
    assert "0.00" not in text
    assert "EUR" not in text


# --------------------------------------------------------------------------
def test_per_asin_events_are_not_site_reason_events() -> None:
    """``gpsr_candidate`` 是逐 ASIN 的，放进站点原因事件会一条条覆盖站点汇总。

    结果是卡片上只剩最后一个商品的名字，站点读取情况整个消失。
    """

    from ziniao_automation.notifications.database import SITE_REASON_EVENTS

    assert "gpsr_scanned" in SITE_REASON_EVENTS
    assert "gpsr_site_unavailable" in SITE_REASON_EVENTS
    assert "gpsr_unknown_reason" in SITE_REASON_EVENTS
    assert "gpsr_candidate" not in SITE_REASON_EVENTS


def test_gpsr_is_registered_as_non_financial() -> None:
    from ziniao_automation.notifications.database import (
        GPSR_WORKFLOW_KEY,
        NON_FINANCIAL_WORKFLOWS,
    )

    assert GPSR_WORKFLOW_KEY in NON_FINANCIAL_WORKFLOWS
    assert "amazon_disbursement" not in NON_FINANCIAL_WORKFLOWS


WORKFLOWS = ("amazon_gpsr_attestation", "amazon_feedback_removal", "amazon_disbursement")
COMPLETION_WORDS = ("已完成", "完成。")


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_waiting_for_a_human_never_reads_as_completed(workflow: str) -> None:
    """一次停在登录页、等着人接管的运行，不许在卡片上自称完成。

    2026-09-18 正香的德国站停在 ``/ap/signin``，飞书卡的正文是「GPSR 安全证明已完成。
    浏览器保留现场，等待人工完成验证」——同一张卡自相矛盾。原因是「等待人工验证」这句
    只写在提现分支里，GPSR 与反馈流程各有自己的汇总函数，都够不着；不认识的 kind 默认
    落到「完成」那一句，等于把未知当成功。

    ⚠️ 逐流程参数化不是凑数：只测 GPSR 会漏掉反馈流程里一模一样的那份拷贝。
    """

    text = rendered(
        notice(
            NotificationKind.WAITING_AUTH,
            workflow=workflow,
            summary=_summary_for(
                NotificationKind.WAITING_AUTH,
                workflow=workflow,
                run_mode="auto",
                run_error="等待人工验证（sign_in）：Amazon 登录操作按钮缺失、禁用或不唯一",
                site_payables=[],
                site_errors=[],
                site_skip_reasons=[],
                event_message="浏览器保留现场，等待人工完成验证",
                has_sites=True,
            ),
        )
    )
    for word in COMPLETION_WORDS:
        assert word not in text, f"{workflow} 的等待验证卡片自称完成：{word}"
    assert "等待人工" in text


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_a_genuinely_completed_run_still_says_so(workflow: str) -> None:
    """反向验证：上面那条不是靠把「完成」二字从代码里删干净来通过的。"""

    text = rendered(
        notice(
            NotificationKind.RUN_COMPLETED,
            workflow=workflow,
            summary=_summary_for(
                NotificationKind.RUN_COMPLETED,
                workflow=workflow,
                run_mode="auto",
                run_error=None,
                site_payables=[],
                site_errors=[],
                site_skip_reasons=[],
                event_message=None,
                has_sites=True,
            ),
        )
    )
    assert any(word in text for word in COMPLETION_WORDS), f"{workflow} 的完成卡片不说完成"
