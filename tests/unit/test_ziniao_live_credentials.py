"""Credentials must be read when a call happens, not when the process booted."""

from __future__ import annotations

import httpx
import pytest

from ziniao_automation import composition
from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import ZiniaoAccount
from ziniao_automation.ziniao.client import ZiniaoClient, ZiniaoClientConfig
from ziniao_automation.ziniao.errors import ZiniaoApiError, ZiniaoCredentialError


def _transport(seen: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"statusCode": "0", "data": []})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_credentials_configured_after_startup_reach_the_next_call() -> None:
    """The console configures the account while the service is already up.

    The controller is built once at boot, so a value captured there is empty on
    exactly the first sync the operator attempts — Ziniao answers
    ``-10003 参数不能为空（登录状态错误）`` and the page blames a login that is
    actually configured.  Observed 2026-08-24 right after the credentials page
    shipped.
    """

    seen: list[dict] = []
    store: dict[str, str] = {}
    client = ZiniaoClient(
        ZiniaoClientConfig(company="", username="", password=""),
        transport=_transport(seen),
        credential_resolver=lambda: dict(store),
    )

    await client.get_browser_list()
    assert seen[-1]["company"] == "", "启动时确实什么都没有"

    # Operator saves the account in the console — no restart.
    store.update(company="某公司", username="u1", password="pw")
    await client.get_browser_list()

    assert seen[-1]["company"] == "某公司"
    assert seen[-1]["username"] == "u1"
    assert seen[-1]["password"] == "pw"
    await client.close()


@pytest.mark.asyncio
async def test_an_empty_resolver_leaves_the_startup_values_in_force() -> None:
    """No account row yet must not blank out a developer-supplied client."""

    seen: list[dict] = []
    client = ZiniaoClient(
        ZiniaoClientConfig(company="boot", username="boot-user", password="boot-pw"),
        transport=_transport(seen),
        credential_resolver=dict,
    )

    await client.get_browser_list()

    assert seen[-1]["company"] == "boot"
    assert seen[-1]["password"] == "boot-pw"
    await client.close()


@pytest.mark.asyncio
async def test_a_failing_resolver_falls_back_instead_of_breaking_every_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Credential Manager can be locked down; that must not brick the console."""

    seen: list[dict] = []

    def explode() -> dict[str, str]:
        raise OSError("credential store unavailable")

    client = ZiniaoClient(
        ZiniaoClientConfig(company="boot", username="boot-user", password="boot-pw"),
        transport=_transport(seen),
        credential_resolver=explode,
    )

    await client.get_browser_list()

    assert seen[-1]["company"] == "boot"
    assert "credential store unavailable" not in caplog.text
    await client.close()


@pytest.mark.asyncio
async def test_partial_live_mapping_falls_back_per_field() -> None:
    """A live read must not blank fields absent from the current record."""

    seen: list[dict] = []
    client = ZiniaoClient(
        ZiniaoClientConfig(
            company="boot-company",
            username="boot-user",
            password="boot-password",
        ),
        transport=_transport(seen),
        credential_resolver=lambda: {
            "company": "live-company",
            "password": "live-password",
        },
    )

    await client.get_browser_list()
    await client.close()

    assert seen[-1]["company"] == "live-company"
    assert seen[-1]["username"] == "boot-user"
    assert seen[-1]["password"] == "live-password"


@pytest.mark.asyncio
async def test_live_resolver_is_called_once_per_api_call() -> None:
    """The request and its error redaction share one credential snapshot."""

    seen: list[dict] = []
    calls = 0

    def resolve() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"company": "live-company", "username": "live-user", "password": "live-pw"}

    client = ZiniaoClient(
        ZiniaoClientConfig(company="boot-company", username="boot-user", password="boot-pw"),
        transport=_transport(seen),
        credential_resolver=resolve,
    )
    await client.get_browser_list()
    await client.close()

    assert calls == 1
    assert seen[-1]["company"] == "live-company"


@pytest.mark.asyncio
async def test_api_error_redacts_the_live_snapshot() -> None:
    """An API error may echo the credentials used by this specific call."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"statusCode": 42, "err": "password=LIVE_SECRET company=LIVE_COMPANY"},
        )

    client = ZiniaoClient(
        ZiniaoClientConfig(company="BOOT_COMPANY", password="BOOT_SECRET"),
        transport=httpx.MockTransport(handler),
        credential_resolver=lambda: {
            "company": "LIVE_COMPANY",
            "password": "LIVE_SECRET",
        },
    )
    with pytest.raises(ZiniaoApiError) as caught:
        await client.get_browser_list()
    await client.close()

    message = str(caught.value)
    assert "LIVE_SECRET" not in message
    assert "LIVE_COMPANY" not in message


@pytest.mark.asyncio
async def test_blank_live_fields_fall_back_to_startup_values() -> None:
    seen: list[dict] = []
    client = ZiniaoClient(
        ZiniaoClientConfig(company="boot-company", username="boot-user", password="boot-pw"),
        transport=_transport(seen),
        credential_resolver=lambda: {"company": "", "username": None, "password": ""},
    )
    await client.get_browser_list()
    await client.close()

    assert seen[-1]["company"] == "boot-company"
    assert seen[-1]["username"] == "boot-user"
    assert seen[-1]["password"] == "boot-pw"


