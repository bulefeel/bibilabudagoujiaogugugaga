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
        self.handle = type("Handle", (), {"page": self.page})()
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
            name="Unified Setup Store",
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

    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonPaymentsPage.detect_identity",
        identity,
    )
    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonPaymentsPage.preflight",
        preflight,
    )
    monkeypatch.setattr(
        "ziniao_automation.workflows.runtime.AmazonPaymentsPage.resolved_page_for",
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
        "ziniao_automation.workflows.runtime.AmazonPaymentsPage.detect_identity",
        hung_identity,
    )

    started = await service.detect_store_setup(store_id, ["CA", "UK"])
    await asyncio.wait_for(cdp_started.wait(), timeout=1.0)
    await asyncio.wait_for(controller.closed.wait(), timeout=1.0)
    result = await _terminal(service, store_id, started["probe_id"])

    assert cdp_cancelled.is_set()
    assert controller.opens == 1
    assert result["status"] == "FAILED"
    assert "30 分钟硬截止" in result["message"]
    assert result["identity"]["status"] == "FAILED"
    assert "30 分钟硬截止" in result["identity"]["message"]
    assert {site["status"] for site in result["marketplaces"]} == {"FAILED"}
    assert all(
        "30 分钟硬截止" in site["message"] for site in result["marketplaces"]
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
        "ziniao_automation.workflows.runtime.AmazonPaymentsPage.detect_identity",
        identity,
    )
    first = await service.detect_store_setup(store_id, ["CA"])
    attached = await service.detect_store_setup(store_id, ["UK"])

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


