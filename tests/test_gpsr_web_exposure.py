"""GPSR 在 web 层的暴露：排期页选得到，运行详情页不渲染成提现。

⚠️ 两条都**发真实请求**来断言。本项目的 FastAPI 存的是 ``_IncludedRouter`` 占位符，
翻 ``app.routes`` 看着是空的其实是通的；同理，光看注册表对象也证明不了页面拿得到。

⚠️ 这里必须**自己把真注册表传进 create_app**：不传的话 web.py 只兜底注册提现流程
（``workflow_registry or WorkflowRegistry((build_amazon_disbursement_definition(),))``），
于是页面测试里 ``workflow_meta`` 对任何非提现流程恒为 None——run_detail 的分支判据
之所以用字面 key 而不是 ``workflow_meta.requires_financial_lock``，就是这个原因。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ziniao_automation.config import Settings
from ziniao_automation.models import Run, SiteRun, Store, StoreMarketplace
from ziniao_automation.web import create_app
from ziniao_automation.workflows.amazon_disbursement import (
    build_amazon_disbursement_definition,
)
from ziniao_automation.workflows.amazon_feedback import (
    build_amazon_feedback_definition,
)
from ziniao_automation.workflows.amazon_gpsr import build_amazon_gpsr_definition
from ziniao_automation.workflows.registry import WorkflowRegistry

from tests.test_db_api import FakeAutomation, bootstrap


GPSR = "amazon_gpsr_attestation"


@pytest.fixture()
def client_with_every_workflow(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        testing=True,
    )
    registry = WorkflowRegistry(
        (
            build_amazon_disbursement_definition(),
            build_amazon_feedback_definition(),
            build_amazon_gpsr_definition(),
        )
    )
    app = create_app(
        settings, automation_service=FakeAutomation(), workflow_registry=registry
    )
    with TestClient(app) as client:
        yield app, client, settings


def _workflow_items(client: TestClient) -> dict[str, dict]:
    response = client.get("/api/workflows")
    assert response.status_code == 200, response.text
    payload = response.json()
    items = payload if isinstance(payload, list) else payload.get("items", [])
    return {item["key"]: item for item in items}


def test_the_schedules_page_can_offer_the_gpsr_workflow(client_with_every_workflow):
    """排期页的流程下拉是按 ``/api/workflows`` 渲染的。"""

    _, client, _ = client_with_every_workflow
    bootstrap(client)

    by_key = _workflow_items(client)
    assert GPSR in by_key, f"排期页选不到这条流程；拿到的是 {sorted(by_key)}"
    # 反向：另外两条也还在，说明这不是「只要有东西就行」。
    assert {"amazon_disbursement", "amazon_feedback_removal"} <= set(by_key)

    gpsr = by_key[GPSR]
    # AUTO 于 2026-09-17 放开；但默认仍是 dry_run，建排期时手滑选不到自动提交。
    assert set(gpsr["supported_modes"]) == {"dry_run", "auto"}
    assert gpsr["default_mode"] == "dry_run"
    # APPROVAL 不能出现：它的人工界面是资金审核台，整个围绕金额建的。
    assert "approval" not in gpsr["supported_modes"]
    schema = gpsr["config_schema"]["properties"]["marketplace_codes"]["items"]
    assert (schema.get("enum") or [schema["const"]]) == ["DE"]


def test_a_gpsr_run_detail_is_not_dressed_as_a_payout(client_with_every_workflow):
    """一分钱都不碰的任务不能渲染成「EUR 0.00 可提现」。

    ⚠️ 必须真的种一行 ``site_run``：站点列表为空时逐行模板代码根本不会执行，
    下面的否定断言就成了空转。
    """

    app, client, _ = client_with_every_workflow
    bootstrap(client)

    session_factory = app.state.sessions
    with session_factory() as session:
        store = Store(
            name="秦登友",
            selector_type="oauth",
            selector_value="selector",
            browser_oauth="selector",
            expected_seller_id="dengyou-us",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        market = StoreMarketplace(
            store_id=store.id,
            code="DE",
            domain="sellercentral.amazon.de",
            currency="EUR",
            enabled=True,
        )
        session.add(market)
        session.flush()
        session.add(
            Run(
                id="gpsr-detail",
                store_id=store.id,
                workflow=GPSR,
                mode="dry_run",
                status="SUCCEEDED",
                trigger="manual",
            )
        )
        session.flush()
        session.add(
            SiteRun(
                id="gpsr-detail-de",
                run_id="gpsr-detail",
                marketplace_id=market.id,
                marketplace_code="DE",
                status="SKIPPED",
                currency="EUR",
            )
        )
        session.commit()

    page = client.get("/runs/gpsr-detail")

    assert page.status_code == 200, page.text
    # 前提：逐行模板代码确实被执行了。
    assert "DE 站点" in page.text
    assert "站点与合规扫描" in page.text
    # 金额卡的标签一个都不能出现。
    assert "可提现 PAYABLE" not in page.text
    assert "延迟资金" not in page.text
    assert "站点与原币金额" not in page.text


def test_a_payout_run_detail_still_shows_its_money_card(client_with_every_workflow):
    """反向验证：同样的种子数据换成提现流程，金额卡必须回来。

    否则一个把金额卡整个删掉的改动会让上面那条否定断言全绿。
    """

    app, client, _ = client_with_every_workflow
    bootstrap(client)

    with app.state.sessions() as session:
        store = Store(
            name="秦登友",
            selector_type="oauth",
            selector_value="s2",
            browser_oauth="s2",
            expected_seller_id="dengyou-us",
            identity_confirmed=True,
            enabled=True,
        )
        session.add(store)
        session.flush()
        market = StoreMarketplace(
            store_id=store.id,
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
            enabled=True,
        )
        session.add(market)
        session.flush()
        session.add(
            Run(
                id="payout-detail",
                store_id=store.id,
                workflow="amazon_disbursement",
                mode="auto",
                status="SUCCEEDED",
                trigger="manual",
            )
        )
        session.flush()
        session.add(
            SiteRun(
                id="payout-detail-ca",
                run_id="payout-detail",
                marketplace_id=market.id,
                marketplace_code="CA",
                status="CONFIRMED",
                currency="CAD",
            )
        )
        session.commit()

    page = client.get("/runs/payout-detail")
    assert page.status_code == 200
    assert "站点与原币金额" in page.text
    assert "可提现 PAYABLE" in page.text