def test_production_controller_observes_account_saved_after_composition(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the production wiring that caused the first-install -10003."""

    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'live-controller.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    captured: dict[str, object] = {}
    vault: dict[str, dict[str, str]] = {}

    def fake_build_controller(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(composition, "build_controller", fake_build_controller)
    monkeypatch.setattr(
        composition,
        "read_generic_credential",
        lambda reference: dict(vault[reference]),
    )

    composition._build_controller_from_database(settings, factory)
    resolver = captured["credential_resolver"]
    assert callable(resolver)
    assert captured["credential_resolver_authoritative"] is True
    with pytest.raises(ZiniaoCredentialError, match="尚未配置或已停用"):
        resolver()

    reference = "ziniao-automation/ziniao/main"
    vault[reference] = {
        "company": "live-company",
        "username": "live-user",
        "password": "live-password",
    }
    with factory() as session:
        session.add(
            ZiniaoAccount(
                display_name="main",
                company="live-company",
                username="live-user",
                credential_ref=reference,
                enabled=True,
            )
        )
        session.commit()

    assert resolver() == vault[reference]
    engine.dispose()


def test_production_resolver_fails_closed_when_account_is_disabled_or_deleted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'disabled-controller.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        composition,
        "build_controller",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    monkeypatch.setattr(
        composition,
        "read_generic_credential",
        lambda _: {
            "company": "old-company",
            "username": "old-user",
            "password": "old-password",
        },
    )
    with factory() as session:
        row = ZiniaoAccount(
            display_name="main",
            company="old-company",
            username="old-user",
            credential_ref="ziniao-ref",
            enabled=True,
        )
        session.add(row)
        session.commit()
        row_id = row.id

    composition._build_controller_from_database(settings, factory)
    resolver = captured["credential_resolver"]
    assert callable(resolver)
    assert resolver()["password"] == "old-password"

    with factory() as session:
        session.get(ZiniaoAccount, row_id).enabled = False
        session.commit()
    with pytest.raises(ZiniaoCredentialError, match="已停用"):
        resolver()

    with factory() as session:
        session.delete(session.get(ZiniaoAccount, row_id))
        session.commit()
    with pytest.raises(ZiniaoCredentialError, match="尚未配置"):
        resolver()
    engine.dispose()


@pytest.mark.parametrize(
    ("company", "username", "secret", "message"),
    [
        ("", "live-user", {"password": "pw"}, "不完整"),
        ("live-company", "", {"password": "pw"}, "不完整"),
        ("live-company", "live-user", {}, "不完整"),
        ("live-company", "live-user", {"password": "pw"}, "格式过旧"),
        (
            "live-company",
            "live-user",
            {"company": "live-company", "password": "pw"},
            "格式过旧",
        ),
        (
            "live-company",
            "live-user",
            {"company": "other-company", "username": "live-user", "password": "pw"},
            "不一致",
        ),
    ],
)
def test_production_resolver_rejects_incomplete_or_mismatched_snapshot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    company: str,
    username: str,
    secret: dict[str, str],
    message: str,
) -> None:
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / (message + company + '.db')).as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        composition,
        "build_controller",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    monkeypatch.setattr(composition, "read_generic_credential", lambda _: dict(secret))
    with factory() as session:
        session.add(
            ZiniaoAccount(
                display_name="main",
                company=company,
                username=username,
                credential_ref="ziniao-ref",
                enabled=True,
            )
        )
        session.commit()

    composition._build_controller_from_database(settings, factory)
    resolver = captured["credential_resolver"]
    with pytest.raises(ZiniaoCredentialError, match=message):
        resolver()
    engine.dispose()


def test_production_resolver_masks_credential_store_read_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'read-failure.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        composition,
        "build_controller",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    def explode(_: str) -> dict[str, str]:
        raise OSError("PRIVATE_STORE_DETAIL")

    monkeypatch.setattr(composition, "read_generic_credential", explode)
    with factory() as session:
        session.add(
            ZiniaoAccount(
                display_name="main",
                company="company",
                username="user",
                credential_ref="ziniao-ref",
                enabled=True,
            )
        )
        session.commit()

    composition._build_controller_from_database(settings, factory)
    resolver = captured["credential_resolver"]
    with pytest.raises(ZiniaoCredentialError) as caught:
        resolver()
    assert "PRIVATE_STORE_DETAIL" not in str(caught.value)
    engine.dispose()


@pytest.mark.asyncio
async def test_authoritative_resolver_never_falls_back_or_sends_partial_credentials() -> None:
    seen: list[dict] = []
    client = ZiniaoClient(
        ZiniaoClientConfig(
            company="old-company",
            username="old-user",
            password="old-password",
        ),
        transport=_transport(seen),
        credential_resolver=lambda: {
            "company": "new-company",
            "username": "new-user",
        },
        credential_resolver_authoritative=True,
    )

    with pytest.raises(ZiniaoCredentialError, match="不完整"):
        await client.get_browser_list()
    await client.close()
    assert seen == []


@pytest.mark.asyncio
async def test_authoritative_resolver_preserves_password_whitespace() -> None:
    seen: list[dict] = []
    client = ZiniaoClient(
        ZiniaoClientConfig(),
        transport=_transport(seen),
        credential_resolver=lambda: {
            "company": "company",
            "username": "user",
            "password": " password with edges ",
        },
        credential_resolver_authoritative=True,
    )

    await client.get_browser_list()
    await client.close()
    assert seen[-1]["password"] == " password with edges "


@pytest.mark.asyncio
async def test_credential_reference_preserves_startup_fields_when_blob_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older Windows blobs may omit metadata or the password field."""

    from ziniao_automation.ziniao import credentials as credential_store

    monkeypatch.setattr(
        credential_store,
        "read_generic_credential",
        lambda _: {"password": "stored-password"},
    )
    client = ZiniaoClient.from_credential_reference(
        ZiniaoClientConfig(
            company="boot-company",
            username="boot-user",
            password="boot-password",
        ),
        "ziniao-ref",
    )

    assert client.config.company == "boot-company"
    assert client.config.username == "boot-user"
    assert client.config.password == "stored-password"
    await client.close()
