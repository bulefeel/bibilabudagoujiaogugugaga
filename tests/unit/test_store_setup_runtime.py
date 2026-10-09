from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from ziniao_automation.config import Settings
from ziniao_automation.db import (
    create_sqlite_engine,
    init_database,
    make_session_factory,
    utc_now,
)
from ziniao_automation.models import OperationGuard, Store, StoreMarketplace
from ziniao_automation.workflows.errors import HumanAuthRequired
from ziniao_automation.workflows.runtime import AutomationService
from ziniao_automation.workflows.store_setup.adapter import SiteSetupResult
from ziniao_automation.ziniao.errors import ZiniaoCredentialError


class _PageToken:
    pass


class _DelayedPaymentDetailsPage:
    def __init__(self) -> None:
        self.url = "https://sellercentral.amazon.ca/payments/dashboard/index.html"
        self.elapsed_ms = 0
        self.details_at_ms: int | None = None

    def schedule_details(self, *, delay_ms: int) -> None:
        self.details_at_ms = self.elapsed_ms + delay_ms

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.elapsed_ms += milliseconds
        if self.details_at_ms is not None and self.elapsed_ms >= self.details_at_ms:
            self.url = (
                "https://sellercentral.amazon.ca/payments/disburse/details"
                "?accountType=PAYABLE"
            )
            self.details_at_ms = None


class _Controller:
    def __init__(self) -> None:
        self.opens = 0
        self.page = _PageToken()
        self.handle = None
        self.wait_for_auth_calls = 0
        self.closed = asyncio.Event()

    @asynccontextmanager
    async def financial_session(self, selector, store_key=None):
        del selector, store_key
        self.opens += 1
        self.handle = type("Handle", (), {"page": self.page, "cleanup_confirmed": True})()
        try:
            yield self.handle
        finally:
            self.closed.set()

    async def wait_for_auth(self, handle, key, **kwargs):
        del handle, key, kwargs
        self.wait_for_auth_calls += 1


def _service(tmp_path, controller, *, seller_id=None, confirmed=False, enabled=False):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'store-setup.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    sessions = make_session_factory(engine)
    with sessions() as db:
        store = Store(
            name="Unified Setup Store", raw_profile={"platform_id": "1"},
            selector_type="oauth",
            selector_value="unified-setup-oauth",
            expected_seller_id=seller_id,
            identity_confirmed=confirmed,
            enabled=enabled,
        )
        db.add(store)
        db.commit()
        store_id = store.id
    service = AutomationService(
        engine=object(),
        run_loader=lambda _: None,
        ziniao_controller=controller,
        session_factory=sessions,
        identity_auth_timeout_seconds=1.0,
    )
    return service, sessions, store_id


async def _terminal(service, store_id, probe_id):
    for _ in range(100):
        result = await service.get_store_setup_probe(store_id, probe_id)
        if result["status"] in {
            "SUCCEEDED",
            "PARTIAL",
            "FAILED",
            "UNAVAILABLE",
            "NEEDS_REVIEW",
            "CANCELLED",
        }:
            return result
        await asyncio.sleep(0)
    raise AssertionError("unified setup probe did not finish")


@pytest.mark.asyncio
async def test_unified_setup_preflights_credentials_before_probe_or_site_write(
    tmp_path,
) -> None:
    class CredentialFailController(_Controller):
        def __init__(self) -> None:
            super().__init__()
            self.preflight_calls = 0

        def preflight_credentials(self) -> None:
            self.preflight_calls += 1
            raise ZiniaoCredentialError("紫鸟凭据引用已失效")

    controller = CredentialFailController()
    service, sessions, store_id = _service(tmp_path, controller)

    with pytest.raises(ZiniaoCredentialError, match="凭据引用已失效"):
        await service.detect_store_setup(store_id, ["CA", "UK"])

    assert controller.preflight_calls == 1
    assert controller.opens == 0
    assert service._marketplace_setup_probes == {}
    assert service._marketplace_setup_store_probes == {}
    with sessions() as db:
        assert list(
            db.scalars(
                select(StoreMarketplace).where(
                    StoreMarketplace.store_id == store_id
                )
            )
        ) == []












