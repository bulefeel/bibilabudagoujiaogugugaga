"""``plan()`` 的行为：读页面、判定、把结果写成可读的运行事件。

这里最要紧的一条不是「数对不对」，而是**空跑预览必须逐条可读**。运营去掉了逐条
人工闸门（决定 1），所以「先空跑再放量」（决定 6）是这条不可逆流程唯一的人工
关卡——只给一个「候选 3 条」的总数，等于让人盲批一次不可逆的合规声明。
"""

from __future__ import annotations

import pytest

from ziniao_automation.workflows.amazon_gpsr.page import ListOutcome
from ziniao_automation.workflows.amazon_gpsr.workflow import AmazonGpsrWorkflow
from ziniao_automation.workflows.errors import HumanAuthRequired
from ziniao_automation.workflows.types import (
    MarketplaceRef,
    RunMode,
    SiteStatus,
    StoreRef,
    WorkflowRun,
)

from .test_gpsr_row_selection import live_page


DE = MarketplaceRef(
    id="market-de", code="DE", domain="sellercentral.amazon.de", currency="EUR"
)


def make_run() -> WorkflowRun:
    return WorkflowRun(
        id="gpsr-run",
        workflow="amazon_gpsr_attestation",
        mode=RunMode.DRY_RUN,
        store=StoreRef(
            id="3",
            name="秦登友",
            selector_type="oauth",
            selector_value="selector",
            expected_seller_id="dengyou-us",
            identity_confirmed=True,
        ),
        marketplaces=(DE,),
    )


class RecordingRepository:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, SiteStatus]] = []
        # ⚠️ 也要记 ``error``:生产的 SQLAlchemy 仓储把它写进 ``SiteRun.error``,而
        # 卡片的**汇总行**读的就是它。替身把它丢掉的话,「原因必须留在站点错误里」
        # 这一类断言全是空的(2026-09-26 自审)。
        self.site_errors: list[tuple[str, SiteStatus, str | None]] = []
        self.events: list[dict] = []

    async def set_site_status(self, run_id, code, status, *, error=None, **kwargs) -> None:
        self.statuses.append((code, status))
        self.site_errors.append((code, status, error))

    async def append_event(
        self, run_id, event_type, message, *, marketplace_code=None, details=None
    ) -> None:
        self.events.append(
            {
                "type": event_type,
                "message": message,
                "marketplace": marketplace_code,
                "details": details or {},
            }
        )

    def of(self, event_type: str) -> list[dict]:
        return [row for row in self.events if row["type"] == event_type]


class FakeAdapter:
    def __init__(self, outcome: ListOutcome, rows: list[dict] | None = None) -> None:
        self.outcome = outcome
        self.rows = rows if rows is not None else []
        self.opened: list[str] = []

    async def open_policies(self, page, domain):
        self.opened.append(domain)
        return self.outcome

    async def read_rows(self, page):
        return self.rows


def build(outcome: ListOutcome, rows=None):
    repo = RecordingRepository()
    workflow = AmazonGpsrWorkflow(
        repository=repo, page_adapter=FakeAdapter(outcome, rows)
    )
    return repo, workflow


# --------------------------------------------------------------------------
async def test_every_candidate_gets_its_own_readable_event() -> None:
    """⚠️ 空跑预览是唯一的人工闸门，必须逐条可读。

    只报一个总数的话，操作员是在没有任何依据的情况下批准一次不可逆的合规声明。
    """

    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), live_page())

    actionable = await workflow._plan_marketplace(make_run(), object(), DE)

    assert actionable == 2
    candidates = repo.of("gpsr_candidate")
    assert [row["details"]["asin"] for row in candidates] == [
        "B0HJG2CJ5F",
        "B0GXZ1ZZLT",
    ]
    # 事件正文要带上 ASIN 和商品标题，人才认得出它是哪个商品。
    assert "B0HJG2CJ5F" in candidates[0]["message"]
    assert "Highland-Kuh" in candidates[0]["message"]
    # 已下架的那个绝不能出现在待提交里。
    assert "B0GWCRRDZS" not in {row["details"]["asin"] for row in candidates}


