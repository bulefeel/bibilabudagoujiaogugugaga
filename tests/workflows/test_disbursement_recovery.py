from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
import unittest

from ziniao_automation.workflows.amazon_disbursement import (
    AmazonDisbursementWorkflow,
)
from ziniao_automation.workflows.engine import WorkflowEngine
from ziniao_automation.workflows.errors import PlanChanged
from ziniao_automation.workflows.registry import WorkflowRegistry
from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
from ziniao_automation.workflows.types import (
    ApprovalStatus,
    GuardState,
    MarketplaceRef,
    MarketplaceSnapshot,
    PreflightResult,
    ReconcileResult,
    ReconcileStatus,
    RunMode,
    RunStatus,
    StoreRef,
    SubmissionReceipt,
    WorkflowReport,
    WorkflowRun,
    utc_now,
)


def make_run(mode: RunMode, run_id: str = "run-1") -> WorkflowRun:
    store = StoreRef(
        id="store-1",
        name="测试店铺",
        selector_type="oauth",
        selector_value="oauth-value",
        expected_seller_id="SELLER-123",
        enabled=True,
        identity_confirmed=True,
    )
    market = MarketplaceRef(
        id="market-ca",
        code="CA",
        domain="sellercentral.amazon.ca",
        currency="CAD",
    )
    return WorkflowRun(
        id=run_id,
        workflow="amazon_disbursement",
        mode=mode,
        store=store,
        marketplaces=(market,),
    )


def make_snapshot(amount: str = "10.00") -> MarketplaceSnapshot:
    return MarketplaceSnapshot(
        marketplace_code="CA",
        domain="sellercentral.amazon.ca",
        seller_id="SELLER-123",
        payment_account="ACCOUNT-001",
        currency="CAD",
        payable_amount=Decimal(amount),
        delayed_amount=Decimal("2.00"),
        settlement_key="2026-08-cycle-1",
        can_submit=True,
        contract_version="test-v1",
        page_fingerprint="stable-dom",
    )


class FakePageAdapter:
    def __init__(self) -> None:
        self.snapshot = make_snapshot()
        self.submit_calls = 0
        self.open_confirmation_calls = 0
        # 回读已被删除。留着这个计数器是为了**钉住它不再被调用** ——
        # 适配器上那个方法还在(lookup_existing 与它共用页面代码),但收口不该碰它。
        self.reconcile_calls = 0
        self.crash_on_submit = False
        self.crash_after_confirmation = False

    async def preflight(self, page, run, marketplace):
        return PreflightResult(
            marketplace.code,
            run.store.expected_seller_id,
            "",
            marketplace.domain,
            "test-v1",
        )

    async def read_snapshot(self, page, run, marketplace):
        return self.snapshot

    async def capture_evidence(self, *args, **kwargs):
        return None

    async def lookup_existing(self, page, run, marketplace, expected):
        return ReconcileResult(ReconcileStatus.NOT_FOUND, utc_now())

    async def open_confirmation(self, page, run, marketplace, expected):
        self.open_confirmation_calls += 1
        if self.crash_after_confirmation:
            raise RuntimeError("simulated crash before ARMED")
        if self.snapshot.snapshot_hash != expected.snapshot_hash:
            raise PlanChanged("fixture amount changed before confirmation")
        return replace(
            expected,
            payment_account="001",
        )

    async def submit_once(self, page, run, marketplace, expected):
        self.submit_calls += 1
        if self.crash_on_submit:
            raise RuntimeError("simulated crash after ARMED")
        return SubmissionReceipt(utc_now(), receipt_id="local-click")

    async def reconcile(self, page, run, marketplace, operation):
        self.reconcile_calls += 1
        return ReconcileResult(
            ReconcileStatus.CONFIRMED,
            utc_now(),
            platform_reference="AMZ-REF-1",
            platform_status="initiated",
        )


async def _not_found() -> ReconcileResult:
    """What the statements page says about a payout that was never requested."""

    return ReconcileResult(ReconcileStatus.NOT_FOUND, utc_now())


class _Handle:
    page = object()


class FakeSessions:
    @asynccontextmanager
    async def financial_session(self, selector, store_key=None):
        yield _Handle()