@pytest.mark.asyncio
async def test_unified_setup_reaching_the_end_succeeds_and_reports_its_store(
    tmp_path, monkeypatch
) -> None:
    """Drive a unified setup all the way to its terminal payload.

    Every other test in this file stops the probe early — on a credential
    failure, on the hard deadline, or by cancelling a reattached probe.  Nothing
    exercised the terminal ``return`` of ``_run_store_setup_account_sites``,
    which deleted ``store_id`` at the top of the body and then read it again
    when assembling that payload.  The result was an UnboundLocalError on every
    setup whose assisted login actually got through, finalized as FAILED via the
    generic-exception branch: unified setup could never report success, and the
    only reason nobody saw it is that the probe usually stalls earlier, at
    assisted login.
    """

    controller = _Controller()
    service, sessions, store_id = _service(tmp_path, controller)

    async def identity(self, page, marketplace):
        del self, page, marketplace
        return "A1SELLERFIXTURE", "payments-dashboard"

    async def preflight(self, page, run, marketplace):
        del self, page, run, marketplace
        return SiteSetupResult("PASSED", "PASSED", "SUPPORTED", "fixture")

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity",
        identity,
    )
    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site",
        preflight,
    )
    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.resolved_page_for",
        lambda self, page: page,
    )

    started = await service.detect_store_setup(store_id, ["CA", "UK"])
    result = await _terminal(service, store_id, started["probe_id"])

    assert result["status"] == "SUCCEEDED", result.get("message")
    # The regression made this key the crash site, so assert the value, not just
    # its presence.
    assert result["store_id"] == store_id
    assert result["seller_id"] == "A1SELLERFIXTURE"
    assert {site["status"] for site in result["marketplaces"]} == {"SUCCEEDED"}
    with sessions() as db:
        store = db.get(Store, store_id)
        assert store.expected_seller_id == "A1SELLERFIXTURE"
        assert store.identity_confirmed is True


@pytest.mark.asyncio
async def test_unified_setup_hard_deadline_cancels_identity_cdp_and_fails_pending_sites(
    tmp_path, monkeypatch
):
    controller = _Controller()
    service, _, store_id = _service(tmp_path, controller)
    service.identity_auth_timeout_seconds = 0.02
    cdp_started = asyncio.Event()
    cdp_cancelled = asyncio.Event()
    never_finishes = asyncio.Event()

    async def hung_identity(self, page, marketplace):
        del self, page, marketplace
        cdp_started.set()
        try:
            await never_finishes.wait()
        except asyncio.CancelledError:
            cdp_cancelled.set()
            raise

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity",
        hung_identity,
    )

    started = await service.detect_store_setup(store_id, ["CA", "UK"])
    await asyncio.wait_for(cdp_started.wait(), timeout=1.0)
    await asyncio.wait_for(controller.closed.wait(), timeout=1.0)
    result = await _terminal(service, store_id, started["probe_id"])

    assert cdp_cancelled.is_set()
    assert controller.opens == 1
    assert result["status"] == "FAILED"
    assert result["outcome"] == "TIMED_OUT"
    assert result["identity"]["status"] == "FAILED"
    assert result["identity"]["reason_code"] == "SETUP_TIMED_OUT"
    assert {site["status"] for site in result["marketplaces"]} == {"FAILED"}
    assert all(
        site["outcome"] == "NOT_RUN" and not site["attempted"] for site in result["marketplaces"]
    )