async def test_the_scan_summary_separates_removed_from_drift() -> None:
    """三个数字含义完全不同，混在一起操作员就没法判断要不要放量。"""

    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), live_page())
    await workflow._plan_marketplace(make_run(), object(), DE)

    summary = repo.of("gpsr_scanned")[0]["details"]
    assert summary["target_asins"] == 3
    assert summary["candidates"] == 2
    assert summary["already_removed"] == 1
    assert summary["drift_rows"] == 0


async def test_the_site_row_is_created_before_any_event() -> None:
    """``append_event`` 靠站点行来绑定站点，建行之前记的事件会成为孤儿。"""

    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), live_page())
    await workflow._plan_marketplace(make_run(), object(), DE)

    assert repo.statuses[0] == ("DE", SiteStatus.PREFLIGHT)
    # 读到了待提交的条目 → 「待提交」,不是「已跳过」(09-23 第三轮复核:等登录、
    # 空跑、重启时卡片都显示这一格)。
    assert repo.statuses[-1] == ("DE", SiteStatus.PLANNED)


async def test_a_site_with_nothing_to_submit_is_skipped() -> None:
    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), [])
    assert await workflow._plan_marketplace(make_run(), object(), DE) == 0
    assert repo.statuses[-1] == ("DE", SiteStatus.SKIPPED)


# --------------------------------------------------------------------------
async def test_an_expired_session_goes_to_human_auth_not_to_nothing_to_do() -> None:
    """⚠️ 实地踩过：德国站会话一小时就掉，登录页上行选择器匹配 0 个。

    若在这里咽下去当成「该站点没活儿」，任务会报成功——每家店可以永远这样绿下去。
    """

    repo, workflow = build(
        ListOutcome(ok=False, reason="auth_required", landed_url="https://x/ap/signin")
    )
    with pytest.raises(HumanAuthRequired):
        await workflow._plan_marketplace(make_run(), object(), DE)

    # 不能把它记成「跳过」——那会让站点看起来是正常读完的。
    assert ("DE", SiteStatus.SKIPPED) not in repo.statuses
    assert repo.of("gpsr_site_unavailable") == []


async def test_a_store_without_a_german_site_is_skipped_not_failed() -> None:
    """那是关于店铺的事实，不是故障；其它站点必须继续跑。"""

    repo, workflow = build(
        ListOutcome(ok=False, reason="marketplace_unavailable", landed_url="https://x/s")
    )
    assert await workflow._plan_marketplace(make_run(), object(), DE) == 0
    assert repo.of("gpsr_site_unavailable")
    assert ("DE", SiteStatus.SKIPPED) in repo.statuses


async def test_the_account_switcher_is_not_reported_as_an_unopened_marketplace() -> None:
    """两种状态不能共用一句人话。

    2026-09-18 正香-张刚CA 实测：登录德国站成功后落在账户切换页，而那一页上
    「德國」在列且**不是**「待註冊」——账号有这个站点，只是本次会话没选定它
    （选定是粘性的，每店一次）。流程当时对操作员说的是「该店铺未开通此站点」，
    那是一句假话，会把人指去查一个根本不存在的开店问题。

    ⚠️ 断言打在**消息正文**上，不是只打在 reason_code 上：说谎的是那句话。
    """

    repo, workflow = build(
        ListOutcome(
            ok=False,
            reason="marketplace_not_selected",
            landed_url="https://x/account-switcher/default/merchantMarketplace",
        )
    )
    assert await workflow._plan_marketplace(make_run(), object(), DE) == 0
    assert ("DE", SiteStatus.SKIPPED) in repo.statuses

    events = repo.of("gpsr_site_unavailable")
    assert len(events) == 1
    message = events[0]["message"]
    assert "该店铺未开通此站点" not in message, f"断言了没读过的原因：{message}"
    assert "账户/站点选择页" in message, message   # 唯一真正观察到的事实
    assert "紫鸟" in message, message              # 可操作的下一步
    assert events[0]["details"]["reason_code"] == "marketplace_not_selected"


