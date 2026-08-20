from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import (
    OperationGuard,
    Run,
    RunQueueEntry,
    SiteRun,
    StoreMarketplace,
)
from ziniao_automation.repositories import ConflictError, StoreRepository


@pytest.fixture()
def sessions(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'reset-guards.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


def _store(db):
    store = StoreRepository(db).create(
        name="Reset Guard Store",
        selector_type="oauth",
        selector_value="reset-guard-oauth",
        expected_seller_id="SELLER",
    )
    store.identity_confirmed = True
    store.enabled = True
    marketplace = StoreMarketplace(
        store_id=store.id,
        code="CA",
        domain="sellercentral.amazon.ca",
        currency="CAD",
        enabled=True,
    )
    db.add(marketplace)
    db.flush()
    return store, marketplace


def _run(db, store_id: int, *, workflow="amazon_disbursement", status="SUCCEEDED"):
    run = Run(
        store_id=store_id,
        workflow=workflow,
        mode="dry_run",
        trigger="manual",
        status=status,
        requested_by="test",
    )
    db.add(run)
    db.flush()
    return run


def test_any_workflow_active_run_blocks_setup_reset(sessions) -> None:
    with sessions() as db:
        store, _ = _store(db)
        run = _run(db, store.id, workflow="future_inventory_workflow", status="RUNNING")
        with pytest.raises(ConflictError) as raised:
            StoreRepository(db).reset_setup(store.id)
        # Naming the blocker is the point: "there is still an unfinished task"
        # gave the operator nowhere to look.
        assert run.id[:8] in str(raised.value)
        assert "RUNNING" in str(raised.value)
        assert store.expected_seller_id == "SELLER"


def test_ready_queue_blocks_reset_even_when_run_status_is_terminal(sessions) -> None:
    with sessions() as db:
        store, _ = _store(db)
        run = _run(db, store.id, workflow="future_report_workflow", status="SUCCEEDED")
        db.add(
            RunQueueEntry(
                run_id=run.id,
                action="START",
                priority=10,
                state="READY",
            )
        )
        db.flush()
        with pytest.raises(ConflictError):
            StoreRepository(db).reset_setup(store.id)




@pytest.mark.parametrize(
    "status", ["QUEUED", "RUNNING", "WAITING_APPROVAL", "WAITING_AUTH", "RECONCILING"]
)
def test_in_flight_run_statuses_still_block_setup_reset(sessions, status) -> None:
    """Guards the relaxation above from being widened by accident."""

    with sessions() as db:
        store, _ = _store(db)
        _run(db, store.id, status=status)
        with pytest.raises(ConflictError):
            StoreRepository(db).reset_setup(store.id)
        assert store.expected_seller_id == "SELLER"


@pytest.mark.parametrize("status", ["UNCERTAIN_FINANCIAL", "NEEDS_HUMAN_AUTH"])
def test_dead_end_run_statuses_do_not_veto_setup_reset(sessions, status) -> None:
    """A run with no exit must not become a permanent veto on re-enrolling.

    Both statuses stamp ``finished_at``, hold no browser and cannot clear
    themselves: ``NEEDS_HUMAN_AUTH`` waits for a human who never came, and
    ``UNCERTAIN_FINANCIAL`` waits for a read-back that structurally cannot
    succeed, because Amazon often publishes a disbursement only the next day.
    Blocking on them locked the operator out of the one action they need
    precisely when a store is stuck.
    """

    with sessions() as db:
        store, _ = _store(db)
        _run(db, store.id, status=status)
        StoreRepository(db).reset_setup(store.id)
        assert store.expected_seller_id is None
        assert store.identity_confirmed is False


@pytest.mark.parametrize("guard_state", ["ARMED", "SUBMITTED", "UNCERTAIN"])
def test_setup_reset_leaves_every_financial_guard_untouched(sessions, guard_state) -> None:
    """Reset no longer refuses on money, because it no longer touches money.

    These states used to block, to protect a locally stored payout-account
    baseline that reset cleared.  Migration 0005 deleted that baseline, so the
    only thing left to prove is the stronger one: the guard row survives the
    reset byte for byte.
    """

    with sessions() as db:
        store, marketplace = _store(db)
        run = _run(db, store.id)
        site = SiteRun(
            run_id=run.id,
            marketplace_id=marketplace.id,
            marketplace_code="CA",
            status="SUBMITTED",
        )
        db.add(site)
        db.flush()
        db.add(
            OperationGuard(
                guard_key=f"guard-{guard_state}",
                run_id=run.id,
                site_run_id=site.id,
                store_id=store.id,
                workflow="amazon_disbursement",
                marketplace_code="CA",
                settlement_key=f"settlement-{guard_state}",
                state=guard_state,
                amount=Decimal("1.00"),
                currency="CAD",
                plan_hash="p" * 64,
                snapshot_hash="s" * 64,
            )
        )
        db.flush()

        StoreRepository(db).reset_setup(store.id)

        guards = db.scalars(
            select(OperationGuard).where(OperationGuard.store_id == store.id)
        ).all()
        assert [(row.guard_key, row.state, row.amount) for row in guards] == [
            (f"guard-{guard_state}", guard_state, Decimal("1.00"))
        ]
        assert store.expected_seller_id is None