@pytest.mark.asyncio
async def test_reattaching_active_probe_does_not_enable_different_sites(
    tmp_path, monkeypatch
):
    controller = _Controller()
    service, sessions, store_id = _service(tmp_path, controller)
    blocked = asyncio.Event()

    async def identity(self, page, marketplace):
        del self, page, marketplace
        await blocked.wait()
        return "SELLER", "fixture"

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity",
        identity,
    )
    first = await service.detect_store_setup(store_id, ["CA"])
    with pytest.raises(RuntimeError, match="不同范围"):
        await service.detect_store_setup(store_id, ["UK"])
    attached = await service.detect_store_setup(store_id, ["CA"])

    assert attached["probe_id"] == first["probe_id"]
    with sessions() as db:
        rows = list(
            db.scalars(
                select(StoreMarketplace).where(StoreMarketplace.store_id == store_id)
            )
        )
        assert [(row.code, row.enabled) for row in rows] == [("CA", True)]
    cancelled = await service.cancel_store_setup_probe(store_id, first["probe_id"])
    assert cancelled["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_a_site_the_switcher_cannot_select_fails_instead_of_waiting_for_a_login(
    tmp_path, monkeypatch
) -> None:
    """建档核验落到账户/站点选择页、自动选站又选不掉:这不是登录问题。

    ⚠️ 原来它被当成「等待登录」:探针占着资金锁在一个不存在的登录上等满 30 分钟,
    之后的站点一个都轮不到(09-23 第三轮复核)。现在如实记下原因、接着核下一个。
    """

    class NoLoginWait(_Controller):
        async def wait_for_auth(self, handle, key, **kwargs):
            # ⚠️ 必须响亮失败,不能像基类那样立刻返回:否则修复被改回去时,核验循环
            # 会在「等登录 → 再核验 → 又要选站」之间无限转圈,测试挂死而不是变红
            # (09-23 反证时真的挂死过,把内存吃满)。
            self.wait_for_auth_calls += 1
            raise AssertionError("选不定站点不该进登录等待")

    controller = NoLoginWait()
    service, sessions, store_id = _service(tmp_path, controller)

    async def identity(self, page, marketplace):
        del self, page, marketplace
        return "A1SELLERFIXTURE", "payments-dashboard"

    async def preflight(self, page, run, marketplace):
        del self, page, run
        if marketplace.code == "CA":
            raise HumanAuthRequired(
                "页面停在亚马逊账户/站点选择页，未能自动选定 CA（not_offered）",
                kind="marketplace_switch",
                reason_code="MARKETPLACE_NOT_OFFERED",
                stage="marketplace_switch", page_category="SITE_SWITCHER",
            )
        return SiteSetupResult("PASSED", "PASSED", "SUPPORTED", "fixture")

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", identity
    )
    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site", preflight
    )
    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonStoreSetupPage.resolved_page_for",
        lambda self, page: page,
    )

    started = await service.detect_store_setup(store_id, ["CA", "UK"])
    result = await _terminal(service, store_id, started["probe_id"])

    assert controller.wait_for_auth_calls == 0, "选不定站点不该进登录等待"
    sites = {site["code"]: site for site in result["marketplaces"]}
    assert sites["CA"]["status"] == "FAILED"
    assert sites["CA"]["reason_code"] == "MARKETPLACE_NOT_OFFERED"
    assert "开通情况待确认" in sites["CA"]["message"]
    assert sites["UK"]["status"] == "SUCCEEDED"
    assert result["status"] == "PARTIAL"