async def test_neither_landing_message_asserts_a_cause_it_never_read() -> None:
    """两句话都只许陈述「看见了什么」。

    ⚠️ 这条测试的前身只钉了「别说未开通」，于是我顺手把它改成了断言
    「账号有此站点、去选一次」——同样是没读过就写死的原因，只是方向反了。
    迁移 0010 给**每一家**店铺无条件补了 enabled=1 的 DE 行，所以真没开德国站的
    店是多数，它们会被指去选一个不存在的站点。区分两者的证据在切换页的 DOM 里，
    代码没读它。
    """

    forbidden = ("未开通", "尚未选定", "已开通此站点")
    for reason in ("marketplace_not_selected", "marketplace_unavailable"):
        repo, workflow = build(
            ListOutcome(ok=False, reason=reason, landed_url="https://x/s")
        )
        assert await workflow._plan_marketplace(make_run(), object(), DE) == 0
        message = repo.of("gpsr_site_unavailable")[0]["message"]
        for word in forbidden:
            assert word not in message, f"{reason} 断言了没读过的原因：{message}"


async def test_the_two_landings_do_not_share_one_sentence() -> None:
    """反向验证：上面那条不是靠把两句话合并成一句空话来通过的。"""

    messages = []
    for reason in ("marketplace_not_selected", "marketplace_unavailable"):
        repo, workflow = build(
            ListOutcome(ok=False, reason=reason, landed_url="https://x/s")
        )
        await workflow._plan_marketplace(make_run(), object(), DE)
        messages.append(repo.of("gpsr_site_unavailable")[0]["message"])
    assert messages[0] != messages[1]


async def test_an_empty_list_is_recorded_as_such() -> None:
    repo, workflow = build(ListOutcome(ok=True, rows_loaded=False))
    assert await workflow._plan_marketplace(make_run(), object(), DE) == 0
    assert repo.of("gpsr_list_empty")


# --------------------------------------------------------------------------
async def test_an_unknown_reason_is_surfaced_since_nothing_blocks_on_it() -> None:
    """运营去掉了「未知原因即阻断」那道阀门，所以这条事件是信号**唯一**的去处。

    它同时意味着两件事：亚马逊新增了合规品类（无害），或者亚马逊改了目标那句
    文案——后者会让候选静默变 0，而页面上明明有活儿。
    """

    drifted = live_page() + [
        {
            "cells": [
                "GPSR：全新的某个合规要求", "2026年9月11日", "商品\nASIN：B0XXXXXXXX",
                "过去 12 个月无销量", "商品有移除风险", "无影响", "查看商品信息",
            ],
            "buttonId": "ahd-action-button-B0XXXXXXXX",
            "inAccordionHeader": False,
            "inAccordionBody": False,
        }
    ]
    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), drifted)
    await workflow._plan_marketplace(make_run(), object(), DE)

    reported = repo.of("gpsr_unknown_reason")
    assert reported, "未知原因不阻断了，那就更不能连报都不报"
    assert "GPSR：全新的某个合规要求" in reported[0]["details"]["unknown_reasons"]
    # 而且它**不**影响正常候选的产出。
    assert len(repo.of("gpsr_candidate")) == 2


async def test_a_clean_scan_does_not_cry_wolf() -> None:
    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), live_page())
    await workflow._plan_marketplace(make_run(), object(), DE)
    assert repo.of("gpsr_unknown_reason") == []


# --------------------------------------------------------------------------
async def test_plan_returns_no_lines_when_there_is_nothing_to_submit() -> None:
    """空计划让引擎把任务收成正常完成，不会走到任何会动手的分支。"""

    repo, workflow = build(ListOutcome(ok=True, rows_loaded=False))
    plan = await workflow.plan(make_run(), object())
    assert plan.lines == ()


