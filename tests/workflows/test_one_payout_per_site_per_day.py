"""同店同站同日只可能有一条资金记录——这是防重复付款仅剩的实质保护。

run 级的 UNCERTAIN_FINANCIAL 阻断已经拿掉：它挡的从来不是重复付款，只是「账没记清
就不许往下走」，代价是每次回读都要人工开一次浏览器。真正让第二笔发不出去的是这里
两件事，它们此前一条测试都没有：

1. ``guard_key`` 里含**操作员本地日期**，同一天算出来的 key 相同
2. ``arm_operation`` 对已存在的 key 返回原记录、``created=False``，绝不新建第二条

工作流据此在 ``_run_sites`` 里跳过该站点并写下 ``existing_guard_today``
（amazon_disbursement/workflow.py 的 "Never re-arm: that is what keeps a second
payout impossible."）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import OperationGuard, Store, StoreMarketplace
from ziniao_automation.repositories import StoreRepository, WorkflowRepository
from ziniao_automation.workflows.types import disbursement_day_key, operation_guard_key


KEY_FIELDS = dict(
    workflow="amazon_disbursement",
    store_id="7",
    marketplace_code="CA",
    settlement_key="2026/8/26 - 至今",
)


def test_the_key_is_stable_within_one_operator_day() -> None:
    """同一本地日的任意两个时刻必须算出同一个 key，否则一天能发两笔。"""

    morning = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)   # 09:00 UTC+8
    evening = datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)  # 23:30 UTC+8

    assert disbursement_day_key(morning) == disbursement_day_key(evening)
    assert operation_guard_key(
        **KEY_FIELDS, disbursement_date=disbursement_day_key(morning)
    ) == operation_guard_key(
        **KEY_FIELDS, disbursement_date=disbursement_day_key(evening)
    )


def test_the_key_changes_across_the_operator_day_boundary() -> None:
    """跨天必须是新 key——否则第二天永远发不出去。

    边界取操作员本地的午夜，不是 UTC 的：UTC 边界落在 UTC+8 的早上 08:00，正好夹在
    两次早间排期中间。
    """

    before = datetime(2026, 8, 27, 15, 59, tzinfo=timezone.utc)  # 23:59 UTC+8
    after = datetime(2026, 8, 27, 16, 1, tzinfo=timezone.utc)    # 次日 00:01 UTC+8

    assert disbursement_day_key(before) != disbursement_day_key(after)
    assert operation_guard_key(
        **KEY_FIELDS, disbursement_date=disbursement_day_key(before)
    ) != operation_guard_key(
        **KEY_FIELDS, disbursement_date=disbursement_day_key(after)
    )


@pytest.fixture()
def database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'guard.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _armed(session, *, guard_key: str, state: str | None = None):
    store = StoreRepository(session).create(
        name="Guard store", selector_type="id", selector_value=f"p-{guard_key[:6]}",
        browser_id=f"p-{guard_key[:6]}", expected_seller_id="SELLER",
    )
    store.identity_confirmed = True
    store.enabled = True
    market = StoreMarketplace(
        store_id=store.id, code="CA",
        domain="sellercentral.amazon.ca", currency="CAD", enabled=True,
    )
    session.add(market)
    session.flush()
    repo = WorkflowRepository(session)
    run = repo.create_run(store_id=store.id, workflow="amazon_disbursement", mode="auto")
    site = repo.save_site_plan(
        run_id=run.id, marketplace_id=market.id, marketplace_code="CA", currency="CAD",
        payable_amount=Decimal("220.11"), delayed_amount=Decimal("0.00"),
        settlement_key="2026/8/26 - 至今", plan_hash="a" * 64, snapshot_hash="b" * 64,
    )
    guard, created = repo.arm_operation(
        guard_key=guard_key, run_id=run.id, site_run_id=site.id, store_id=store.id,
        workflow="amazon_disbursement", marketplace_code="CA",
        settlement_key="2026/8/26 - 至今", amount=Decimal("220.11"), currency="CAD",
        plan_hash="a" * 64, snapshot_hash="b" * 64,
    )
    if state:
        repo.transition_guard(guard.id, expected_states=("ARMED",), to_state=state)
    session.commit()
    return repo, guard, created, run, site, store


@pytest.mark.parametrize("state", ["ARMED", "SUBMITTED", "UNCERTAIN", "CONFIRMED"])
def test_the_same_key_is_never_armed_twice_whatever_state_it_is_in(database, state) -> None:
    """UNCERTAIN 那一档是重点：那正是「点了但没读回来」的样子。

    拿掉 run 级阻断之后，第二次运行遇到的就是这个现场——它必须原样返回已有记录，
    绝不新建第二条，也不得改写已经记下的派发时刻。
    """

    key = "one-per-site-per-day"
    with database() as session:
        repo, first, created, run, site, store = _armed(session, guard_key=key, state=state)
        assert created is True
        # 直查列，不读 ORM 对象：transition_guard 走的是 Core UPDATE，不会刷新
        # 内存里那个实例，读它拿到的是过期值。
        def _submitted():
            return session.scalar(
                select(OperationGuard.submitted_at).where(OperationGuard.id == first.id)
            )

        before = _submitted()

        again, created_again = repo.arm_operation(
            guard_key=key, run_id=run.id, site_run_id=site.id, store_id=store.id,
            workflow="amazon_disbursement", marketplace_code="CA",
            settlement_key="2026/8/26 - 至今",
            amount=Decimal("999.99"), currency="CAD",
            plan_hash="c" * 64, snapshot_hash="d" * 64,
        )
        session.commit()

        assert created_again is False, "已有记录必须原样返回，绝不重新 arm"
        assert again.id == first.id
        assert session.scalar(select(func.count(OperationGuard.id))) == 1
        assert _submitted() == before, "派发时刻不可被第二次尝试改写"
        assert session.scalar(
            select(OperationGuard.amount).where(OperationGuard.id == first.id)
        ) == Decimal("220.11"), "金额也不该被后来的读数覆盖"