class _DiagnosticWaitController(_Controller):
    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()
        self.decision = asyncio.Event()
        self.cancelled = False

    async def wait_for_auth(self, handle, key, *, timeout_seconds, on_waiting=None):
        from ziniao_automation.ziniao.errors import AuthWaitCancelled
        assert handle is self.handle
        self.wait_for_auth_calls += 1
        if on_waiting:
            await on_waiting()
        self.waiting.set()
        await asyncio.wait_for(self.decision.wait(), timeout_seconds)
        if self.cancelled:
            raise AuthWaitCancelled("cancelled")
        return handle

    async def continue_auth(self, key):
        self.decision.set()
        return True

    async def cancel_auth(self, key):
        self.cancelled = True
        self.decision.set()
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["identity", "site", "legacy_site"])
async def test_setup_auth_diagnostic_survives_wait_resume_and_redacts_raw_error(
    tmp_path, monkeypatch, caplog, phase,
):
    import json
    caplog.set_level("INFO", logger="ziniao_automation.workflows.runtime")
    controller = _DiagnosticWaitController()
    service, sessions, store_id = _service(
        tmp_path, controller, seller_id="A1SELLERFIXTURE", confirmed=True,
    )
    service.identity_auth_timeout_seconds = 10
    first_identity = first_site = True
    secret = "SENTINEL_PASSWORD_COOKIE_OTP_123456"

    def stopped():
        return HumanAuthRequired(
            secret, kind="challenge", reason_code="AUTH_INPUT_NOT_READY",
            stage="otp_signin", action="otp_signin", attempts=0,
            page_category="LOGIN",
        )

    async def identity(self, page, marketplace):
        nonlocal first_identity
        if phase == "identity" and first_identity:
            first_identity = False
            raise stopped()
        return "A1SELLERFIXTURE", "fixture"

    async def preflight(self, page, run, marketplace):
        nonlocal first_site
        if phase != "identity" and first_site:
            first_site = False
            raise stopped()
        return SiteSetupResult("PASSED", "PASSED", "SUPPORTED", "fixture")

    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", identity)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site", preflight)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.resolved_page_for", lambda self, page: page)
    legacy = phase == "legacy_site"
    start = service.detect_marketplace_setup if legacy else service.detect_store_setup
    get = service.get_marketplace_setup_probe if legacy else service.get_store_setup_probe
    resume = service.continue_marketplace_setup_probe if legacy else service.continue_store_setup_probe
    cancel = service.cancel_marketplace_setup_probe if legacy else service.cancel_store_setup_probe
    started = await start(store_id, ["CA"])
    pid = started["probe_id"]
    try:
        await asyncio.wait_for(controller.waiting.wait(), 1)
        waiting = await get(store_id, pid)
        assert waiting["status"] == "WAITING_AUTH"
        assert waiting["last_diagnostic"]["reason_code"] == "AUTH_INPUT_NOT_READY"
        assert waiting["last_diagnostic"]["stage"] == "otp_signin"
        assert waiting["last_diagnostic"]["attempts"] == 0
        assert "自动填充尚未就绪" in waiting["message"]
        assert secret not in json.dumps(waiting) + caplog.text
        detail = waiting["identity"] if phase == "identity" else waiting["marketplaces"][0]
        assert detail["reason_code"] == "AUTH_INPUT_NOT_READY"
        await resume(store_id, pid)
        for _ in range(100):
            final = await get(store_id, pid)
            if final["status"] == "SUCCEEDED":
                break
            await asyncio.sleep(0)
        assert final["status"] == "SUCCEEDED"
        assert "reason_code" not in final["marketplaces"][0]
        assert final["last_diagnostic"]["reason_code"] == "AUTH_INPUT_NOT_READY"
        assert secret not in json.dumps(final) + caplog.text
        assert controller.opens == 1
        assert controller.closed.is_set()
        with sessions() as db:
            assert list(db.scalars(select(OperationGuard))) == []
    finally:
        await cancel(store_id, pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason, expected_sites, outcome", [
    ("INACTIVE_ACCOUNT", ["CA", "UK"], "PARTIAL"),
    ("DOMAIN_MISMATCH", ["CA"], "FAILED"),
])
async def test_setup_distinguishes_site_blocker_from_untrusted_environment(
    tmp_path, monkeypatch, reason, expected_sites, outcome,
):
    controller = _Controller()
    service, sessions, store_id = _service(tmp_path, controller)
    checked = []

    async def identity(self, page, marketplace):
        return "A1SELLERFIXTURE", "fixture"

    async def preflight(self, page, run, marketplace):
        checked.append(marketplace.code)
        if marketplace.code == "CA":
            raise HumanAuthRequired(
                "raw details must not escape", kind="marketplace_switch",
                reason_code=reason, stage="marketplace_switch", page_category=reason,
            )
        return SiteSetupResult("PASSED", "PASSED", "SUPPORTED", "fixture")

    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", identity)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site", preflight)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.resolved_page_for", lambda self, page: page)
    started = await service.detect_store_setup(store_id, ["CA", "UK"])
    final = await _terminal(service, store_id, started["probe_id"])
    assert final["status"] == outcome
    assert checked == expected_sites
    assert final["marketplaces"][0]["reason_code"] == reason
    assert final["last_diagnostic"]["reason_code"] == reason
    assert controller.wait_for_auth_calls == 0
    assert controller.closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["detect_store_setup", "detect_marketplace_setup", "detect_store_identity"])
