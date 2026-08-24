from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal

import pytest

from ziniao_automation.workflows.amazon_disbursement import (
    AmazonDisbursementWorkflow,
    DisbursementPolicy,
)
from ziniao_automation.workflows.engine import WorkflowEngine
from ziniao_automation.workflows.errors import (
    DomContractError,
    HumanAuthRequired,
    PayoutRateLimited,
    PreflightRejected,
    SubmissionNotDispatched,
)
from ziniao_automation.workflows.registry import WorkflowRegistry
from ziniao_automation.workflows.repository_memory import InMemoryWorkflowRepository
from ziniao_automation.workflows.types import (
    GuardState,
    MarketplaceRef,
    MarketplaceSnapshot,
    OperationIntent,
    PreflightResult,
    ReconcileResult,
    ReconcileStatus,
    RunMode,
    StoreRef,
    SubmissionReceipt,
    WorkflowRun,
    disbursement_day_key,
    operation_guard_key,
    utc_now,
)


class CursorAdapter:
    def __init__(self) -> None:
        self.preflight_calls: list[str] = []
        self.snapshot_calls: list[str] = []
        self.submit_calls: list[str] = []
        self.open_calls: list[str] = []
        self.auth_raised = False

    async def preflight(self, page, run, marketplace):
        self.preflight_calls.append(marketplace.code)
        return PreflightResult(
            marketplace.code,
            run.store.expected_seller_id,
            "",
            marketplace.domain,
            "test-v1",
        )

    async def read_snapshot(self, page, run, marketplace):
        self.snapshot_calls.append(marketplace.code)
        return MarketplaceSnapshot(
            marketplace_code=marketplace.code,
            domain=marketplace.domain,
            seller_id=run.store.expected_seller_id,
            payment_account="",
            currency=marketplace.currency,
            payable_amount=Decimal("10.00"),
            delayed_amount=Decimal("0.00"),
            settlement_key=f"cycle-{marketplace.code}",
            can_submit=True,
            contract_version="test-v1",
            page_fingerprint=f"dom-{marketplace.code}",
        )

    async def capture_evidence(self, *args, **kwargs):
        return None

    async def lookup_existing(self, *args, **kwargs):
        return ReconcileResult(ReconcileStatus.NOT_FOUND, utc_now())

    async def open_confirmation(self, page, run, marketplace, expected):
        self.open_calls.append(marketplace.code)
        if marketplace.code == "UK" and not self.auth_raised:
            self.auth_raised = True
            raise HumanAuthRequired("Passkey", kind="passkey")
        return replace(expected, payment_account="493")

    async def submit_once(self, page, run, marketplace, expected):
        self.submit_calls.append(marketplace.code)
        return SubmissionReceipt(utc_now(), receipt_id=f"click-{marketplace.code}")

    async def reconcile(self, page, run, marketplace, operation):
        return ReconcileResult(
            ReconcileStatus.CONFIRMED,
            utc_now(),
            platform_reference=f"ref-{marketplace.code}",
            platform_status="initiated",
        )


class Handle:
    def __init__(self):
        self.page = object()


class VisibleZiniaoSessions:
    def __init__(self) -> None:
        self.handle = Handle()
        self.inside = False
        self.wait_inside = False
        self.wait_page = None

    @asynccontextmanager
    async def financial_session(self, selector, store_key=None):
        self.inside = True
        try:
            yield self.handle
        finally:
            self.inside = False

    async def wait_for_auth(self, handle, auth_key, *, timeout_seconds=1800):
        self.wait_inside = self.inside
        self.wait_page = handle.page
        return handle

    async def continue_auth(self, auth_key):
        return False

    async def cancel_auth(self, auth_key):
        return False