async def test_plan_keeps_a_line_when_a_site_has_work() -> None:
    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), live_page())
    plan = await workflow.plan(make_run(), object())
    assert len(plan.lines) == 1
    snapshot = plan.lines[0].snapshot
    assert snapshot.can_submit is True
    assert snapshot.settlement_key == "gpsr:DE"
    # 金额字段结构上必须有，但必须全是 0——它一分钱都不碰。
    assert snapshot.payable_amount == 0
    assert snapshot.delayed_amount == 0


async def test_execute_never_returns_an_operation_record() -> None:
    """写一条 operation_guards 就会把任务拖进 UNCERTAIN_FINANCIAL，
    然后在资金审核台上渲染成一笔提现。这条流程永远返回空。
    """

    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), live_page())
    run = make_run()
    plan = await workflow.plan(run, object())
    assert await workflow.execute(run, object(), plan) == ()
    assert await workflow.reconcile(run, object(), ()) == ()


async def test_no_asin_reaches_the_log_file(caplog) -> None:
    """ASIN 和商品标题不进 ``data/logs/*.jsonl``——和金额、收款尾号同一个口径。

    逐条明细走 run_events（只进 SQLite）。
    """

    import logging

    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), live_page())
    with caplog.at_level(logging.INFO):
        await workflow._plan_marketplace(make_run(), object(), DE)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("B0HJG2CJ5F", "B0GXZ1ZZLT", "Highland-Kuh"):
        assert secret not in logged, secret
    # 但计数必须在，否则日志排查就没东西可看。
    assert "gpsr_site" in logged


async def test_every_skipped_asin_is_named_not_just_counted() -> None:
    """⚠️ 「已下架跳过 1 个」这个数字操作员没法核对。

    空跑预览是这条不可逆流程唯一的人工闸门（决定 1 去掉了逐条确认，决定 6 用空跑
    代替）。页面上有 11 条目标行、我们只提交 10 条，那被放掉的是哪一条**必须写出
    ASIN**，否则「我们跳过的」和「他在页面上看到的」根本对不上。
    """

    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), live_page())
    await workflow._plan_marketplace(make_run(), object(), DE)

    skipped = repo.of("gpsr_already_removed")
    assert [row["details"]["asin"] for row in skipped] == ["B0GWCRRDZS"]
    assert "B0GWCRRDZS" in skipped[0]["message"]
    # 逐条事件的条数必须和汇总里的计数一致，否则两个数字会各说各话。
    assert len(skipped) == repo.of("gpsr_scanned")[0]["details"]["already_removed"]


async def test_the_three_dispositions_account_for_every_target_row() -> None:
    """目标条目数 = 候选 + 已下架跳过。对不上就说明有行被悄悄吃掉了。"""

    repo, workflow = build(ListOutcome(ok=True, rows_loaded=True), live_page())
    await workflow._plan_marketplace(make_run(), object(), DE)

    summary = repo.of("gpsr_scanned")[0]["details"]
    assert summary["target_asins"] == summary["candidates"] + summary["already_removed"]
    assert len(repo.of("gpsr_candidate")) == summary["candidates"]
    assert len(repo.of("gpsr_already_removed")) == summary["already_removed"]


async def test_the_reason_auto_selection_failed_reaches_the_event() -> None:
    """自动选站失败的**具体原因**要进事件,否则运营只看到「停在选择页」,
    分不清是列表里压根没有这个国家,还是点了没落地。"""

    for switch_reason, seen in (
        ("not_offered", "没有与此站点完全一致的国家"),
        ("timeout", "自动选站没有完成（timeout）"),
    ):
        repo, workflow = build(
            ListOutcome(
                ok=False,
                reason="marketplace_not_selected",
                landed_url="https://x/account-switcher",
                switch_reason=switch_reason,
            )
        )
        await workflow._plan_marketplace(make_run(), object(), DE)
        event = repo.of("gpsr_site_unavailable")[0]
        assert event["details"]["switch_reason"] == switch_reason
        assert seen in event["message"], event["message"]