async def test_missing_onboarding_policy_rejects_before_browser_or_site_writes(tmp_path, entry):
    controller = _Controller()
    service, sessions, store_id = _service(tmp_path, controller, seller_id="SELLER", confirmed=True)
    service.setup_policy_path = tmp_path / "missing-policy.toml"
    with pytest.raises(ValueError, match="onboarding-policy.toml"):
        args = (store_id,) if entry == "detect_store_identity" else (store_id, ["CA"])
        await getattr(service, entry)(*args)
    assert controller.opens == 0
    assert service._marketplace_setup_probes == {}
    assert service._identity_probes == {}
    with sessions() as db:
        assert list(db.scalars(select(StoreMarketplace))) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["SUPPORTED", "NO_PAYMENT_DATA", "UNKNOWN"])
async def test_readonly_check_results_are_not_financial_runs(tmp_path, monkeypatch, capability):
    from ziniao_automation.models import Run, OperationGuard
    controller = _Controller()
    service, sessions, store_id = _service(tmp_path, controller)
    async def identity(self, page, marketplace):
        return "SELLER", "dom:content"
    async def check_site(self, page, store, marketplace):
        return SiteSetupResult("PASSED", "PASSED", capability, "dom:content")
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", identity)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site", check_site)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.resolved_page_for", lambda self, page: page)
    started = await service.detect_store_setup(store_id, ["CA"])
    result = await _terminal(service, store_id, started["probe_id"])
    assert result["status"] == "SUCCEEDED"
    checked = result["marketplaces"][0]
    assert checked["identity_check"] == checked["access_check"] == "PASSED"
    assert checked["payment_capability"] == capability
    with sessions() as db:
        assert list(db.scalars(select(Run))) == []
        assert list(db.scalars(select(OperationGuard))) == []


@pytest.mark.asyncio
async def test_none_check_result_is_failure_not_silent_success(tmp_path, monkeypatch):
    controller = _Controller()
    service, _, store_id = _service(tmp_path, controller)
    async def identity(self, page, marketplace):
        return "SELLER", "dom:content"
    async def invalid(self, page, store, marketplace):
        return None
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", identity)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site", invalid)
    started = await service.detect_store_setup(store_id, ["CA", "UK"])
    result = await _terminal(service, store_id, started["probe_id"])
    assert result["status"] == "FAILED"
    assert {site["status"] for site in result["marketplaces"]} == {"FAILED"}
    assert result["reason_code"] == "SETUP_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["identity", "site"])
async def test_structured_identity_conflict_stops_remaining_sites(tmp_path, monkeypatch, phase):
    from ziniao_automation.workflows.store_setup.identity import SetupRejected
    controller = _Controller()
    service, sessions, store_id = _service(tmp_path, controller, seller_id="BOUND", confirmed=True)
    checked = []
    async def identity(self, page, marketplace):
        if phase == "identity":
            raise SetupRejected("IDENTITY_AMBIGUOUS", count=2)
        return "BOUND", "dom:content"
    async def check_site(self, page, store, marketplace):
        checked.append(marketplace.code)
        raise SetupRejected("IDENTITY_MISMATCH")
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", identity)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site", check_site)
    started = await service.detect_store_setup(store_id, ["CA", "UK"])
    result = await _terminal(service, store_id, started["probe_id"])
    assert result["status"] == ("NEEDS_REVIEW" if phase == "site" else "FAILED")
    assert checked == ([] if phase == "identity" else ["CA"])
    expected = "IDENTITY_AMBIGUOUS" if phase == "identity" else "IDENTITY_MISMATCH"
    assert result["last_diagnostic"]["reason_code"] == expected
    assert all(site["status"] == "FAILED" for site in result["marketplaces"])
    with sessions() as db:
        assert db.get(Store, store_id).expected_seller_id == "BOUND"


