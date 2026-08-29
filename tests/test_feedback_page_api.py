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


# --------------------------------------------------------------------------
# 星级和留言要能看到——但买家的联系方式不该发到飞书群里
# --------------------------------------------------------------------------
def _seed_reviewed_run(app, *, mode: str, state: str, comment: str) -> str:
    from ziniao_automation.models import Run

    with app.state.sessions() as session:
        if not session.query(FeedbackReview).count():
            StoreRepository(session).create(
                name="测试店铺", selector_type="oauth", selector_value="oauth-1"
            )
            session.flush()
        run_id = f"run-{mode}-{state}"
        session.add(
            Run(id=run_id, store_id=1, workflow="amazon_feedback_removal",
                mode=mode, status="SUCCEEDED")
        )
        session.flush()
        session.add(
            FeedbackReview(
                run_id=run_id, store_id=1, marketplace_code="CA",
                order_id="702-2037224-8066659", rating=2, comment=comment,
                category="delivery-related-feedback", reason_code="402",
                state=state,
            )
        )
        session.commit()
        return run_id


def _card_body(app, run_id: str) -> str:
    from ziniao_automation.notifications.database import DatabaseNoticeBuilder
    from ziniao_automation.notifications.dto import NotificationKind
    from ziniao_automation.notifications.feishu import _message

    notice = DatabaseNoticeBuilder(app.state.sessions).build(
        run_id, NotificationKind.RUN_COMPLETED
    )
    assert notice is not None
    return _message(notice)


def test_the_card_shows_the_stars_and_the_buyers_words(client) -> None:
    app, _, _ = client
    run_id = _seed_reviewed_run(
        app, mode="auto", state="SUBMITTED", comment="包裹寄丢了等了三周"
    )

    body = _card_body(app, run_id)

    assert "本次已提交" in body
    assert "★★☆☆☆" in body
    assert "包裹寄丢了等了三周" in body
    assert "订单由亚马逊配送" in body
    # The full order id, not the 8-character run-id form.
    assert "702-2037224-8066659" in body


def test_a_dry_run_card_says_these_have_not_been_sent(client) -> None:
    app, _, _ = client
    run_id = _seed_reviewed_run(
        app, mode="dry_run", state="PENDING", comment="东西不对"
    )

    body = _card_body(app, run_id)

    assert "本次将提交（尚未发出）" in body
    assert "东西不对" in body


def test_a_buyers_phone_number_never_reaches_feishu(client) -> None:
    """亚马逊的删除理由 103 就是「反馈包含了个人识别信息」——含联系方式的留言
    不是边缘情况，正是这个流程要处理的一类。完整原文留在本地页面上。"""

    app, _, _ = client
    run_id = _seed_reviewed_run(
        app, mode="auto", state="SUBMITTED",
        comment="Call me at 555-0142 or bob@example.com",
    )

    body = _card_body(app, run_id)

    assert "555-0142" not in body
    assert "bob@example.com" not in body
    assert "号码已隐藏" in body
    assert "邮箱已隐藏" in body


def test_a_date_in_a_comment_is_not_mistaken_for_a_phone_number(client) -> None:
    app, _, _ = client
    run_id = _seed_reviewed_run(
        app, mode="auto", state="SUBMITTED", comment="包裹 2026-08-29 才到"
    )

    body = _card_body(app, run_id)

    # markdown 转义会把连字符写成 ``\-``；真正要钉的是它没有被当成号码抹掉。
    assert "号码已隐藏" not in body
    assert "2026" in body and "08" in body and "29" in body


def test_the_run_detail_lists_the_reviews_that_run_handled(client) -> None:
    app, test_client, _ = client
    run_id = _seed_reviewed_run(
        app, mode="auto", state="SUBMITTED", comment="迟到了很久非常失望"
    )

    page = test_client.get(f"/runs/{run_id}")

    assert page.status_code == 200
    assert "本次处理的评论" in page.text
    # The local page is the audit surface, so it shows the untouched text.
    assert "迟到了很久非常失望" in page.text
    assert "702-2037224-8066659" in page.text


def test_a_payout_run_detail_has_no_review_table(client) -> None:
    app, test_client, _ = client
    run_id = _seed_run(app, "amazon_disbursement")

    assert "本次处理的评论" not in test_client.get(f"/runs/{run_id}").text


def test_the_page_can_be_filtered_to_one_site(client) -> None:
    """一次运行覆盖 CA/UK/AU，站点不能筛就是一锅粥。"""

    app, test_client, _ = client
    with app.state.sessions() as session:
        StoreRepository(session).create(
            name="测试店铺", selector_type="oauth", selector_value="oauth-1"
        )
        session.flush()
        for code, order in (("CA", "702-CA"), ("UK", "702-UK")):
            session.add(
                FeedbackReview(
                    store_id=1, marketplace_code=code, order_id=order,
                    rating=1, comment=f"{code} 的留言", state="ALREADY_REQUESTED",
                )
            )
        session.commit()

    everything = test_client.get("/feedback").text
    assert "CA 的留言" in everything and "UK 的留言" in everything

    only_uk = test_client.get("/feedback?site=UK").text
    assert "UK 的留言" in only_uk
    assert "CA 的留言" not in only_uk
    # The per-site breakdown counts the whole ledger, not the filtered view.
    assert "各店各站点已收录的条数" in only_uk
