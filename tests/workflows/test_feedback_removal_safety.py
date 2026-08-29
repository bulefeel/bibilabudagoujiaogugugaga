"""反馈删除流程的安全性质。

提交「请求审核」是**不可逆**的：亚马逊对一条反馈只受理一次。所以这里钉住的不是
「功能能跑」，而是四件「绝不能发生」的事：

1. **绝不对 4-5 星发起删除请求**——筛选器可能没生效、页面可能在两次读取之间变化，
   所以星级是逐条、在动手前一刻从**那一行自己**读出来的
2. **同一条反馈绝不提交第二次**——载体是 ``feedback_reviews`` 的唯一约束，
   不是资金流程那张按「站点×天」的 ``operation_guards``
3. **判不准就不提交**——分类器超时/报错/给出白名单外的值，一律落人工
4. **超出单次上限就停**——爆炸半径

另外钉住一条会被误改的接线：本流程**不占全局资金锁**。占了的话一次长时间的反馈
清扫会挡住所有店铺的提现，而亚马逊的提现是滚动 24 小时一次，挡住就是漏掉一次。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import FeedbackReview, Run
from ziniao_automation.repositories import StoreRepository
from ziniao_automation.workflows.amazon_feedback import (
    AmazonFeedbackWorkflow,
    FeedbackReviewStore,
    build_amazon_feedback_definition,
)
from ziniao_automation.workflows.amazon_feedback.classifier import ReasonDecision
from ziniao_automation.workflows.amazon_feedback.page import FeedbackRow, ListOutcome
from ziniao_automation.workflows.amazon_feedback.review_store import (
    NEEDS_HUMAN,
    PENDING,
    ALREADY_REQUESTED,
    SUBMITTED,
)
from ziniao_automation.workflows.types import (
    MarketplaceRef,
    RunMode,
    StoreRef,
    WorkflowRun,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakePage:
    """Stands in for the Playwright page adapter, recording every submission."""

    def __init__(self, rows: Sequence[FeedbackRow]) -> None:
        self._rows = list(rows)
        self.submitted: list[tuple[str, str, str]] = []
        self.opened: list[str] = []
        self.closed = 0
        self.select_result_ok = True
        self.unavailable = False
        self.truncated = False
        self.reloads = 0

    async def open_list(self, page: Any, domain: str) -> ListOutcome:
        if self.unavailable:
            # Seller Central bounces to the account switcher for a marketplace
            # the store does not sell on.
            return ListOutcome(ok=False, reason="marketplace_unavailable")
        return ListOutcome(ok=True, rows_loaded=bool(self._rows))

    async def read_all_rows(self, page: Any) -> tuple[list[FeedbackRow], bool]:
        return list(self._rows), self.truncated

    async def read_rows(self, page: Any) -> list[FeedbackRow]:
        return list(self._rows)

    async def open_removal_panel(self, page: Any, order_id: str) -> dict[str, Any]:
        row = next((r for r in self._rows if r.order_id == order_id), None)
        if row is None:
            return {"ok": False, "reason": "row_not_found"}
        if not row.is_low_star:
            return {"ok": False, "reason": "rating_out_of_range"}
        if not row.removal_available:
            return {"ok": False, "reason": "removal_not_offered"}
        self.opened.append(order_id)
        return {"ok": True, "rating": row.rating}

    async def select_reason(
        self, page: Any, order_id: str, category: str, reason_code: str
    ) -> dict[str, Any]:
        if not self.select_result_ok:
            return {"ok": False, "reason": "reason_not_offered"}
        return {"ok": True, "submit_disabled": False}

    async def submit_removal(
        self, page: Any, order_id: str, category: str, reason_code: str
    ) -> dict[str, Any]:
        row = next((r for r in self._rows if r.order_id == order_id), None)
        # Mirrors the browser-side invariant: the click never happens for a row
        # outside 1-3 stars, whatever the caller believed.
        if row is None or not row.is_low_star:
            return {"ok": False, "reason": "rating_out_of_range"}
        self.submitted.append((order_id, category, reason_code))
        # Amazon stops offering the action once a review has been requested.
        self._rows = [
            FeedbackRow(
                r.order_id, r.rating, r.order_date, r.comment,
                removal_available=False if r.order_id == order_id else r.removal_available,
            )
            for r in self._rows
        ]
        return {"ok": True}

    async def close_panels(self, page: Any) -> int:
        self.closed += 1
        return 1

    async def confirm_removal_requested(
        self, page: Any, domain: str, order_id: str, *, attempts: int = 3
    ) -> tuple[bool, list[FeedbackRow]]:
        self.reloads += 1
        row = next((r for r in self._rows if r.order_id == order_id), None)
        confirmed = row is not None and not row.removal_available
        return confirmed, list(self._rows)


class FakeClassifier:
    def __init__(self, decision: ReasonDecision | None) -> None:
        self.decision = decision
        self.calls = 0

    async def classify(
        self,
        *,
        comment: str,
        rating: int,
        order_date: str | None = None,
    ):
        self.calls += 1
        return self.decision


class RecordingRepository:
    """Only the two repository methods this workflow actually uses."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.site_status: dict[str, str] = {}

    async def append_event(
        self, run_id, event_type, message, *, marketplace_code=None, details=None
    ):
        self.events.append((event_type, marketplace_code or "", dict(details or {})))

    async def set_site_status(self, run_id, marketplace_code, status, *, error=None):
        self.site_status[marketplace_code] = getattr(status, "value", str(status))


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
CA = MarketplaceRef(id="1", code="CA", domain="sellercentral.amazon.ca", currency="CAD")
DECISION = ReasonDecision(
    category="delivery-related-feedback", reason_code="402", note="亚马逊配送"
)


