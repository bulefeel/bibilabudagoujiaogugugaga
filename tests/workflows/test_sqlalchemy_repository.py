from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ziniao_automation.db import Base
from ziniao_automation.models import (
    OperationGuard,
    Run,
    SiteRun,
    Store,
    StoreMarketplace,
)
from ziniao_automation.workflows.repository_sqlalchemy import (
    SqlAlchemyWorkflowRepository,
)
from ziniao_automation.workflows.types import (
    GuardState,
    MarketplaceRef,
    MarketplaceSnapshot,
    OperationIntent,
    PlanLine,
    RunStatus,
    SiteStatus,
    WorkflowPlan,
)


class SqlAlchemyRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(engine, expire_on_commit=False)
        with self.factory() as session:
            store = Store(
                name="Store",
                selector_type="oauth",
                selector_value="selector",
                browser_oauth="selector",
                expected_seller_id="SELLER",
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
            run = Run(
                id="sql-run",
                store_id=store.id,
                workflow="amazon_disbursement",
                mode="auto",
                status="QUEUED",
            )
            session.add(run)
            session.commit()
            self.store_id = store.id
            self.market_id = market.id
        self.repo = SqlAlchemyWorkflowRepository(self.factory)

    async def test_armed_is_committed_before_return_and_transitions(self) -> None:
        changed = await self.repo.set_run_status(
            "sql-run", RunStatus.RUNNING, allowed_from=(RunStatus.QUEUED,)
        )
        self.assertTrue(changed)
        await self.repo.set_site_status("sql-run", "CA", SiteStatus.PREFLIGHT)
        market = MarketplaceRef(
            id=str(self.market_id),
            code="CA",
            domain="sellercentral.amazon.ca",
            currency="CAD",
        )
        snapshot = MarketplaceSnapshot(
            marketplace_code="CA",
            domain=market.domain,
            seller_id="SELLER",
            payment_account="ACCOUNT",
            currency="CAD",
            payable_amount=Decimal("10.00"),
            delayed_amount=Decimal("1.00"),
            settlement_key="cycle-1",
            can_submit=True,
            contract_version="test",
            page_fingerprint="dom",
        )
        plan = WorkflowPlan(
            "amazon_disbursement",
            "sql-run",
            str(self.store_id),
            (PlanLine(market, snapshot),),
        )
        await self.repo.save_plan(plan)
        intent = OperationIntent(
            guard_key="guard-key",
            run_id="sql-run",
            site_run_key="unused",
            store_id=str(self.store_id),
            marketplace_code="CA",
            settlement_key="cycle-1",
            amount=Decimal("10.00"),
            currency="CAD",
            plan_hash=plan.plan_hash,
            snapshot_hash=snapshot.snapshot_hash,
        )
        armed = await self.repo.arm_operation(intent)
        self.assertTrue(armed.created)
        with self.factory() as independent:
            persisted = independent.scalar(
                select(OperationGuard).where(
                    OperationGuard.guard_key == "guard-key"
                )
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.state, "ARMED")

        submitted = await self.repo.transition_operation(
            "guard-key",
            expected=(GuardState.ARMED,),
            target=GuardState.SUBMITTED,
            receipt_id="receipt",
            details={"submitted": True},
        )
        self.assertEqual(submitted.state, GuardState.SUBMITTED)
        self.assertEqual(submitted.receipt_id, "receipt")

    async def _armed_guard(self, guard_key: str) -> OperationIntent:
        # ARMED is a financial execution boundary, so fixtures must model the
        # same QUEUED -> RUNNING lease that the worker acquires in production.
        await self.repo.set_run_status(
            "sql-run", RunStatus.RUNNING, allowed_from=(RunStatus.QUEUED,)
        )
        await self.repo.set_site_status("sql-run", "CA", SiteStatus.PREFLIGHT)
        intent = OperationIntent(
            guard_key=guard_key,
            run_id="sql-run",
            site_run_key="unused",
            store_id=str(self.store_id),
            marketplace_code="CA",
            # Distinct per guard: uq_financial_operation is unique on
            # (store_id, workflow, marketplace_code, settlement_key).
            settlement_key=f"cycle-{guard_key}",
            amount=Decimal("10.00"),
            currency="CAD",
            plan_hash="p" * 64,
            snapshot_hash="s" * 64,
        )
        await self.repo.arm_operation(intent)
        return intent

    async def test_release_deletes_a_guard_with_no_recorded_dispatch(self) -> None:
        """Releasing frees the day's guard_key so the site can be retried."""

        await self._armed_guard("release-armed")
        self.assertTrue(await self.repo.release_operation("release-armed"))
        self.assertIsNone(await self.repo.get_operation("release-armed"))
        # Releasing twice is a no-op, not an error.
        self.assertFalse(await self.repo.release_operation("release-armed"))

    async def test_release_clears_an_uncertain_guard_that_never_dispatched(self) -> None:
        """The operator's only exit from a phantom money record.

        A guard that armed and then failed before the click is moved to
        UNCERTAIN by the next read-back, which finds nothing on the statements
        page.  Without this path that row blocks its marketplace and keeps
        reporting a payout that was never requested.
        """

        await self._armed_guard("release-phantom")
        await self.repo.transition_operation(
            "release-phantom",
            expected=(GuardState.ARMED,),
            target=GuardState.UNCERTAIN,
        )
        phantom = await self.repo.get_operation("release-phantom")
        self.assertIsNone(phantom.submitted_at)
        self.assertFalse(phantom.dispatch_recorded)

        self.assertTrue(await self.repo.release_operation("release-phantom"))
        self.assertIsNone(await self.repo.get_operation("release-phantom"))

    async def test_release_refuses_a_guard_that_reached_submitted(self) -> None:
        """The safety floor: a dispatched payout can never be deleted.

        ``submitted_at`` is stamped only after the sole irreversible click
        returns, so it is the hard boundary — including for the UNCERTAIN state,
        which is reachable both from a real dispatch and from a phantom.  The
        predicate lives in the DELETE itself, so a guard that reaches SUBMITTED
        between the caller's decision and this write survives.
        """

        for guard_key, states in (
            ("release-submitted", (GuardState.SUBMITTED,)),
            ("release-uncertain", (GuardState.SUBMITTED, GuardState.UNCERTAIN)),
            ("release-confirmed", (GuardState.SUBMITTED, GuardState.CONFIRMED)),
        ):
            with self.subTest(guard_key=guard_key):
                await self._armed_guard(guard_key)
                for state in states:
                    await self.repo.transition_operation(
                        guard_key,
                        expected=(
                            GuardState.ARMED,
                            GuardState.SUBMITTED,
                            GuardState.UNCERTAIN,
                        ),
                        target=state,
                    )
                dispatched = await self.repo.get_operation(guard_key)
                self.assertTrue(dispatched.dispatch_recorded)
                self.assertFalse(await self.repo.release_operation(guard_key))
                surviving = await self.repo.get_operation(guard_key)
                self.assertIsNotNone(surviving)
                self.assertEqual(surviving.state, states[-1])

    async def test_same_cycle_on_a_different_day_is_a_separate_operation(self) -> None:
        """The invariant is per day, and ``guard_key`` alone now carries it.

        A cycle-wide unique key used to refuse the second arming outright.  An
        Amazon settlement cycle stays open for weeks and only rolls over once a
        payout succeeds, so one leftover guard made its marketplace permanently
        unpayable.  Removed in migration 0004.
        """

        first = await self._armed_guard("cycle-day-1")
        second = replace(first, guard_key="cycle-day-2")
        self.assertEqual(first.settlement_key, second.settlement_key)

        armed = await self.repo.arm_operation(second)
        self.assertTrue(armed.created)
        self.assertEqual(armed.state, GuardState.ARMED)
        self.assertIsNotNone(await self.repo.get_operation("cycle-day-1"))
        self.assertIsNotNone(await self.repo.get_operation("cycle-day-2"))

        # Same day, same key is still one operation and is never re-armed.
        again = await self.repo.arm_operation(second)
        self.assertFalse(again.created)





if __name__ == "__main__":
    unittest.main()
