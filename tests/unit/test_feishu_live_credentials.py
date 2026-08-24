from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from ziniao_automation.config import Settings
from ziniao_automation.db import (
    create_sqlite_engine,
    init_database,
    make_session_factory,
)
from ziniao_automation.models import (
    NotificationDelivery,
    Run,
    SiteRun,
    Store,
    StoreMarketplace,
    SystemSetting,
)
from ziniao_automation.notifications import (
    DatabaseFeishuCredentialProvider,
    FeishuNotifier,
    NotificationDeliveryService,
    NotificationKind,
    SafeRunNotice,
)


@pytest.fixture()
def database(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        evidence_dir=tmp_path / "evidence",
        database_url=f"sqlite:///{(tmp_path / 'feishu-live.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


def test_database_provider_observes_configuration_saved_after_startup(database) -> None:
    vault: dict[str, dict[str, str]] = {}
    provider = DatabaseFeishuCredentialProvider(
        database, lambda reference: dict(vault[reference])
    )

    # The process-wide sender can be built before first-run setup.
    assert provider() is None

    reference = "ziniao-automation/feishu/main"
    vault[reference] = {"app_secret": "SECRET"}
    with database() as session:
        session.add(
            SystemSetting(
                key="feishu",
                value={
                    "enabled": True,
                    "credential_ref": reference,
                    "app_id": "APP_FROM_DB",
                    "chat_id": "CHAT_FROM_DB",
                },
            )
        )
        session.commit()

    assert provider() == {
        "app_id": "APP_FROM_DB",
        "app_secret": "SECRET",
        "chat_id": "CHAT_FROM_DB",
    }

    # Both stores are read again rather than being captured at construction.
    vault[reference] = {
        "app_id": "APP_FROM_VAULT",
        "app_secret": "NEW_SECRET",
        "chat_id": "CHAT_FROM_VAULT",
    }
    assert provider() == {
        "app_id": "APP_FROM_VAULT",
        "app_secret": "NEW_SECRET",
        "chat_id": "CHAT_FROM_VAULT",
    }


def test_database_provider_contains_an_unreadable_credential(database) -> None:
    with database() as session:
        session.add(
            SystemSetting(
                key="feishu",
                value={
                    "enabled": True,
                    "credential_ref": "missing-reference",
                    "app_id": "APP",
                    "chat_id": "CHAT",
                },
            )
        )
        session.commit()

    def unavailable(_: str):
        raise OSError("local credential store unavailable")

    provider = DatabaseFeishuCredentialProvider(database, unavailable)
    assert provider() is None


@pytest.mark.asyncio
async def test_delivery_service_built_before_setup_uses_the_next_saved_config(
    database,
) -> None:
    vault: dict[str, dict[str, str]] = {}
    provider = DatabaseFeishuCredentialProvider(
        database, lambda reference: dict(vault[reference])
    )
    sent_to: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "TOKEN", "expire": 7200},
            )
        sent_to.append(payload["receive_id"])
        return httpx.Response(200, json={"code": 0})

    service = NotificationDeliveryService(
        database,
        FeishuNotifier(provider, transport=httpx.MockTransport(handler)),
    )
    with database() as session:
        store = Store(
            name="live settings store",
            selector_type="oauth",
            selector_value="live-settings-oauth",
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
            store_id=store.id,
            workflow="amazon_disbursement",
            mode="dry_run",
            trigger="manual",
            status="SUCCEEDED",
            requested_by="admin",
        )
        session.add(run)
        session.flush()
        session.add(
            SiteRun(
                run_id=run.id,
                marketplace_id=market.id,
                marketplace_code="CA",
                status="SUCCEEDED",
                currency="CAD",
            )
        )
        session.commit()
        run_id = run.id

    # Missing setup is contained by the delivery boundary, not propagated to
    # the workflow status, and the receipt remains retryable.
    assert not await service.deliver(run_id, NotificationKind.RUN_COMPLETED)
    with database() as session:
        receipt = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.run_id == run_id)
        )
        assert receipt is not None
        assert receipt.status == "FAILED"
        assert receipt.attempts == 1
        assert session.get(Run, run_id).status == "SUCCEEDED"

    reference = "ziniao-automation/feishu/main"
    vault[reference] = {
        "app_id": "APP",
        "app_secret": "SECRET",
        "chat_id": "CHAT_AFTER_SAVE",
    }
    with database() as session:
        session.add(
            SystemSetting(
                key="feishu",
                value={
                    "enabled": True,
                    "credential_ref": reference,
                    "app_id": "APP",
                    "chat_id": "CHAT_AFTER_SAVE",
                },
            )
        )
        session.commit()

    assert await service.deliver(run_id, NotificationKind.RUN_COMPLETED)
    assert sent_to == ["CHAT_AFTER_SAVE"]
    with database() as session:
        receipt = session.scalar(
            select(NotificationDelivery).where(NotificationDelivery.run_id == run_id)
        )
        assert receipt.status == "SENT"
        assert receipt.attempts == 2
        assert session.get(Run, run_id).status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_notifier_refreshes_cached_token_when_live_app_credentials_change() -> None:
    current = {
        "app_id": "APP_ONE",
        "app_secret": "SECRET_ONE",
        "chat_id": "CHAT_ONE",
    }
    token_requests: list[dict[str, str]] = []
    messages: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path.endswith("tenant_access_token/internal"):
            token_requests.append(payload)
            token = f"TOKEN_{payload['app_id']}"
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": token, "expire": 7200},
            )
        messages.append((request.headers["authorization"], payload["receive_id"]))
        return httpx.Response(200, json={"code": 0})

    notifier = FeishuNotifier(
        lambda: dict(current), transport=httpx.MockTransport(handler)
    )
    notice = SafeRunNotice(
        kind=NotificationKind.RUN_COMPLETED,
        title="configuration refresh",
        summary="test",
        run_short_id="TEST",
        store_name="test store",
    )

    await notifier.send(notice)
    await notifier.send(notice)
    current.update(
        app_id="APP_TWO", app_secret="SECRET_TWO", chat_id="CHAT_TWO"
    )
    await notifier.send(notice)

    assert token_requests == [
        {"app_id": "APP_ONE", "app_secret": "SECRET_ONE"},
        {"app_id": "APP_TWO", "app_secret": "SECRET_TWO"},
    ]
    assert messages == [
        ("Bearer TOKEN_APP_ONE", "CHAT_ONE"),
        ("Bearer TOKEN_APP_ONE", "CHAT_ONE"),
        ("Bearer TOKEN_APP_TWO", "CHAT_TWO"),
    ]