class Collector:
    def __init__(self) -> None:
        self.reports: list[WorkflowReport] = []

    async def send(self, report: WorkflowReport) -> None:
        self.reports.append(report)


def make_engine(repo, adapter):
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
    )
    return WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=FakeSessions(),
        notifier=Collector(),
    )


class DisbursementRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_crash_after_dashboard_click_before_armed_never_final_submits(self) -> None:
        repo = InMemoryWorkflowRepository()
        adapter = FakePageAdapter()
        adapter.crash_after_confirmation = True
        run = make_run(RunMode.AUTO, "run-phase-one-crash")
        await repo.add_run(run)
        engine = make_engine(repo, adapter)

        with self.assertRaisesRegex(RuntimeError, "before ARMED"):
            await engine.start(run)
        self.assertEqual(adapter.open_confirmation_calls, 1)
        self.assertEqual(adapter.submit_calls, 0)
        self.assertEqual(await repo.list_operations(run.id), ())
        self.assertEqual(await repo.get_run_status(run.id), RunStatus.FAILED)

    async def test_approval_is_invalidated_when_amount_changes(self) -> None:
        repo = InMemoryWorkflowRepository()
        adapter = FakePageAdapter()
        run = make_run(RunMode.APPROVAL)
        await repo.add_run(run)
        engine = make_engine(repo, adapter)

        prepared = await engine.start(run)
        self.assertEqual(prepared.status, RunStatus.WAITING_APPROVAL)
        adapter.snapshot = replace(
            adapter.snapshot, payable_amount=Decimal("11.00")
        )
        with self.assertRaises(PlanChanged):
            await engine.approve(run)

        approval = await repo.get_approval(run.id)
        self.assertIsNotNone(approval)
        self.assertEqual(approval.status, ApprovalStatus.INVALIDATED)
        self.assertEqual(adapter.submit_calls, 0)
        self.assertEqual(await repo.list_operations(run.id), ())

    async def test_an_armed_crash_never_clicks_again_and_waits_for_a_human(self) -> None:
        """arm 之后崩溃 → 停在说不清,**绝不第二次点击**,交给人工。

        ⚠️ 这里有一处**刻意接受的能力损失**。2026-09-23 删掉回读之前,重启时会开一次
        紫鸟去亚马逊结算页查这笔到底有没有出去,查到就自动转成 CONFIRMED。现在不查了,
        所以 arm 之后崩溃的记录只能由人在运行详情页收尾
        (「确认未发出，释放」/「我已在亚马逊核对，结案」)。

        换来的是:不再有开机自动开浏览器、不再有查不到就堆一条待办。
        而**真正的红线**——「恢复绝不再点一次提现」——和回读无关,由 ARM 屏障保证,
        下面那条 submit_calls 断言钉的就是它。
        """

        repo = InMemoryWorkflowRepository()
        adapter = FakePageAdapter()
        adapter.crash_on_submit = True
        run = make_run(RunMode.AUTO, "run-crash")
        await repo.add_run(run)
        engine = make_engine(repo, adapter)

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            await engine.start(run)
        records = await repo.list_operations(run.id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].state, GuardState.ARMED)
        self.assertEqual(adapter.submit_calls, 1)
        self.assertEqual(
            await repo.get_run_status(run.id), RunStatus.UNCERTAIN_FINANCIAL
        )

        adapter.crash_on_submit = False
        recovered = await engine.start(run)

        self.assertEqual(recovered.status, RunStatus.UNCERTAIN_FINANCIAL)
        self.assertEqual(adapter.submit_calls, 1, "recovery must never click again")
        self.assertEqual(adapter.reconcile_calls, 0, "收口不许联网")
        records = await repo.list_operations(run.id)
        # 收口把它定性成「说不清」并写清理由,而不是留着 ARMED 让人猜。
        self.assertEqual(records[0].state, GuardState.UNCERTAIN)
        self.assertIn("未能确认", records[0].details["reason"])

    async def test_readback_never_claims_a_payout_that_was_never_dispatched(
        self,
    ) -> None:
        """The one fact this workflow exists to get right must not be invented.

        A guard that armed and then failed before the click carries no
        ``submitted_at``.  Claiming 「已提交提现请求」 for it announces a payout on
        no evidence —— 这正是整条流程唯一必须说准的那件事。

        删掉回读之后这一条更要紧:没有任何东西会去亚马逊求证了,所以文案只能说
        「系统没有记录到派发」,由人去裁定。
        """

        repo = InMemoryWorkflowRepository()
        adapter = FakePageAdapter()
        adapter.crash_on_submit = True
        run = make_run(RunMode.AUTO, "run-phantom")
        await repo.add_run(run)
        engine = make_engine(repo, adapter)

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            await engine.start(run)
        armed = (await repo.list_operations(run.id))[0]
        self.assertEqual(armed.state, GuardState.ARMED)
        self.assertFalse(armed.dispatch_recorded)

        await engine.settle(run)

        record = (await repo.list_operations(run.id))[0]
        self.assertEqual(record.state, GuardState.UNCERTAIN)
        self.assertFalse(record.details["dispatch_recorded"])
        self.assertNotIn("已提交", record.details["reason"])
        self.assertIn("未能确认", record.details["reason"])
        self.assertEqual(adapter.reconcile_calls, 0, "收口不许联网")

    async def test_readback_still_says_submitted_when_a_click_was_recorded(
        self,
    ) -> None:
        """诚实的另一半:点击确实返回过,那就是**成功**,不是「说不清」。

        ⚠️ 这条的期望在 2026-09-23 翻转了。有回读的时候,查不到就写 UNCERTAIN ——
        而亚马逊常隔天才显示,于是每一笔正常提现都会留下一条待办(面板上堆了 43 笔)。
        删掉回读之后,``submitted_at`` 非空就是终态 ``SUBMITTED``、运行 ``SUCCEEDED``。
        不这么改的话,每一次**成功的**提现都会收在红色的 UNCERTAIN_FINANCIAL,
        比删之前还糟。
        """

        repo = InMemoryWorkflowRepository()
        adapter = FakePageAdapter()
        run = make_run(RunMode.AUTO, "run-real-submit")
        await repo.add_run(run)
        engine = make_engine(repo, adapter)

        result = await engine.start(run)

        record = (await repo.list_operations(run.id))[0]
        self.assertEqual(record.state, GuardState.SUBMITTED)
        self.assertTrue(record.dispatch_recorded)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(adapter.reconcile_calls, 0, "提交完不许再联网回读")

    async def test_the_irreversible_click_is_written_to_the_event_stream(self) -> None:
        """"A payout request went out" must be visible in the audit trail.

        It previously lived only in ``operation_guards.submitted_at``: the
        timeline jumped from ``operation_armed`` straight to a status change, so
        reading the run told an operator nothing about money having left.
        """

        repo = InMemoryWorkflowRepository()
        adapter = FakePageAdapter()
        run = make_run(RunMode.AUTO, "run-submitted-event")
        await repo.add_run(run)
        engine = make_engine(repo, adapter)

        await engine.start(run)

        submitted = [
            event
            for event in repo.events
            if event["run_id"] == run.id
            and event["event_type"] == "operation_submitted"
        ]
        self.assertEqual(len(submitted), 1, "exactly one submit per site per run")
        event = submitted[0]
        self.assertIn("不可撤销", event["message"])
        self.assertIn(str(adapter.snapshot.payable_amount), event["message"])
        self.assertEqual(event["details"]["currency"], adapter.snapshot.currency)
        self.assertEqual(
            event["details"]["amount"], str(adapter.snapshot.payable_amount)
        )
        self.assertTrue(event["details"]["submitted_at"])

        order = [
            item["event_type"]
            for item in repo.events
            if item["run_id"] == run.id
            and item["event_type"] in {"operation_armed", "operation_submitted"}
        ]
        self.assertEqual(order, ["operation_armed", "operation_submitted"])

    async def test_no_submit_event_when_nothing_was_clicked(self) -> None:
        """A run that never reaches the click must not claim one happened."""

        repo = InMemoryWorkflowRepository()
        adapter = FakePageAdapter()
        adapter.crash_after_confirmation = True
        run = make_run(RunMode.AUTO, "run-no-submit-event")
        await repo.add_run(run)
        engine = make_engine(repo, adapter)

        with self.assertRaises(RuntimeError):
            await engine.start(run)

        self.assertEqual(adapter.submit_calls, 0)
        self.assertEqual(
            [
                event
                for event in repo.events
                if event["event_type"] == "operation_submitted"
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