@pytest.mark.asyncio
async def test_missing_payment_structure_does_not_report_identity_failure(tmp_path, monkeypatch):
    from ziniao_automation.workflows.store_setup.identity import SetupRejected
    controller = _Controller()
    service, _, store_id = _service(tmp_path, controller)
    async def identity(self, page, marketplace):
        return "SELLER", "dom:content"
    async def check_site(self, page, store, marketplace):
        if marketplace.code == "CA":
            raise SetupRejected("SITE_ACCESS_UNPROVEN", stage="site_access", category="TARGET_PAGE")
        return SiteSetupResult("PASSED", "PASSED", "SUPPORTED", "dom:content")
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity", identity)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site", check_site)
    started = await service.detect_store_setup(store_id, ["CA", "UK"])
    result = await _terminal(service, store_id, started["probe_id"])
    assert result["status"] == "PARTIAL"
    ca = result["marketplaces"][0]
    assert ca["identity_check"] == "PASSED"
    assert ca["access_check"] == "FAILED"
    assert ca["payment_capability"] == "UNKNOWN"
    assert result["marketplaces"][1]["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_marketplace_owned_by_different_account_stops_the_store(tmp_path, monkeypatch):
    controller = _Controller()
    service, _, store_id = _service(tmp_path, controller)
    checked=[]
    async def identity(self, page, marketplace):
        return "SELLER", "dom:content"
    async def wrong_owner(self, page, store, marketplace):
        checked.append(marketplace.code)
        raise HumanAuthRequired("owner mismatch",kind="marketplace_switch",
                                reason_code="IDENTITY_MISMATCH",stage="marketplace_switch")
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity",identity)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site",wrong_owner)
    started=await service.detect_store_setup(store_id,["CA","UK"])
    result=await _terminal(service,store_id,started["probe_id"])
    assert result["status"] == "NEEDS_REVIEW"
    assert checked == ["CA"]
    assert result["last_diagnostic"]["reason_code"] == "IDENTITY_MISMATCH"