@pytest.fixture()
def session_factory(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'feedback.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    # store_id and run_id are real foreign keys; give them real rows.
    with factory() as session:
        StoreRepository(session).create(
            name="测试店铺", selector_type="oauth", selector_value="oauth-1"
        )
        session.flush()
        for run_id in ("run-1", "run-2"):
            session.add(
                Run(
                    id=run_id,
                    store_id=1,
                    workflow="amazon_feedback_removal",
                    mode="auto",
                    status="RUNNING",
                )
            )
        session.commit()
    yield factory
    engine.dispose()


def make_run(run_id: str = "run-1", *, mode: RunMode = RunMode.AUTO, budget: int = 20):
    return WorkflowRun(
        id=run_id,
        workflow="amazon_feedback_removal",
        mode=mode,
        store=StoreRef(
            id="1",
            name="测试店铺",
            selector_type="oauth",
            selector_value="oauth-1",
            expected_seller_id="",
            identity_confirmed=True,
        ),
        marketplaces=(CA,),
        workflow_config={"marketplace_codes": ["CA"], "max_submissions_per_run": budget},
    )


def build(session_factory, rows, *, decision=DECISION):
    page = FakePage(rows)
    repo = RecordingRepository()
    workflow = AmazonFeedbackWorkflow(
        repository=repo,
        review_store=FeedbackReviewStore(session_factory),
        classifier=FakeClassifier(decision),
        page_adapter=page,
    )
    return workflow, page, repo


def row(order_id: str, rating: int, *, available: bool = True) -> FeedbackRow:
    return FeedbackRow(
        order_id=order_id,
        rating=rating,
        order_date="2026/08/04",
        comment="包裹延迟了很久",
        removal_available=available,
    )


def states(session_factory) -> dict[str, str]:
    with session_factory() as session:
        return {
            str(item.order_id): str(item.state)
            for item in session.query(FeedbackReview).all()
        }


# --------------------------------------------------------------------------
# 1. 绝不对好评发起删除请求
# --------------------------------------------------------------------------
async def test_high_star_feedback_is_never_touched(session_factory) -> None:
    rows = [row("702-1", 5), row("702-2", 4), row("702-3", 2)]
    workflow, page, _ = build(session_factory, rows)
    run = make_run()

    plan = await workflow.plan(run, object())
    await workflow.execute(run, object(), plan)

    assert [item[0] for item in page.submitted] == ["702-3"]
    recorded = states(session_factory)
    assert "702-1" not in recorded and "702-2" not in recorded


async def test_a_row_that_became_high_star_is_not_submitted(session_factory) -> None:
    """计划时是 2 星，执行时那一行变成了 5 星——必须放弃，而不是照计划提交。"""

    workflow, page, _ = build(session_factory, [row("702-9", 2)])
    run = make_run()
    plan = await workflow.plan(run, object())

    page._rows = [row("702-9", 5)]
    await workflow.execute(run, object(), plan)

    assert page.submitted == []
    assert states(session_factory)["702-9"] != SUBMITTED


# --------------------------------------------------------------------------
# 2. 同一条绝不提交第二次
# --------------------------------------------------------------------------
async def test_a_feedback_is_never_considered_twice(session_factory) -> None:
    rows = [row("702-5", 1)]
    workflow, page, _ = build(session_factory, rows)

    first = make_run("run-1")
    plan = await workflow.plan(first, object())
    await workflow.execute(first, object(), plan)
    assert len(page.submitted) == 1

    # A later run sees the same order id and must leave it alone.
    workflow2, page2, _ = build(session_factory, [row("702-5", 1)])
    second = make_run("run-2")
    plan2 = await workflow2.plan(second, object())
    await workflow2.execute(second, object(), plan2)

    assert page2.submitted == []
    assert plan2.lines == ()


async def test_the_unique_constraint_is_the_real_barrier(session_factory) -> None:
    """即使绕过内存去重，数据库也必须拒绝第二条记录。"""

    store = FeedbackReviewStore(session_factory)
    first = await store.record_candidate(
        run_id=None, store_id=1, marketplace_code="CA", order_id="702-7",
        rating=1, order_date=None, comment="x", state=PENDING,
    )
    second = await store.record_candidate(
        run_id=None, store_id=1, marketplace_code="CA", order_id="702-7",
        rating=1, order_date=None, comment="x", state=PENDING,
    )
    assert first is not None
    assert second is None


async def test_a_submitted_entry_can_never_be_walked_back(session_factory) -> None:
    store = FeedbackReviewStore(session_factory)
    review_id = await store.record_candidate(
        run_id=None, store_id=1, marketplace_code="CA", order_id="702-8",
        rating=1, order_date=None, comment="x", state=PENDING,
    )
    await store.mark(review_id, SUBMITTED)
    await store.mark(review_id, PENDING)
    assert states(session_factory)["702-8"] == SUBMITTED
    assert await store.set_decision(
        review_id, category="not-listed", reason_code="301", decision_source="human"
    ) is False


# --------------------------------------------------------------------------
# 3. 判不准就不提交
# --------------------------------------------------------------------------
async def test_an_unsure_classifier_queues_for_a_human(session_factory) -> None:
    workflow, page, _ = build(session_factory, [row("702-4", 1)], decision=None)
    run = make_run()

    plan = await workflow.plan(run, object())
    await workflow.execute(run, object(), plan)

    assert page.submitted == []
    assert states(session_factory)["702-4"] == NEEDS_HUMAN
    assert plan.lines == ()


async def test_a_feedback_amazon_already_has_a_request_for_is_terminal(
    session_factory,
) -> None:
    """入口消失＝亚马逊那边已经有一条请求了（卖家确认过的口径）。

    机会已经用掉，所以既不提交、也不该白白问一次模型，更不能落成「待人工」
    让操作员去选一个永远提交不出去的原因。
    """

    page = FakePage([row("702-6", 1, available=False)])
    classifier = FakeClassifier(DECISION)
    workflow = AmazonFeedbackWorkflow(
        repository=RecordingRepository(),
        review_store=FeedbackReviewStore(session_factory),
        classifier=classifier,
        page_adapter=page,
    )
    run = make_run()

    plan = await workflow.plan(run, object())
    await workflow.execute(run, object(), plan)

    assert page.submitted == []
    assert states(session_factory)["702-6"] == ALREADY_REQUESTED
    assert classifier.calls == 0


async def test_dry_run_records_the_decision_without_submitting(session_factory) -> None:
    workflow, page, _ = build(session_factory, [row("702-10", 3)])
    run = make_run(mode=RunMode.DRY_RUN)

    plan = await workflow.plan(run, object())

    assert page.submitted == []
    assert states(session_factory)["702-10"] == PENDING
    assert plan.lines and plan.lines[0].snapshot.can_submit is True


# --------------------------------------------------------------------------
# 4. 爆炸半径
# --------------------------------------------------------------------------
async def test_the_per_run_budget_stops_further_submissions(session_factory) -> None:
    rows = [row(f"702-{i}", 2) for i in range(5)]
    workflow, page, repo = build(session_factory, rows)
    run = make_run(budget=2)

    plan = await workflow.plan(run, object())
    await workflow.execute(run, object(), plan)

    assert len(page.submitted) == 2
    assert any(event[0] == "feedback_budget_reached" for event in repo.events)


async def test_every_attempt_returns_the_table_to_rest(session_factory) -> None:
    workflow, page, _ = build(session_factory, [row("702-11", 2)])
    page.select_result_ok = False
    run = make_run()

    plan = await workflow.plan(run, object())
    await workflow.execute(run, object(), plan)

    assert page.submitted == []
    assert page.closed >= 1


# --------------------------------------------------------------------------
# 5. 接线：不占全局资金锁
# --------------------------------------------------------------------------
def test_the_feedback_workflow_does_not_take_the_funds_lock() -> None:
    definition = build_amazon_feedback_definition()
    assert definition.requires_financial_lock is False
    assert definition.execution_class.value == "standard"
    # DRY_RUN by default: submitting is irreversible and one-shot.
    assert definition.default_mode is RunMode.DRY_RUN


async def test_the_engine_only_locks_funds_for_financial_workflows() -> None:
    from ziniao_automation.workflows.engine import WorkflowEngine
    from ziniao_automation.workflows.amazon_disbursement import (
        build_amazon_disbursement_definition,
    )
    from ziniao_automation.workflows.registry import WorkflowRegistry

    class Sessions:
        def __init__(self) -> None:
            self.used: list[str] = []

        def _ctx(self, name):
            sessions = self

            class Ctx:
                async def __aenter__(self):
                    sessions.used.append(name)
                    return object()

                async def __aexit__(self, *exc):
                    return False

            return Ctx()

        def session(self, selector, store_key=None):
            return self._ctx("session")

        def financial_session(self, selector, store_key=None):
            return self._ctx("financial_session")

    class Stub:
        name = "amazon_disbursement"

    registry = WorkflowRegistry(
        (
            build_amazon_disbursement_definition(Stub()),
            build_amazon_feedback_definition(),
        )
    )
    sessions = Sessions()
    engine = WorkflowEngine(
        registry=registry,
        repository=object(),
        browser_sessions=sessions,
        notifier=object(),
    )

    async with engine._financial_session(make_run()):
        pass
    payout = make_run()
    payout = WorkflowRun(
        id=payout.id,
        workflow="amazon_disbursement",
        mode=payout.mode,
        store=payout.store,
        marketplaces=payout.marketplaces,
    )
    async with engine._financial_session(payout):
        pass

    assert sessions.used == ["session", "financial_session"]


# --------------------------------------------------------------------------
# 6. 跨运行的交接 —— 三条 blocker 的回归钉子
#
# 这三条都是「看起来一切正常，但功能永久失效」的形状：运行是绿色的、页面有数据、
# 日志没有报错，只是那批反馈再也提交不出去，而且唯一约束让它们无法被重新收录。
# --------------------------------------------------------------------------
async def test_a_dry_run_hands_its_decisions_to_the_next_real_run(session_factory) -> None:
    """空跑也会落库。那批「待提交」必须能被后续的 auto 运行接手。

    最初 execute 只看 ``pending_for_run(run_id)``，于是空跑写下的条目对之后的任何
    运行都不可见；而 ``known_order_ids`` 又把它们算作「见过了」，唯一约束再挡住重新
    收录——用户按习惯先空跑一次，就把整店的中差评永久废掉了，且运行显示已完成。
    """

    rows = [row("702-20", 2), row("702-21", 1)]
    workflow, page, _ = build(session_factory, rows)

    dry = make_run("run-1", mode=RunMode.DRY_RUN)
    await workflow.plan(dry, object())
    assert page.submitted == []
    assert set(states(session_factory).values()) == {PENDING}

    # A different run, in auto mode, must pick them up.
    workflow2, page2, _ = build(session_factory, [row("702-20", 2), row("702-21", 1)])
    live = make_run("run-2", mode=RunMode.AUTO)
    plan = await workflow2.plan(live, object())
    assert plan.lines, "空跑遗留的待提交条目必须让本次运行有事可做"
    await workflow2.execute(live, object(), plan)

    assert sorted(item[0] for item in page2.submitted) == ["702-20", "702-21"]
    assert set(states(session_factory).values()) == {SUBMITTED}


async def test_a_reason_chosen_by_a_human_is_submitted_by_the_next_run(
    session_factory,
) -> None:
    """人工在反馈处理台选完原因后，下次运行必须真的提交——页面就是这么承诺的。"""

    workflow, page, _ = build(session_factory, [row("702-30", 1)], decision=None)
    first = make_run("run-1")
    plan = await workflow.plan(first, object())
    await workflow.execute(first, object(), plan)
    assert states(session_factory)["702-30"] == NEEDS_HUMAN
    assert page.submitted == []

    # Exactly what POST /api/feedback-reviews/{id}/decision writes.
    with session_factory() as session:
        review = session.query(FeedbackReview).filter_by(order_id="702-30").one()
        review.category = "delivery-related-feedback"
        review.reason_code = "402"
        review.decision_source = "human"
        review.state = PENDING
        session.commit()

    workflow2, page2, _ = build(session_factory, [row("702-30", 1)])
    second = make_run("run-2")
    plan2 = await workflow2.plan(second, object())
    await workflow2.execute(second, object(), plan2)

    assert [item[0] for item in page2.submitted] == ["702-30"]
    assert states(session_factory)["702-30"] == SUBMITTED


async def test_an_unreadable_action_menu_is_never_written_down(session_factory) -> None:
    """读不到操作菜单 ≠ 亚马逊已经有请求了。

    列表是异步渲染的，菜单的 shadowRoot 可能还没挂上。把这种情况记成
    ALREADY_REQUESTED 会用一个**假事实**永久废掉整店的中差评——那是终态，
    唯一约束又挡住重新收录。宁可这次不记，留给下一次。
    """

    unreadable = FeedbackRow(
        order_id="702-40", rating=1, order_date="2026/08/04",
        comment="包裹延迟", removal_available=False, menu_readable=False,
    )
    workflow, page, repo = build(session_factory, [unreadable])
    run = make_run()

    await workflow.plan(run, object())

    assert states(session_factory) == {}, "读不出来的行绝不能落库"
    assert any(event[0] == "feedback_menu_unreadable" for event in repo.events)

    # Next run reads it properly and records the truth.
    readable = FeedbackRow(
        order_id="702-40", rating=1, order_date="2026/08/04",
        comment="包裹延迟", removal_available=False, menu_readable=True,
    )
    workflow2, _, _ = build(session_factory, [readable])
    await workflow2.plan(make_run("run-2"), object())
    assert states(session_factory)["702-40"] == ALREADY_REQUESTED


async def test_a_finished_site_is_not_reported_as_failed(session_factory) -> None:
    """站点收尾用的必须是真实存在的枚举成员。

    原来写的是 ``SiteStatus.SUCCEEDED``——这个成员不存在，AttributeError 在所有请求
    都已经提交出去之后才抛，被 execute 的宽 except 吞成「该站点失败」，操作员收到
    的是一张红色的失败卡片。
    """

    workflow, page, repo = build(session_factory, [row("702-50", 2)])
    run = make_run()

    plan = await workflow.plan(run, object())
    await workflow.execute(run, object(), plan)

    assert len(page.submitted) == 1
    assert repo.site_status.get("CA") != "FAILED"
    assert not any(event[0] == "site_execution_failed" for event in repo.events)


# --------------------------------------------------------------------------
# 7. 实战暴露的两件事（2026-08-29）
# --------------------------------------------------------------------------
async def test_a_marketplace_the_store_does_not_sell_on_is_skipped(
    session_factory,
) -> None:
    """店铺没开通的站点会被 Seller Central 跳到账户选择页。

    那上面没有 feedback-list，原来会等满 45 秒抛契约错误，**整个 run 判失败**——
    廖莉-7.2 和卢光桂-卢元CA 就是这么红的。这是店铺的事实，不是故障：
    该站点跳过，其余站点照跑。
    """

    workflow, page, repo = build(session_factory, [row("702-60", 1)])
    page.unavailable = True
    run = make_run()

    plan = await workflow.plan(run, object())

    assert plan.lines == ()
    assert states(session_factory) == {}
    assert any(event[0] == "feedback_site_unavailable" for event in repo.events)
    assert repo.site_status.get("CA") == "SKIPPED"


async def test_an_empty_site_is_not_reported_as_a_timeout(session_factory) -> None:
    """没有商品的店铺读到 0 行是正常的，别写成「等待超时」。"""

    workflow, _, repo = build(session_factory, [])
    await workflow.plan(make_run(), object())

    empty = [event for event in repo.events if event[0] == "feedback_list_empty"]
    assert empty and empty[0][2]["reason_code"] == "no_rows"


async def test_reading_more_pages_than_the_cap_is_reported(session_factory) -> None:
    """列表分页了却只读了前几页，绝不能当成完整扫描静静过去。"""

    workflow, page, repo = build(session_factory, [row("702-70", 1)])
    page.truncated = True

    await workflow.plan(make_run(), object())

    assert any(event[0] == "feedback_list_truncated" for event in repo.events)


# --------------------------------------------------------------------------
# 8. 亚马逊自己剔除的，和「此前已请求」不是一回事
# --------------------------------------------------------------------------
async def plan_of(workflow, run):
    return await workflow.plan(run, object())


def struck(order_id: str, rating: int) -> FeedbackRow:
    """页面上正文被划掉、并附了亚马逊说明的那种行。"""

    return FeedbackRow(
        order_id=order_id, rating=rating, order_date="2026/08/04",
        comment="包裹一直没到", removal_available=False,
        amazon_removed=True,
    )


async def test_amazon_removing_it_itself_is_its_own_state(session_factory) -> None:
    """亚马逊对自己配送的订单会主动剔除反馈——那不是「有人请求过」。

    两者都是终态、都不再提交，但分开记才看得出自动化到底帮了多少、
    以及哪些是人工（或客服）真的发过请求的。
    """

    workflow, page, _ = build(session_factory, [struck("702-80", 1)])
    run = make_run()

    plan = await workflow.plan(run, object())
    await workflow.execute(run, object(), plan)

    assert page.submitted == []
    assert states(session_factory)["702-80"] == "AMAZON_REMOVED"


async def test_a_removed_entry_never_costs_a_model_call(session_factory) -> None:
    page = FakePage([struck("702-81", 1)])
    classifier = FakeClassifier(DECISION)
    workflow = AmazonFeedbackWorkflow(
        repository=RecordingRepository(),
        review_store=FeedbackReviewStore(session_factory),
        classifier=classifier,
        page_adapter=page,
    )

    await workflow.plan(make_run(), object())

    assert classifier.calls == 0
    assert states(session_factory)["702-81"] == "AMAZON_REMOVED"


async def test_amazon_removal_wins_over_a_missing_action(session_factory) -> None:
    """两个信号同时出现时，以亚马逊自己的说明为准——它更具体。"""

    workflow, _, _ = build(session_factory, [struck("702-82", 2)])
    await workflow.plan(make_run(), object())

    assert states(session_factory)["702-82"] == "AMAZON_REMOVED"


# --------------------------------------------------------------------------
# 9. 每个站点都要出现在报告里
# --------------------------------------------------------------------------
async def test_every_marketplace_gets_a_site_row(session_factory) -> None:
    """飞书卡片只列出「出问题的站点」，读得好好的一个都不显示。

    根因：site_runs 行只在两处产生——计划里有可提交条目，或我在跳过/失败路径上
    调了 set_site_status。正常读完、没事可做的站点两处都不沾，于是三站全正常的
    店铺整张卡上一个站点都没有，而只有一个站点没开通的店铺看起来「只跑了那一个」。
    """

    workflow, _, repo = build(session_factory, [row("702-90", 1)])
    await workflow.plan(make_run(), object())

    assert "CA" in repo.site_status, "读取正常的站点也必须有站点行"
    assert repo.site_status["CA"] != "PREFLIGHT", "必须落到终态，不能停在读取中"


async def test_an_unavailable_marketplace_still_gets_a_row(session_factory) -> None:
    workflow, page, repo = build(session_factory, [row("702-91", 1)])
    page.unavailable = True

    await workflow.plan(make_run(), object())

    assert repo.site_status.get("CA") == "SKIPPED"


async def test_an_empty_marketplace_still_gets_a_row(session_factory) -> None:
    workflow, _, repo = build(session_factory, [])
    await workflow.plan(make_run(), object())

    assert repo.site_status.get("CA") == "SKIPPED"


async def test_the_site_row_exists_before_events_are_logged(session_factory) -> None:
    """事件是靠 marketplace_code 去查 site_runs 行来挂载的。

    行不存在时事件就成了孤儿，卡片上那个站点便没有任何原因说明——这正是
    「未开通」的站点在卡片上只显示一个光秃秃「已跳过」的原因。
    """

    order: list[str] = []

    class OrderedRepository(RecordingRepository):
        async def set_site_status(self, run_id, marketplace_code, status, *, error=None):
            order.append(f"site:{marketplace_code}")
            await super().set_site_status(run_id, marketplace_code, status, error=error)

        async def append_event(self, run_id, event_type, message, *, marketplace_code=None, details=None):
            order.append(f"event:{event_type}")
            await super().append_event(
                run_id, event_type, message,
                marketplace_code=marketplace_code, details=details,
            )

    page = FakePage([row("702-92", 1)])
    page.unavailable = True
    workflow = AmazonFeedbackWorkflow(
        repository=OrderedRepository(),
        review_store=FeedbackReviewStore(session_factory),
        classifier=FakeClassifier(DECISION),
        page_adapter=page,
    )
    await workflow.plan(make_run(), object())

    assert order[0].startswith("site:"), f"站点行必须先建立，实际顺序：{order}"


# --------------------------------------------------------------------------
# 10. 「待人工」不是死胡同
# --------------------------------------------------------------------------
async def test_an_undecided_entry_is_judged_again_next_run(session_factory) -> None:
    """旧提示词判不准的条目，必须能被新提示词重新判。

    实际发生过：秦登友 CA 那条 3 星在第一次运行时被保守版提示词判成「待人工」，
    之后提示词改果断了，它却再也不会被重判——known_order_ids 不看状态所以不再
    收录，pending_for_store 只取 PENDING 所以不会提交。卖家在页面上明明看到
    「请求审核」是可点的，飞书却一直说没有可提交的。
    """

    # 第一次：模型判不准
    workflow, _, _ = build(session_factory, [row("702-100", 3)], decision=None)
    await workflow.plan(make_run("run-1"), object())
    assert states(session_factory)["702-100"] == NEEDS_HUMAN

    # 第二次：模型给得出原因了
    workflow2, page2, repo2 = build(session_factory, [row("702-100", 3)])
    second = make_run("run-2")
    plan = await workflow2.plan(second, object())

    assert states(session_factory)["702-100"] == PENDING
    assert plan.lines, "重判出结果后本次运行就该有事可做"
    assert any(event[0] == "feedback_rejudged" for event in repo2.events)

    await workflow2.execute(second, object(), plan)
    assert [item[0] for item in page2.submitted] == ["702-100"]


async def test_rejudging_skips_entries_amazon_has_since_handled(
    session_factory,
) -> None:
    """页面上已经没有入口了就别再花模型调用——它已经提交不了了。"""

    workflow, _, _ = build(session_factory, [row("702-101", 3)], decision=None)
    await workflow.plan(make_run("run-1"), object())

    page = FakePage([row("702-101", 3, available=False)])
    classifier = FakeClassifier(DECISION)
    workflow2 = AmazonFeedbackWorkflow(
        repository=RecordingRepository(),
        review_store=FeedbackReviewStore(session_factory),
        classifier=classifier,
        page_adapter=page,
    )
    await workflow2.plan(make_run("run-2"), object())

    assert classifier.calls == 0
    assert states(session_factory)["702-101"] == NEEDS_HUMAN


async def test_a_submitted_entry_is_never_rejudged(session_factory) -> None:
    """重判只针对「待人工」；终态绝不能被重新排上。"""

    workflow, page, _ = build(session_factory, [row("702-102", 2)])
    first = make_run("run-1")
    plan = await workflow.plan(first, object())
    await workflow.execute(first, object(), plan)
    assert states(session_factory)["702-102"] == SUBMITTED

    workflow2, page2, _ = build(session_factory, [row("702-102", 2)])
    second = make_run("run-2")
    plan2 = await workflow2.plan(second, object())
    await workflow2.execute(second, object(), plan2)

    assert page2.submitted == []
    assert states(session_factory)["702-102"] == SUBMITTED


async def test_confirming_a_submission_reloads_the_list(session_factory) -> None:
    """确认必须重新加载列表，不能反复读同一个 DOM。

    Angular 的列表不会自己重取，所以点完提交后再读到的还是提交前的内容——
    第一次真实提交就是这么把一条**确实送达**的请求记成了「未确认」。
    """

    workflow, page, _ = build(session_factory, [row("702-110", 2)])
    run = make_run()

    plan = await workflow.plan(run, object())
    await workflow.execute(run, object(), plan)

    assert page.reloads >= 1, "确认阶段必须重新打开列表"
    assert states(session_factory)["702-110"] == SUBMITTED
