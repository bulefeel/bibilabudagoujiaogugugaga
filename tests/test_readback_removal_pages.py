"""删掉资金回读之后，页面**真的**说了什么、按钮**真的**放不放行。

⚠️ 这里原来是一组源码字符串断言（``'"ARMED", "UNCERTAIN"' in repositories.py``、
``"上次回读" in run_detail.html``）。那种测试钉的是实现字面：换个等价写法就红，
写错了意思反而照样绿 —— 2026-09-23 自审里它钉住的正是一句错话（把人工结案顺手写的
``last_reconciled_at`` 显示成「上次回读…仍未查到」）。一律改成发真实请求、读渲染结果。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from ziniao_automation.config import Settings
from ziniao_automation.models import (
    OperationGuard,
    Run,
    SiteRun,
    Store,
    StoreMarketplace,
)
from ziniao_automation.web import create_app
from ziniao_automation.workflows.types import utc_now


class _FakeAutomation:
    async def sync_ziniao(self):
        return {"created": 0, "updated": 0}


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'pages.db').as_posix()}",
        testing=True,
    )
    app = create_app(settings, automation_service=_FakeAutomation())
    with TestClient(app) as test_client:
        response = test_client.get("/")
        assert response.status_code == 200, response.text
        yield app, test_client, {"X-CSRF-Token": test_client.cookies["ziniao_csrf"]}


def _seed(app, guards: list[dict], *, run_status: str = "SUCCEEDED") -> str:
    """一家店、一个 CA 站点、一条运行；每个 dict 造一条资金记录。"""

    with app.state.sessions() as session:
        store = session.query(Store).first()
        if store is None:
            store = Store(
                name="页面测试店",
                selector_type="oauth",
                selector_value="oauth-pages",
                browser_oauth="oauth-pages",
                expected_seller_id="SELLER-PAGES",
                identity_confirmed=True,
                enabled=True,
            )
            session.add(store)
            session.flush()
            session.add(
                StoreMarketplace(
                    store_id=store.id,
                    code="CA",
                    domain="sellercentral.amazon.ca",
                    currency="CAD",
                    enabled=True,
                )
            )
            session.flush()
        marketplace = session.query(StoreMarketplace).first()
        run = Run(
            store_id=store.id,
            workflow="amazon_disbursement",
            mode="auto",
            trigger="manual",
            status=run_status,
            requested_by="admin",
        )
        session.add(run)
        session.flush()
        # 一条运行在一个站点上只有一行 SiteRun；多条资金记录挂在同一行上
        # （不同结算周期），足够让页面渲染多行守卫。
        site = SiteRun(
            run_id=run.id,
            marketplace_id=marketplace.id,
            marketplace_code="CA",
            status="SUBMITTED",
            currency="CAD",
            payable_amount=10,
            delayed_amount=0,
            settlement_key=f"cycle-{run.id}",
            plan_hash="a" * 64,
            snapshot_hash="b" * 64,
        )
        session.add(site)
        session.flush()
        for index, spec in enumerate(guards):
            session.add(
                OperationGuard(
                    guard_key=spec["key"],
                    run_id=run.id,
                    site_run_id=site.id,
                    store_id=store.id,
                    workflow="amazon_disbursement",
                    marketplace_code="CA",
                    settlement_key=f"cycle-{run.id}-{index}",
                    state=spec["state"],
                    amount=10,
                    currency="CAD",
                    plan_hash="a" * 64,
                    snapshot_hash="b" * 64,
                    submitted_at=spec.get("submitted_at"),
                    last_reconciled_at=spec.get("last_reconciled_at"),
                    metadata_json=spec.get("metadata", {}),
                )
            )
        session.commit()
        return str(run.id)


def _guard_rows(html: str) -> list[str]:
    return re.findall(r'<div class="guard-row">(.*?)</div>', html, flags=re.S)


# ---------------------------------------------------------------------------
# 运行详情：守卫行
# ---------------------------------------------------------------------------


def test_guard_rows_only_mention_a_read_back_for_legacy_uncertain_records(client) -> None:
    """「回读」只能出现在删除之前留下的 UNCERTAIN 历史记录上，而且要说明是历史。

    人工结案会顺手写 ``last_reconciled_at``（旧的 ``transition_guard``），页面曾把它
    显示成「上次回读 X，仍未查到」—— 一件从没发生过的事。
    """

    app, test_client, _ = client
    now = utc_now()
    run_id = _seed(
        app,
        [
            {"key": "g-submitted", "state": "SUBMITTED", "submitted_at": now},
            {
                "key": "g-closed",
                "state": "CONFIRMED",
                "submitted_at": now,
                "last_reconciled_at": now,
            },
            {
                "key": "g-legacy",
                "state": "UNCERTAIN",
                "submitted_at": now - timedelta(days=13),
                "last_reconciled_at": now - timedelta(days=13),
                "metadata": {"reason": "已提交提现请求，回读时亚马逊尚未显示结果"},
            },
        ],
    )

    page = test_client.get(f"/runs/{run_id}")

    assert page.status_code == 200
    rows = _guard_rows(page.text)
    assert len(rows) == 3
    # 按结算周期后缀（种子里的下标）认行，不依赖 armed_at 的排序。
    by_index = {
        int(re.search(r"<code>cycle-[^<]*-(\d+)</code>", row).group(1)): row
        for row in rows
    }
    submitted, closed, legacy = by_index[0], by_index[1], by_index[2]

    assert "已派发" in submitted
    assert "回读" not in submitted
    assert "结案" in submitted, "已派发的记录必须有人工结案的出口"

    assert "回读" not in closed, "人工结案不是回读"

    assert "历史回读" in legacy
    assert "上次回读" not in page.text
    assert "只回读，不重提" not in page.text
    assert "「已提交」就是结论" in page.text


def test_the_manual_close_out_accepts_a_submitted_guard_and_refuses_an_armed_one(client) -> None:
    """⚠️ 删回读之后已派发的记录停在 SUBMITTED。人工结案只认 UNCERTAIN 的话，
    新记录永远没有出口，反倒是存量走得通。

    任务还在跑时的 ARMED 可能正在点击,不许替它下结论。
    """

    app, test_client, headers = client
    now = utc_now()
    run_id = _seed(
        app,
        [
            {"key": "g-ok", "state": "SUBMITTED", "submitted_at": now},
            {"key": "g-armed", "state": "ARMED"},
        ],
        run_status="RUNNING",
    )

    ok = test_client.post(f"/api/runs/{run_id}/guards/g-ok/acknowledge", headers=headers)
    armed = test_client.post(
        f"/api/runs/{run_id}/guards/g-armed/acknowledge", headers=headers
    )

    assert ok.status_code == 200, ok.text
    assert armed.status_code == 409, armed.text
    with app.state.sessions() as session:
        states = {
            guard.guard_key: guard.state for guard in session.query(OperationGuard)
        }
        closed = session.query(OperationGuard).filter_by(guard_key="g-ok").one()
        assert closed.last_reconciled_at is None, "人工结案不写回读时间"
    assert states == {"g-ok": "CONFIRMED", "g-armed": "ARMED"}


def test_an_armed_guard_left_in_a_finished_run_can_be_closed_as_paid(client) -> None:
    """点击之后流程抛了异常:运行停在 UNCERTAIN_FINANCIAL、记录还是 ARMED。收口只在
    重启时才跑,原来这里结案被拒、只给「确认未发出，释放」—— 操作员在卖家后台看到了
    这笔也没有如实结案的出口,而释放会放开当天第二笔(09-23 第二轮复核)。"""

    app, test_client, headers = client
    run_id = _seed(
        app, [{"key": "g-stranded", "state": "ARMED"}], run_status="UNCERTAIN_FINANCIAL"
    )

    page = test_client.get(f"/runs/{run_id}")
    assert 'data-run-action="acknowledge-guard"' in page.text
    assert 'data-run-action="release-guard"' in page.text, "两条出口都要给,由人按事实选"

    response = test_client.post(
        f"/api/runs/{run_id}/guards/g-stranded/acknowledge", headers=headers
    )
    assert response.status_code == 200, response.text
    with app.state.sessions() as session:
        guard = session.query(OperationGuard).filter_by(guard_key="g-stranded").one()
        assert guard.state == "CONFIRMED"
        assert guard.submitted_at is None, "不伪造派发时间"


def test_an_uncertain_guard_with_no_recorded_dispatch_offers_close_out(client) -> None:
    """模板那一半:UNCERTAIN + 没有 submitted_at 的行,也要给「结案」按钮。"""

    app, test_client, _ = client
    run_id = _seed(
        app, [{"key": "g-u", "state": "UNCERTAIN"}], run_status="UNCERTAIN_FINANCIAL"
    )

    page = test_client.get(f"/runs/{run_id}")

    (row,) = _guard_rows(page.text)
    assert 'data-run-action="acknowledge-guard"' in row
    assert 'data-run-action="release-guard"' in row


# ---------------------------------------------------------------------------
# 面板：「待人工核对」
# ---------------------------------------------------------------------------


def test_the_dashboard_counts_only_records_that_need_a_human(client) -> None:
    """数 UNCERTAIN，加上**所属运行已经结束**还停在 ARMED 的。

    不数：SUBMITTED（已派发，是结论）、CONFIRMED、以及正在点击中的 ARMED
    （运行还在跑，那一下还没返回，是正常的）。原来它数的是全部「活跃 guard」，
    面板上一度堆到 44 笔，没有一笔真的需要人管。
    """

    app, test_client, _ = client
    now = utc_now()
    _seed(
        app,
        [
            {"key": "d-submitted", "state": "SUBMITTED", "submitted_at": now},
            {"key": "d-confirmed", "state": "CONFIRMED", "submitted_at": now},
            {"key": "d-uncertain", "state": "UNCERTAIN", "submitted_at": now},
            {"key": "d-armed-stranded", "state": "ARMED"},
        ],
        run_status="UNCERTAIN_FINANCIAL",
    )
    _seed(app, [{"key": "d-armed-in-flight", "state": "ARMED"}], run_status="RUNNING")

    page = test_client.get("/")

    assert page.status_code == 200
    metric = re.search(
        r"<small>待人工核对</small><strong>(\d+)</strong><span>(.*?)</span>", page.text
    )
    assert metric, page.text
    assert metric.group(1) == "2"
    assert "回读" not in metric.group(2)
    assert "需要回读核对" not in page.text


# ---------------------------------------------------------------------------
# 系统诊断页：「强制切换」
# ---------------------------------------------------------------------------


def test_the_force_switch_is_not_visible_until_a_refusal_reveals_it(client) -> None:
    """⚠️ ``hidden`` 属性曾被 ``.button{display:inline-flex}`` 盖掉：页面一打开「强制
    切换」就在那儿，可以不看阻止理由直接越过。要的是「先被拒、看完理由、再决定」。"""

    _, test_client, _ = client

    page = test_client.get("/diagnostics")
    css = test_client.get("/static/app.css")

    assert page.status_code == 200 and css.status_code == 200
    button = re.search(r'<button[^>]*data-action="force-webdriver"[^>]*>', page.text)
    assert button, "诊断页上应该有强制切换按钮"
    assert re.search(r"\shidden(\s|>|=)", button.group(0)), button.group(0)
    # 作者样式表里的 display 规则优先于浏览器默认的 [hidden]{display:none}，
    # 所以全局规则必须带 !important 才压得住 .button。
    assert re.search(r"\[hidden\]\s*\{\s*display\s*:\s*none\s*!important", css.text)
    # ⚠️ 而且必须排在所有同权重的 display:…!important 之后:.store-target 这类规则
    # 权重与 [hidden] 相同,谁在后面谁赢。放在文件开头时,排期页的店铺搜索永远藏不住行,
    # 「全选可见店铺」选中的和屏幕上看到的不是一组(09-23 自审复核)。
    last_hidden = css.text.rindex("[hidden]{display:none!important}")
    later = css.text[last_hidden + len("[hidden]{display:none!important}"):]
    assert not re.search(r"display\s*:\s*[a-z-]+\s*!important", later), later[:200]


def test_a_verdict_also_moves_the_site_row_out_of_the_money_state(client) -> None:
    """人工裁定完一条资金记录，站点行必须跟着走到终态。

    ⚠️ 只改 guard 的话：运行收尾后徽章仍是「资金结果待人工核对」、之后每张卡还把
    它列成待办，而人早就裁定完了（2026-09-26 自审，三条独立发现指向同一处）。
    """

    app, test_client, headers = client
    now = utc_now()
    run_id = _seed(
        app,
        [
            {"key": "g-ack", "state": "UNCERTAIN", "submitted_at": now},
            {"key": "g-rel", "state": "ARMED"},
        ],
        run_status="UNCERTAIN_FINANCIAL",
    )
    with app.state.sessions() as session:
        session.query(SiteRun).filter_by(run_id=run_id).one().status = "UNCERTAIN_FINANCIAL"
        session.commit()

    assert test_client.post(
        f"/api/runs/{run_id}/guards/g-ack/acknowledge", headers=headers
    ).status_code == 200
    with app.state.sessions() as session:
        site = session.query(SiteRun).filter_by(run_id=run_id).one()
        assert site.status == "CONFIRMED"
        assert "核对" in site.error
        site.status = "UNCERTAIN_FINANCIAL"  # 再验释放那一侧
        session.commit()

    assert test_client.post(
        f"/api/runs/{run_id}/guards/g-rel/release", headers=headers
    ).status_code == 200
    with app.state.sessions() as session:
        site = session.query(SiteRun).filter_by(run_id=run_id).one()
        assert site.status == "CANCELLED"
        assert "未发出" in site.error
    # 站点那一行也不再写「待人工核对」(运行本身还是,因为人还没点收尾 —— 那是对的)
    page = test_client.get(f"/runs/{run_id}").text
    ledger = page[page.index('class="site-ledger"'):]
    ledger = ledger[: ledger.index('</section>')]
    assert "资金结果待人工核对" not in ledger, ledger
    assert "已取消" in ledger


def test_a_verdict_never_overwrites_a_site_that_already_finished(client) -> None:
    """反向：站点已经是别的终态（例如余额为零被跳过）时，裁定另一条记录不许改它。"""

    app, test_client, headers = client
    run_id = _seed(
        app,
        [{"key": "g-skip-site", "state": "UNCERTAIN", "submitted_at": utc_now()}],
        run_status="UNCERTAIN_FINANCIAL",
    )
    with app.state.sessions() as session:
        session.query(SiteRun).filter_by(run_id=run_id).one().status = "SKIPPED"
        session.commit()

    assert test_client.post(
        f"/api/runs/{run_id}/guards/g-skip-site/acknowledge", headers=headers
    ).status_code == 200
    with app.state.sessions() as session:
        assert session.query(SiteRun).filter_by(run_id=run_id).one().status == "SKIPPED"