@pytest.mark.asyncio
async def test_setup_cleanup_hang_is_bounded_and_late_completion_does_not_reopen(tmp_path):
    from ziniao_automation.setup_errors import SetupConflict
    from ziniao_automation.ziniao.models import ProfileSelector

    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    class HangingCleanup(_Controller):
        @asynccontextmanager
        async def financial_session(self, selector, store_key=None):
            self.handle = type('Handle', (), {'cleanup_confirmed': False})()
            try:
                yield self.handle
            finally:
                cleanup_entered.set()
                try:
                    await cleanup_release.wait()
                except asyncio.CancelledError:
                    await cleanup_release.wait()
                self.handle.cleanup_confirmed = True
                self.closed.set()
    controller = HangingCleanup()
    service, _, sid = _service(tmp_path, controller)
    service.setup_cleanup_grace_seconds = .02
    row = service.setup_records.begin(sid, ['CA'], kind='unified', seconds=30, policy_version='v1')
    pid = row['probe_id']
    async def operate():
        async with service._setup_session(pid, ProfileSelector('oauth','unified-setup-oauth'), sid):
            service.setup_records.progress(pid, sites=[{'code':'CA','status':'SUCCEEDED','identity_check':'PASSED','access_check':'PASSED'}])
        return {'status':'SUCCEEDED'}
    try:
        await asyncio.wait_for(service._run_setup_probe_before_deadline(expires_at=asyncio.get_running_loop().time()+2,
            cleanup_grace_seconds=.02, operation=operate, probe_id=pid), 1)
        assert cleanup_entered.is_set()
        saved = service.setup_records.get(sid, pid)
        assert saved['lifecycle'] == 'FINISHED' and saved['cleanup_state'] == 'UNCONFIRMED'
        with pytest.raises(SetupConflict):
            service.setup_records.begin(sid, ['CA'], kind='unified', seconds=30, policy_version='v1')
        cleanup_release.set()
        await asyncio.wait_for(controller.closed.wait(), 1)
        assert service.setup_records.get(sid, pid) == saved
    finally:
        cleanup_release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_second_cancel_during_cleanup_reaches_terminal_and_releases_public_wait(tmp_path):
    from ziniao_automation.ziniao.models import ProfileSelector
    cleanup_entered, cleanup_cancelled, operation_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class CancelCleanup(_Controller):
        @asynccontextmanager
        async def financial_session(self, selector, store_key=None):
            self.handle = type('Handle', (), {'cleanup_confirmed': False})()
            try:
                yield self.handle
            finally:
                cleanup_entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_cancelled.set()
    controller=CancelCleanup()
    service,_,sid=_service(tmp_path,controller)
    service.setup_cleanup_grace_seconds=.05
    row=service.setup_records.begin(sid,['CA'],kind='unified',seconds=30,policy_version='v1')
    pid=row['probe_id']
    async def operate():
        async with service._setup_session(pid,ProfileSelector('oauth','unified-setup-oauth'),sid):
            operation_entered.set()
            await asyncio.Event().wait()
    task=asyncio.create_task(service._run_setup_probe_before_deadline(expires_at=asyncio.get_running_loop().time()+2,
        cleanup_grace_seconds=.05,operation=operate,probe_id=pid))
    await asyncio.wait_for(operation_entered.wait(),1)
    task.cancel()
    await asyncio.wait_for(cleanup_entered.wait(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    await asyncio.wait_for(cleanup_cancelled.wait(),1)
    saved=service.setup_records.get(sid,pid)
    assert saved['lifecycle']=='FINISHED' and saved['outcome']=='CANCELLED'
    assert saved['cleanup_state']=='UNCONFIRMED'


@pytest.mark.asyncio
async def test_retry_bootstraps_identity_from_passed_site_without_rechecking_it(tmp_path, monkeypatch):
    from ziniao_automation.workflows.store_setup.identity import SetupRejected
    controller=_Controller()
    service,_,sid=_service(tmp_path,controller,seller_id='SELLER',confirmed=True)
    identity_calls,site_calls=[],[]
    retrying=False
    async def identity(self,page,marketplace):
        identity_calls.append(marketplace.code)
        if retrying and marketplace.code!='UK':
            raise SetupRejected('IDENTITY_CURRENT_NOT_FOUND')
        return 'SELLER','dom:content'
    async def check(self,page,store,marketplace):
        site_calls.append(marketplace.code)
        if marketplace.code!='UK':raise SetupRejected('REGISTRATION_REQUIRED')
        return SiteSetupResult('PASSED','PASSED','SUPPORTED','dom:content')
    monkeypatch.setattr('ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity',identity)
    monkeypatch.setattr('ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site',check)
    monkeypatch.setattr('ziniao_automation.workflows.runtime.AmazonStoreSetupPage.resolved_page_for',lambda self,page:page)
    first=await service.detect_store_setup(sid,['CA','UK','AU'])
    assert (await _terminal(service,sid,first['probe_id']))['outcome']=='PARTIAL'
    identity_calls.clear();site_calls.clear();retrying=True
    retry=await service.detect_store_setup(sid,['CA','UK','AU'],scope='retry_unverified')
    result=await _terminal(service,sid,retry['probe_id'])
    assert result['identity']['status']=='SUCCEEDED'
    assert identity_calls==['UK']
    assert site_calls==['CA','AU']
    assert service.setup_records.summary(sid)['marketplaces']['UK']['outcome']=='PASSED'
    assert controller.wait_for_auth_calls==0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["identity", "site"])
async def test_empty_otp_finishes_site_releases_store_and_preserves_saved_identity(tmp_path,monkeypatch,phase):
    from ziniao_automation.workflows.store_setup.identity import SetupRejected
    controller=_Controller()
    service,sessions,sid=_service(tmp_path,controller)
    async def identity(self,page,marketplace):
        if phase=="identity":
            raise SetupRejected("AUTH_OTP_NOT_FILLED",category="LOGIN",stage="otp_signin")
        return "BOUND-SELLER","fixture"
    visited=[]
    async def check(self,page,store,marketplace):
        visited.append(marketplace.code)
        if marketplace.code=="CA":
            raise SetupRejected("AUTH_OTP_NOT_FILLED",category="LOGIN",stage="otp_signin")
        return SiteSetupResult("PASSED","PASSED","UNKNOWN","fixture")
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.detect_identity",identity)
    monkeypatch.setattr("ziniao_automation.workflows.runtime.AmazonStoreSetupPage.check_site",check)
    result=await service.detect_store_setup(sid,["CA","UK"])
    final=await _terminal(service,sid,result["probe_id"])
    assert controller.wait_for_auth_calls==0
    assert controller.closed.is_set()
    assert final["cleanup_state"]=="CONFIRMED"
    if phase=="site":
        assert final["status"]=="PARTIAL"
        assert visited==["CA","UK"]
        assert final["identity"]["status"]=="SUCCEEDED"
        assert "卖家身份已保存" in final["message"]
    else:
        assert final["status"]=="FAILED"
    with sessions() as db:
        row=db.get(Store,sid)
        assert bool(row.expected_seller_id)==(phase=="site")
        assert list(db.scalars(select(OperationGuard)))==[]
