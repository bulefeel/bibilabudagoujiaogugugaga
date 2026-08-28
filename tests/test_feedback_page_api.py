"""反馈处理台的 HTTP 面。

路由是否真的挂上去了，只能靠**发真实请求**来断言。本项目的 FastAPI 里
``app.routes`` 存的是 ``_IncludedRouter`` 占位符，看着空其实是通的，反过来也一样，
所以这里一律断言状态码而不是去数路由表。

人工改判那个接口有一条硬规则：**已提交的条目不能再改、更不能重排**。
亚马逊对一条反馈只受理一次，放行第二次就是把用户仅有的一次机会又花掉一遍。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ziniao_automation.config import Settings
from ziniao_automation.models import FeedbackReview
from ziniao_automation.repositories import StoreRepository
from ziniao_automation.web import create_app


class FakeAutomation:
    async def sync_ziniao(self):
        return {"created": 0, "updated": 0}


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'feedback-api.db').as_posix()}",
        testing=True,
    )
    app = create_app(settings, automation_service=FakeAutomation())
    with TestClient(app) as test_client:
        response = test_client.post(
            "/auth/bootstrap",
            json={
                "username": "operator",
                "password": "Local-Ledger-2026!",
                "confirm_password": "Local-Ledger-2026!",
            },
        )
        assert response.status_code == 200, response.text
        yield app, test_client, test_client.cookies["ziniao_csrf"]


def seed(app, *, state: str, category: str | None = None, reason: str | None = None) -> str:
    with app.state.sessions() as session:
        if not session.query(FeedbackReview).count():
            StoreRepository(session).create(
                name="测试店铺", selector_type="oauth", selector_value="oauth-1"
            )
            session.flush()
        review = FeedbackReview(
            store_id=1,
            marketplace_code="CA",
            order_id=f"702-{state}",
            rating=1,
            comment="包裹一直没到",
            state=state,
            category=category,
            reason_code=reason,
        )
        session.add(review)
        session.commit()
        return str(review.id)


def test_the_feedback_page_is_actually_served(client) -> None:
    app, test_client, _ = client
    seed(app, state="NEEDS_HUMAN")

    page = test_client.get("/feedback")

    assert page.status_code == 200
    assert "反馈处理台" in page.text
    # The reason picker must offer Amazon's own wording, not invented labels.
    assert "订单由亚马逊配送" in page.text
    assert "反馈包含了个人识别信息" in page.text
    # And it must say plainly that nothing here is ever retried.
    assert "永不重试" in page.text


def test_it_is_reachable_from_the_sidebar(client) -> None:
    _, test_client, _ = client
    assert 'href="/feedback"' in test_client.get("/runs").text


def test_an_operator_can_supply_the_reason_the_classifier_declined(client) -> None:
    app, test_client, csrf = client
    review_id = seed(app, state="NEEDS_HUMAN")

    response = test_client.post(
        f"/api/feedback-reviews/{review_id}/decision",
        json={"category": "delivery-related-feedback", "reason_code": "402"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.text
    # Recorded only — submission still belongs to the next scheduled run.
    assert response.json()["status"] == "PENDING"
    with app.state.sessions() as session:
        row = session.get(FeedbackReview, review_id)
        assert row.state == "PENDING"
        assert row.decision_source == "human"


def test_a_reason_amazon_does_not_offer_is_refused(client) -> None:
    app, test_client, csrf = client
    review_id = seed(app, state="NEEDS_HUMAN")

    response = test_client.post(
        f"/api/feedback-reviews/{review_id}/decision",
        # 201 looks plausible next to 202/203/204 but Amazon has no such code.
        json={"category": "product-feedback", "reason_code": "201"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("state", ["SUBMITTED", "UNCERTAIN", "ALREADY_REQUESTED"])
def test_an_already_submitted_feedback_can_never_be_requeued(client, state) -> None:
    """这是整个页面上最重要的一条：提交过的绝不能被重新排上。

    ``ALREADY_REQUESTED`` 同样在内：入口消失意味着亚马逊那边已经有一条请求，
    给它选原因只会排出一次注定提交不掉的重试。
    """

    app, test_client, csrf = client
    review_id = seed(app, state=state, category="not-listed", reason="301")

    response = test_client.post(
        f"/api/feedback-reviews/{review_id}/decision",
        json={"category": "delivery-related-feedback", "reason_code": "402"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    with app.state.sessions() as session:
        row = session.get(FeedbackReview, review_id)
        assert row.state == state
        assert row.reason_code == "301"


def test_the_ai_key_card_explains_where_the_comment_goes(client) -> None:
    """买家留言会离开这台机器，这件事必须写在配置它的地方。"""

    _, test_client, _ = client
    page = test_client.get("/diagnostics")

    assert page.status_code == 200
    assert 'data-settings-form="ai"' in page.text
    assert "唯一的对外网络请求" in page.text


# --------------------------------------------------------------------------
# 不能用提现的语言描述一个不碰钱的流程
# --------------------------------------------------------------------------
def _seed_run(app, workflow: str, mode: str = "auto") -> str:
    from ziniao_automation.models import Run

    with app.state.sessions() as session:
        if not session.query(FeedbackReview).count():
            StoreRepository(session).create(
                name="测试店铺", selector_type="oauth", selector_value="oauth-1"
            )
            session.flush()
        run = Run(
            id=f"run-{workflow}-{mode}",
            store_id=1,
            workflow=workflow,
            mode=mode,
            status="SUCCEEDED",
        )
        session.add(run)
        session.commit()
        return str(run.id)


def test_a_feedback_run_detail_is_not_dressed_as_a_payout(client) -> None:
    app, test_client, _ = client
    run_id = _seed_run(app, "amazon_feedback_removal")

    page = test_client.get(f"/runs/{run_id}")

    assert page.status_code == 200
    assert "站点与反馈处理" in page.text
    # 「可提现 PAYABLE 0.00」 for a workflow that never reads a balance reads as
    # "this store cannot withdraw anything".
    assert "可提现 PAYABLE" not in page.text
    assert "延迟资金" not in page.text
    assert 'href="/feedback"' in page.text


def test_the_feishu_card_for_a_feedback_run_never_mentions_payouts(client) -> None:
    from ziniao_automation.notifications.database import DatabaseNoticeBuilder
    from ziniao_automation.notifications.dto import NotificationKind

    app, _, _ = client
    run_id = _seed_run(app, "amazon_feedback_removal")

    notice = DatabaseNoticeBuilder(app.state.sessions).build(
        run_id, NotificationKind.RUN_COMPLETED
    )

    assert notice is not None
    assert "反馈" in notice.title
    assert "请求审核" in notice.summary
    # 整段正文都要查：标题和摘要来自 database.py，而「状态」「下一步」两行是
    # feishu.py 自己按 kind 填的默认提现文案，只测前两者会漏掉它们。
    from ziniao_automation.notifications.feishu import _message

    body = _message(notice)
    assert "提现" not in body, body
    assert "反馈处理台" in body


def test_a_payout_run_still_gets_the_payout_card(client) -> None:
    """反馈的改动不能把提现的文案改掉——它才是这些措辞的正主。"""

    from ziniao_automation.notifications.database import DatabaseNoticeBuilder
    from ziniao_automation.notifications.dto import NotificationKind

    app, _, _ = client
    run_id = _seed_run(app, "amazon_disbursement")

    notice = DatabaseNoticeBuilder(app.state.sessions).build(
        run_id, NotificationKind.RUN_COMPLETED
    )

    from ziniao_automation.notifications.feishu import _message

    assert notice is not None
    assert "紫鸟提现" in notice.title
    assert "提现" in notice.summary
    assert "提现结果已确认" in _message(notice)
