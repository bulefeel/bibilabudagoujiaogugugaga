"""切换到紫鸟 WebDriver 模式时，为什么被挡、以及怎么越过。

2026-09-22 运营在系统诊断页点了这颗按钮，拿到三条阻止理由，把紫鸟全部关掉之后
报错照旧，于是问「是不是判据坏了」。三件事都得修：

* 前两条是**本服务进程内的 asyncio 锁**，关浏览器根本不碰它们 —— 文案不说清楚，
  操作员只会一遍遍去关浏览器；
* 「以下店铺的浏览器会话仍在进行：36」里的 36 是店铺 id，现在有三十多家店，
  那个数字谁也认不出来；
* 锁被卡住时（持锁的协程在等一个已经关掉的浏览器）除了重启服务没有别的出路，
  所以要一个显式的强制出口。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ziniao_automation.ziniao.webdriver_mode import (
    ModeReport,
    describe_blockers,
    start_webdriver_mode,
)


NAMES = {"2": "正香-张刚CA", "36": "知旭"}


def test_a_busy_store_is_named_not_just_numbered() -> None:
    """屏幕上必须出现店铺名，光给一个 id 等于没说。"""

    blockers = describe_blockers(
        {"funds_locked": False, "stores": {"36": True, "2": False}},
        [],
        store_names=NAMES,
    )

    assert len(blockers) == 1
    assert "知旭" in blockers[0]
    assert "#36" in blockers[0]


def test_an_unknown_store_key_still_says_something_usable() -> None:
    """店铺被删掉之后锁还在字典里。认不出名字也不能崩，也不能凭空编一个。"""

    blockers = describe_blockers(
        {"stores": {"999": True}}, [], store_names=NAMES
    )

    assert blockers == ["以下店铺的浏览器会话仍在进行：#999；关闭紫鸟窗口不会释放它"]


@pytest.mark.parametrize(
    "snapshot",
    [
        {"funds_locked": True},
        {"stores": {"2": True}},
    ],
)
def test_in_process_locks_say_that_closing_ziniao_will_not_help(snapshot) -> None:
    """⚠️ 这是这次事故的核心一句。

    运营把紫鸟全部关掉，报错一字未变，于是以为程序坏了。这两条是 asyncio 锁，
    只有持锁的协程释放、或者重启服务才会没 —— 不写在文案里，人只会重复做
    一件根本不起作用的事。
    """

    blockers = describe_blockers(snapshot, [], store_names=NAMES)

    assert len(blockers) == 1
    assert "关闭紫鸟窗口不会释放它" in blockers[0]


def test_the_durable_guard_line_does_not_claim_that() -> None:
    """反向验证：未收尾的资金记录是 SQLite 里的行，和关不关浏览器无关。

    给它也贴上「关闭紫鸟窗口不会释放它」会把两类完全不同的东西混成一句话。
    """

    blockers = describe_blockers({}, ["AU（ARMED）"], store_names=NAMES)

    assert len(blockers) == 1
    assert "关闭紫鸟窗口" not in blockers[0]
    # 资金回读 09-23 已删除，不能再指人去点一个不存在的按钮；ARMED 也没法「结案」
    # （人工结案只接受已有结论的记录），能做的只有等、或重启让它自动收口。
    assert "回读" not in blockers[0]
    assert "结案" not in blockers[0]
    assert "重启服务后会自动收口" in blockers[0]


@pytest.mark.asyncio
async def test_without_force_a_blocked_switch_kills_nothing(monkeypatch) -> None:
    monkeypatch.setattr(
        "ziniao_automation.ziniao.webdriver_mode.port_open", lambda *a, **k: False
    )
    monkeypatch.setattr(
        "ziniao_automation.ziniao.webdriver_mode._running_ziniao_pids",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该去找进程")),
    )

    report = await start_webdriver_mode(
        Path(__file__),  # 存在即可，走不到启动那一步
        host="127.0.0.1",
        port=16851,
        blockers=["有提现任务正在执行（资金锁被占用）；关闭紫鸟窗口不会释放它"],
    )

    assert isinstance(report, ModeReport)
    assert report.ok is False
    assert report.status == "busy"
    assert report.details["forceable"] is True


@pytest.mark.asyncio
async def test_force_goes_through_and_records_what_it_overrode(
    monkeypatch, caplog
) -> None:
    """强制归强制，**不许静默**。

    真出了「提现提交到一半被杀」的事，得能查到是谁在什么时候越过了什么，
    所以被忽略的每一条都要进 WARNING 日志，界面上也要看得见。

    ⚠️ 但**店铺名不进日志**（店铺名 = 卖家名）：界面上带名字，日志里只有 ``#id``。
    """

    monkeypatch.setattr(
        "ziniao_automation.ziniao.webdriver_mode.port_open",
        _port_open_after_launch(),
    )

    async def no_processes(_executable):
        return []

    monkeypatch.setattr(
        "ziniao_automation.ziniao.webdriver_mode._running_ziniao_pids", no_processes
    )

    launched: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        launched.append(args)

        class _Proc:
            pass

        return _Proc()

    monkeypatch.setattr(
        "ziniao_automation.ziniao.webdriver_mode.asyncio.create_subprocess_exec",
        fake_exec,
    )

    snapshot = {"funds_locked": True, "stores": {"36": True}}
    blockers = describe_blockers(snapshot, [], store_names=NAMES)
    with caplog.at_level("WARNING", logger="ziniao_automation.ziniao.webdriver_mode"):
        report = await start_webdriver_mode(
            Path(__file__),
            host="127.0.0.1",
            port=16851,
            blockers=blockers,
            force=True,
            log_blockers=describe_blockers(snapshot, [], store_names={}),
        )

    assert report.ok is True
    assert launched, "强制切换应该真的去启动客户端"
    assert "强制切换" in report.message
    assert "资金锁" in report.message
    assert "知旭" in report.message, "界面上要认得出是哪家店"
    assert report.details["overridden_blockers"] == blockers

    forced = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(forced) == 1, "越过了什么必须进 WARNING 日志"
    logged = forced[0].getMessage()
    assert "资金锁" in logged and "#36" in logged
    assert "知旭" not in logged, "店铺名（卖家名）不进日志文件"


def _port_open_after_launch():
    """第一次问「端口通了吗」答否（否则直接短路成 already_ready），之后答是。"""

    state = {"calls": 0}

    def port_open(host, port, timeout=None):
        state["calls"] += 1
        return state["calls"] > 1

    return port_open


# ---------------------------------------------------------------------------
# 运行时那一层:AutomationService.start_webdriver_mode
# ---------------------------------------------------------------------------
#
# ⚠️ 上面几条都直接调 webdriver_mode,「只有 ARMED 挡切换」和「日志里只写 #id」这两件
# 事却都写在运行时那一层(runtime.py)。把它们改回去,上面那些照样绿 ——
# 09-23 自审复核抓到的。下面用真的 AutomationService + 临时库走一遍。


@pytest.fixture()
def runtime_db(tmp_path: Path):
    from ziniao_automation.config import Settings
    from ziniao_automation.db import (
        create_sqlite_engine,
        init_database,
        make_session_factory,
    )

    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'webdriver.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _seed_guards(sessions, states: list[str], *, run_status: str = "SUCCEEDED") -> int:
    from ziniao_automation.models import (
        OperationGuard,
        Run,
        SiteRun,
        Store,
        StoreMarketplace,
    )

    with sessions() as session:
        store = Store(
            name="知旭",
            selector_type="oauth",
            selector_value="oauth-wd",
            browser_oauth="oauth-wd",
            expected_seller_id="SELLER-WD",
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
        for index, state in enumerate(states):
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
            site = SiteRun(
                run_id=run.id,
                marketplace_id=market.id,
                marketplace_code="CA",
                status="SUBMITTED",
                currency="CAD",
            )
            session.add(site)
            session.flush()
            session.add(
                OperationGuard(
                    guard_key=f"wd-{index}",
                    run_id=run.id,
                    site_run_id=site.id,
                    store_id=store.id,
                    workflow="amazon_disbursement",
                    marketplace_code="CA",
                    settlement_key=f"wd-cycle-{index}",
                    state=state,
                    amount=10,
                    currency="CAD",
                    plan_hash="a" * 64,
                    snapshot_hash="b" * 64,
                )
            )
        session.commit()
        return store.id


def _service(sessions, busy_stores: dict[str, bool]):
    from types import SimpleNamespace

    from ziniao_automation.workflows.runtime import AutomationService

    class Locks:
        def snapshot(self):
            return {"funds_locked": False, "stores": dict(busy_stores)}

    controller = SimpleNamespace(
        locks=Locks(),
        config=SimpleNamespace(client_path=__file__),
        client=SimpleNamespace(config=SimpleNamespace(host="127.0.0.1", port=16851)),
    )
    return AutomationService(
        engine=SimpleNamespace(registry=None, repository=object()),
        run_loader=object(),
        ziniao_controller=controller,
        session_factory=sessions,
    )


def _no_real_client(monkeypatch) -> list[tuple]:
    monkeypatch.setattr(
        "ziniao_automation.ziniao.webdriver_mode.port_open", _port_open_after_launch()
    )

    async def no_processes(_executable):
        return []

    monkeypatch.setattr(
        "ziniao_automation.ziniao.webdriver_mode._running_ziniao_pids", no_processes
    )
    launched: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        launched.append(args)
        return object()

    monkeypatch.setattr(
        "ziniao_automation.ziniao.webdriver_mode.asyncio.create_subprocess_exec",
        fake_exec,
    )
    return launched


@pytest.mark.asyncio
async def test_concluded_payouts_do_not_block_a_normal_switch(runtime_db, monkeypatch) -> None:
    """SUBMITTED 已点完、UNCERTAIN 是等人裁定的纸面记录:关不关浏览器都不影响它们。
    把它们算进来,第一笔提现成功之后普通切换就永远被挡(09-23 自审抓到的回归)。"""

    # ⚠️ 必须挂在**还在进行**的运行上。挂在 SUCCEEDED 上的话,后来加的
    # 「只数进行中运行的 guard」那道 join 会先把它整条过滤掉,这条测试就检验不到
    # 它自称要钉的状态过滤了(2026-09-26 自审)。
    _seed_guards(
        runtime_db, ["SUBMITTED", "UNCERTAIN", "CONFIRMED"], run_status="RECONCILING"
    )
    launched = _no_real_client(monkeypatch)

    result = await _service(runtime_db, {}).start_webdriver_mode()

    assert result["ok"] is True, result
    assert launched


@pytest.mark.asyncio
async def test_a_click_in_flight_blocks_a_normal_switch(runtime_db, monkeypatch) -> None:
    _seed_guards(runtime_db, ["ARMED"], run_status="RUNNING")
    launched = _no_real_client(monkeypatch)

    result = await _service(runtime_db, {}).start_webdriver_mode()

    assert result["ok"] is False
    assert result["status"] == "busy"
    assert "CA（ARMED）" in result["message"]
    assert launched == []


@pytest.mark.asyncio
async def test_an_armed_record_of_a_finished_run_is_not_a_click_in_flight(
    runtime_db, monkeypatch
) -> None:
    """任务早已结束还停在 ARMED(点击后抛了异常)是等人裁定的纸面记录。算进来会叫人
    去「等一个永远不会结束的任务」(09-23 第二轮复核)。"""

    _seed_guards(runtime_db, ["ARMED"], run_status="UNCERTAIN_FINANCIAL")
    launched = _no_real_client(monkeypatch)

    result = await _service(runtime_db, {}).start_webdriver_mode()

    assert result["ok"] is True, result
    assert launched


@pytest.mark.asyncio
async def test_a_forced_switch_logs_store_ids_never_store_names(
    runtime_db, monkeypatch, caplog
) -> None:
    store_id = _seed_guards(runtime_db, [])
    _no_real_client(monkeypatch)

    with caplog.at_level("WARNING", logger="ziniao_automation.ziniao.webdriver_mode"):
        result = await _service(runtime_db, {str(store_id): True}).start_webdriver_mode(
            force=True
        )

    assert result["ok"] is True
    assert "知旭" in result["message"], "界面上要认得出是哪家店"
    forced = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(forced) == 1
    assert f"#{store_id}" in forced[0]
    assert "知旭" not in forced[0], "店铺名(卖家名)不进日志文件"