def make_multi_site_run() -> WorkflowRun:
    specs = (
        ("CA", "sellercentral.amazon.ca", "CAD"),
        ("UK", "sellercentral.amazon.co.uk", "GBP"),
        ("AU", "sellercentral.amazon.com.au", "AUD"),
    )
    return WorkflowRun(
        id="cursor-run",
        workflow="amazon_disbursement",
        mode=RunMode.AUTO,
        store=StoreRef(
            id="store-1",
            name="store",
            selector_type="oauth",
            selector_value="oauth-value",
            expected_seller_id="SELLER",
            identity_confirmed=True,
        ),
        marketplaces=tuple(
            MarketplaceRef(
                id=f"market-{code.lower()}",
                code=code,
                domain=domain,
                currency=currency,
            )
            for code, domain, currency in specs
        ),
    )


@pytest.mark.asyncio
async def test_auth_on_uk_resumes_same_handle_at_uk_not_ca() -> None:
    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    sessions = VisibleZiniaoSessions()
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=sessions,
    )

    result = await engine.start(run)

    # ``auto`` no longer parks the whole run for a human: UK wanted one, so UK
    # alone is dropped and the cursor advances to AU instead of stalling the
    # queue while holding the browser, store and funds locks.
    assert adapter.open_calls == ["CA", "UK", "AU"]
    assert adapter.submit_calls == ["CA", "AU"]
    assert len(await repo.list_operations(run.id)) == 2
    assert sessions.wait_inside is False, "auto must not wait for a human"

    uk = await repo.get_site_status(run.id, "UK")
    assert uk is not None and uk.value == "NEEDS_HUMAN_AUTH"
    kinds = [
        event["details"].get("kind")
        for event in repo.events
        if event["event_type"] == "site_needs_human_auth"
    ]
    assert kinds == ["passkey"]
    # CA's confirmation/final submission are still never repeated.
    assert adapter.open_calls.count("CA") == 1


