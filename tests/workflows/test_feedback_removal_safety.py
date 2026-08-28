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
from ziniao_automation.workflows.amazon_feedback.page import FeedbackRow
from ziniao_automation.workflows.amazon_feedback.review_store import (
    NEEDS_HUMAN,
    PENDING,
    SKIPPED,
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

    async def open_list(self, page: Any, domain: str) -> bool:
        # True = rows arrived.  The workflow must tell an empty list apart
        # from one that never loaded.
        return bool(self._rows)

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


class FakeClassifier:
    def __init__(self, decision: ReasonDecision | None) -> None:
        self.decision = decision
        self.calls = 0

    async def classify(self, *, comment: str, rating: int, order_date: str | None = None):
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


async def test_amazon_not_offering_the_action_is_a_skip_not_a_submit(
    session_factory,
) -> None:
    """1 星但亚马逊没给「请求审核」入口——跳过，且不该白白问一次模型。"""

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
    assert states(session_factory)["702-6"] == SKIPPED
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