@pytest.mark.asyncio
async def test_auto_still_parks_when_the_site_already_armed() -> None:
    """Money that may be in flight is never dropped — it must reconcile.

    The whole point of the drop is throughput, and throughput must never be
    bought with an unreconciled payout.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()

    # Let every site open its confirmation page normally, then demand a human
    # at the moment of the irreversible click — by which time UK is ARMED.
    async def open_without_auth(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        return replace(expected, payment_account="493")

    submitted_once: list[str] = []

    async def submit_needing_human(page, run_, marketplace, expected):
        adapter.submit_calls.append(marketplace.code)
        if marketplace.code == "UK" and "UK" not in submitted_once:
            submitted_once.append("UK")
            raise HumanAuthRequired("Passkey", kind="passkey")
        return SubmissionReceipt(utc_now(), receipt_id=f"click-{marketplace.code}")

    adapter.open_confirmation = open_without_auth
    adapter.submit_once = submit_needing_human
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    sessions = VisibleZiniaoSessions()
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=sessions,
    )

    await engine.start(run)

    assert sessions.wait_inside is True, "an armed site must still park"
    assert [
        event
        for event in repo.events
        if event["event_type"] == "site_needs_human_auth"
    ] == []




@pytest.mark.asyncio
async def test_one_sites_contract_failure_does_not_stop_the_others() -> None:
    """A payable balance drifts while the run works — isolate that site.

    Field-observed 2026-08-18: AU's balance moved between the dashboard read
    and the confirmation page, `_verify_details` raised DomContractError, and
    because nothing caught it per site the whole run failed with UK's GBP
    573.37 still PLANNED and never attempted.  The failure happens before
    ``arm_operation``, so no money moved and the other sites are unaffected.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()

    async def open_with_drifting_amount(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        if marketplace.code == "AU":
            raise DomContractError(
                "确认页金额与执行计划不一致：计划 AUD 931.07，确认页 939.60"
            )
        return replace(expected, payment_account="493")

    adapter.open_confirmation = open_with_drifting_amount
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    await engine.start(run)

    # AU is dropped; CA and UK still get paid.
    assert adapter.submit_calls == ["CA", "UK"]
    assert len(await repo.list_operations(run.id)) == 2
    au = await repo.get_site_status(run.id, "AU")
    assert au is not None and au.value == "FAILED"
    reasons = [
        event["details"].get("reason")
        for event in repo.events
        if event["event_type"] == "site_execution_failed"
    ]
    assert len(reasons) == 1
    # The operator must be able to see BOTH amounts, not just "不一致".
    assert "931.07" in reasons[0] and "939.60" in reasons[0]


@pytest.mark.asyncio
async def test_changed_payout_account_fails_only_its_own_site() -> None:
    """Refusing one site's changed payout account must not veto the rest.

    Field-observed 2026-08-18: UK's payout account really had changed on
    Amazon, `_verify_details` correctly refused to pay it, and — because only
    the `DomContractError` subclass was isolated — that correct refusal killed
    the whole run.  CA and AU were never attempted.  Like every other check in
    this ``try``, it fires before ``arm_operation``: no money moved.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()

    async def open_with_changed_account(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        if marketplace.code == "UK":
            raise PreflightRejected(
                "确认页收款账户与建档基线不一致：建档 尾号 003，确认页 尾号 402。"
                "…点「确认接受新账户」后重新检测即可解锁；"
                "若并非本人变更，请立即核查账户安全，不要解锁。"
            )
        return replace(expected, payment_account="493")

    adapter.open_confirmation = open_with_changed_account
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    await engine.start(run)

    assert adapter.submit_calls == ["CA", "AU"]
    assert len(await repo.list_operations(run.id)) == 2
    uk = await repo.get_site_status(run.id, "UK")
    assert uk is not None and uk.value == "FAILED"
    # No guard was ever armed for the refused site.
    assert all(
        operation.intent.marketplace_code != "UK"
        for operation in await repo.list_operations(run.id)
    )
    reasons = [
        event["details"].get("reason")
        for event in repo.events
        if event["event_type"] == "site_execution_failed"
    ]
    assert len(reasons) == 1
    assert "003" in reasons[0] and "402" in reasons[0]


@pytest.mark.asyncio
async def test_provably_undispatched_site_releases_its_guard_and_others_continue() -> None:
    """An ARMED guard for a click that never happened is worse than no guard.

    ``submit_once`` runs after ``arm_operation``, so a refusal there used to
    strand the guard forever: the run could only be read back, the read-back
    found nothing on the statements page, and the site was then relabelled
    「已提交提现请求，回读时亚马逊尚未显示结果」 — announcing a payout nobody ever
    requested, which is the one fact this whole system exists to get right.
    The adapter can prove the click never happened, so the guard is deleted and
    the site becomes retryable instead.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()
    # This fixture's default UK behaviour is a one-off auth pause, which would
    # drop the site before submit_once is ever reached.  Not what is under test.
    adapter.auth_raised = True

    async def refuse_before_clicking(page, run_, marketplace, expected):
        if marketplace.code == "UK":
            raise SubmissionNotDispatched("确认页最终请求付款按钮不是唯一一个可用按钮")
        adapter.submit_calls.append(marketplace.code)
        return SubmissionReceipt(utc_now(), receipt_id=f"click-{marketplace.code}")

    adapter.submit_once = refuse_before_clicking
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    await engine.start(run)

    assert adapter.submit_calls == ["CA", "AU"]
    uk = await repo.get_site_status(run.id, "UK")
    assert uk is not None and uk.value == "FAILED"
    # The guard is gone, so nothing will later claim UK's money was requested
    # and a later run today may legitimately try again.
    assert all(
        operation.intent.marketplace_code != "UK"
        for operation in await repo.list_operations(run.id)
    )
    released = [
        event for event in repo.events if event["event_type"] == "operation_released"
    ]
    assert len(released) == 1
    assert released[0]["details"]["released"] is True


@pytest.mark.asyncio
async def test_a_failed_site_with_no_payout_is_not_reported_as_completed() -> None:
    """Green 「已完成」 on a run that paid nothing and failed a site is a lie.

    ``_status_from_operations`` returns SUCCEEDED for an empty operation list,
    and the all-skipped downgrade needs EVERY site to be SKIPPED, so one FAILED
    site left the run showing as complete while real money sat unpaid.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()
    adapter.auth_raised = True

    async def refuse_every_site(page, run_, marketplace, expected):
        raise PreflightRejected(f"{marketplace.code} 执行前检查未通过")

    adapter.open_confirmation = refuse_every_site
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    result = await engine.start(run)

    assert adapter.submit_calls == []
    assert result.status.value == "PARTIAL"


@pytest.mark.asyncio
async def test_rate_limited_site_is_skipped_and_the_others_still_pay() -> None:
    """Amazon's 24-hour cap belongs to one marketplace, not to the run.

    Field-observed 2026-08-18: AU had been paid an hour earlier, so its
    confirmation page refused a second request.  That refusal was reported as a
    DOM contract failure and AU was marked FAILED — a red result for an entirely
    normal "come back later", on a run where nothing was wrong.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()
    adapter.auth_raised = True

    async def refuse_au_for_24h(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        if marketplace.code == "AU":
            raise PayoutRateLimited(
                "亚马逊限制该账户 24 小时内仅可请求一次提现，约 22 hrs 49 mins 后可再次请求",
                retry_after="22 hrs 49 mins",
            )
        return replace(expected, payment_account="493")

    adapter.open_confirmation = refuse_au_for_24h
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    await engine.start(run)

    assert adapter.submit_calls == ["CA", "UK"]
    au = await repo.get_site_status(run.id, "AU")
    assert au is not None and au.value == "SKIPPED"
    # Nothing armed for the throttled site, and it is not called a failure.
    assert all(
        operation.intent.marketplace_code != "AU"
        for operation in await repo.list_operations(run.id)
    )
    assert not [
        event for event in repo.events if event["event_type"] == "site_execution_failed"
    ]
    skipped = next(
        event
        for event in repo.events
        if event["event_type"] == "site_skipped"
        and (event["details"] or {}).get("reason_code") == "payout_rate_limited"
    )
    assert skipped["details"]["retry_after"] == "22 hrs 49 mins"


@pytest.mark.asyncio
async def test_every_site_throttled_ends_as_skipped_with_a_reason_per_site() -> None:
    """Field case 9130f315, reproduced end to end.

    All three marketplaces were inside Amazon's rolling 24-hour cap, so all
    three were correctly skipped — and the run then reported nothing anywhere:
    no notification kind existed for ``SKIPPED`` and no branch wrote to the log
    file, so the operator saw the browser open, read both sites and close, with
    every channel silent.  That is indistinguishable from a crash, and it is how
    a working automation came to look broken three days running.

    What this pins is the data the report is built from: the run reaches
    ``SKIPPED`` (not FAILED, not SUCCEEDED) and every site carries its own
    machine-readable reason.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()
    adapter.auth_raised = True

    waits = {"CA": "23 小时 8 分钟", "UK": "24 小时 1 分钟", "AU": "2 小时 40 分钟"}

    async def refuse_every_site_for_24h(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        raise PayoutRateLimited(
            "亚马逊限制该账户 24 小时内仅可请求一次提现，"
            f"约 {waits[marketplace.code]} 后可再次请求",
            retry_after=waits[marketplace.code],
        )

    adapter.open_confirmation = refuse_every_site_for_24h
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    result = await engine.start(run)

    assert adapter.submit_calls == []
    assert not await repo.list_operations(run.id)
    # SKIPPED, not FAILED: nothing went wrong and nothing needs fixing.
    assert result.status.value == "SKIPPED"
    for code in ("CA", "UK", "AU"):
        status = await repo.get_site_status(run.id, code)
        assert status is not None and status.value == "SKIPPED"
    throttled = {
        event["marketplace_code"]: event["details"]["retry_after"]
        for event in repo.events
        if event["event_type"] == "site_skipped"
        and (event["details"] or {}).get("reason_code") == "payout_rate_limited"
    }
    assert throttled == waits


@pytest.mark.asyncio
async def test_a_site_dropped_for_auth_makes_the_run_partial_not_completed() -> None:
    """A dropped marketplace with money on it is not "任务检查已完成".

    Auto mode drops a site that wants a human instead of parking the whole run,
    leaving it ``NEEDS_HUMAN_AUTH`` — which matched neither the all-skipped
    branch nor the any-failed branch, so the run fell through to the default
    SUCCEEDED.  RUN_EXAMPLE_B therefore announced 「任务检查已完成（非提现确认）」
    while the marketplace still held a balance that nothing had even attempted.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()
    adapter.auth_raised = True

    async def au_wants_a_human_others_throttled(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        if marketplace.code == "AU":
            raise HumanAuthRequired("需要人工验证", kind="sign_in")
        raise PayoutRateLimited(
            "亚马逊限制该账户 24 小时内仅可请求一次提现", retry_after="22 小时 5 分钟"
        )

    adapter.open_confirmation = au_wants_a_human_others_throttled
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    result = await engine.start(run)

    assert adapter.submit_calls == []
    assert not await repo.list_operations(run.id)
    au = await repo.get_site_status(run.id, "AU")
    assert au is not None and au.value == "NEEDS_HUMAN_AUTH"
    assert result.status.value == "PARTIAL"


@pytest.mark.asyncio
async def test_existing_guard_skips_only_that_site_instead_of_killing_the_run() -> None:
    """One site's settled money must not veto the other marketplaces.

    The duplicate pre-check demanded the SAME run id, so the day's second run
    aborted at the first site that had already been paid — and every remaining
    site, including ones with real money waiting, was never attempted.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    # An earlier run already paid CA today, and left UK armed-but-unfinished.
    plan = await workflow.plan(run, object())
    by_code = {line.marketplace.code: line for line in plan.lines}
    for code, state in (("CA", GuardState.CONFIRMED), ("UK", GuardState.ARMED)):
        intent = OperationIntent(
            guard_key=operation_guard_key(
                workflow=workflow.name,
                store_id=run.store.id,
                marketplace_code=code,
                settlement_key=by_code[code].snapshot.settlement_key,
                disbursement_date=disbursement_day_key(),
            ),
            run_id="an-earlier-run",
            site_run_key=f"an-earlier-run:{code}",
            store_id=run.store.id,
            marketplace_code=code,
            settlement_key=by_code[code].snapshot.settlement_key,
            amount=Decimal("10.00"),
            currency=by_code[code].marketplace.currency,
            plan_hash="p" * 64,
            snapshot_hash="s" * 64,
        )
        await repo.arm_operation(intent)
        if state is GuardState.CONFIRMED:
            await repo.transition_operation(
                intent.guard_key,
                expected=(GuardState.ARMED,),
                target=GuardState.SUBMITTED,
            )
            await repo.transition_operation(
                intent.guard_key,
                expected=(GuardState.SUBMITTED,),
                target=GuardState.CONFIRMED,
            )
    adapter.submit_calls.clear()

    await engine.start(run)

    # AU is the only site left to pay — and it does get paid.
    assert adapter.submit_calls == ["AU"]
    for code in ("CA", "UK"):
        status = await repo.get_site_status(run.id, code)
        assert status is not None and status.value == "SKIPPED"
    skipped = [
        event["details"].get("guard_state")
        for event in repo.events
        if event["event_type"] == "site_skipped" and event["details"]
    ]
    assert sorted(filter(None, skipped)) == ["ARMED", "CONFIRMED"]


@pytest.mark.asyncio
async def test_growing_balance_no_longer_aborts_and_the_real_amount_is_recorded() -> None:
    """The payable balance is an observation, not a commitment.

    More orders settle while the run works, so the confirmation page routinely
    shows more than the dashboard did seconds earlier — Amazon even says on
    that page that the transferred amount may differ from the displayed
    balance.  Requiring equality aborted the payout; worse, it recorded the
    stale planned figure everywhere instead of the amount actually requested.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()

    grown = {"CA": Decimal("10.55"), "UK": Decimal("11.20"), "AU": Decimal("12.99")}

    async def open_with_grown_amount(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        # What _verify_details returns: the confirmation page's own figure.
        return replace(
            expected,
            payable_amount=grown[marketplace.code],
        )

    adapter.open_confirmation = open_with_grown_amount
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    result = await engine.start(run)

    # Every site is paid out even though all three amounts moved.
    assert result.status.value == "SUCCEEDED"
    assert adapter.submit_calls == ["CA", "UK", "AU"]

    # The guard records what the confirmation page said, not the planned 10.00.
    recorded = {
        op.intent.marketplace_code: op.intent.amount
        for op in await repo.list_operations(run.id)
    }
    assert recorded == grown

    # ...and so does the audit event the operator reads.
    submitted = [
        event for event in repo.events if event["event_type"] == "operation_submitted"
    ]
    assert len(submitted) == 3
    for event in submitted:
        code = event["marketplace_code"]
        assert event["details"]["amount"] == str(grown[code])
        assert event["details"]["planned_amount"] == "10.00"
        assert str(grown[code]) in event["message"]


@pytest.mark.asyncio
async def test_identity_change_still_aborts_even_though_amounts_are_free() -> None:
    """Loosening the amount must not loosen where the money goes."""

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()

    original_read = adapter.read_snapshot
    calls = {"n": 0}

    async def read_with_changed_seller(page, run_, marketplace, *a, **k):
        snapshot = await original_read(page, run_, marketplace)
        calls["n"] += 1
        # First pass is planning; the execute-time re-read reports a different
        # seller for CA.
        if calls["n"] > len(run.marketplaces) and marketplace.code == "CA":
            return replace(snapshot, seller_id="SOMEONE-ELSE")
        return snapshot

    adapter.read_snapshot = read_with_changed_seller
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    await engine.start(run)

    assert "CA" not in adapter.submit_calls, "a changed seller must never pay out"
    ca = await repo.get_site_status(run.id, "CA")
    assert ca is not None and ca.value == "FAILED"


@pytest.mark.asyncio
async def test_any_payout_account_is_accepted_and_recorded_not_judged() -> None:
    """A destination this system has never seen must not stop the payout.

    Amazon owns where a disbursement goes and this automation only presses the
    button, so a locally stored "expected" tail could refuse a transfer but
    never redirect one.  It refused three legitimate payouts — once leaving
    GBP 1,710.23 stranded — and caught nothing, so the comparison is gone.
    What Amazon showed is still recorded against the money record.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    adapter = CursorAdapter()
    adapter.auth_raised = True

    async def confirm_with_a_brand_new_tail(page, run_, marketplace, expected):
        adapter.open_calls.append(marketplace.code)
        return replace(expected, payment_account=f"9{marketplace.code}9")

    adapter.open_confirmation = confirm_with_a_brand_new_tail
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    await engine.start(run)

    assert adapter.submit_calls == ["CA", "UK", "AU"]
    tails = {
        operation.intent.marketplace_code: operation.intent.payout_account_tail
        for operation in await repo.list_operations(run.id)
    }
    assert tails == {"CA": "9CA9", "UK": "9UK9", "AU": "9AU9"}
    assert not [
        event for event in repo.events if event["event_type"] == "site_execution_failed"
    ]


@pytest.mark.asyncio
async def test_a_site_with_no_prior_setup_pays_out_on_its_very_first_run() -> None:
    """Enrolment used to consume a whole run before a site could ever pay.

    A marketplace with no stored baseline was planned, sent through a probe
    that clicked into the confirmation page purely to read a tail, then marked
    SKIPPED with "will be used from the next task onwards".  With no baseline
    to establish, that round trip has no purpose.
    """

    repo = InMemoryWorkflowRepository()
    run = make_multi_site_run()
    await repo.add_run(run)
    # Nothing about these marketplaces carries a payout baseline any more.
    assert not any(
        hasattr(marketplace, "expected_payment_account")
        for marketplace in run.marketplaces
    )
    adapter = CursorAdapter()
    adapter.auth_raised = True
    workflow = AmazonDisbursementWorkflow(
        repository=repo,
        page_adapter=adapter,
        policy=DisbursementPolicy(reconcile_attempts=1, reconcile_interval_seconds=0),
    )
    engine = WorkflowEngine(
        registry=WorkflowRegistry((workflow,)),
        repository=repo,
        browser_sessions=VisibleZiniaoSessions(),
    )

    await engine.start(run)

    assert adapter.submit_calls == ["CA", "UK", "AU"]
    assert not [
        event
        for event in repo.events
        if event["event_type"]
        in {"payment_account_enrolled", "payment_account_conflict"}
    ]
