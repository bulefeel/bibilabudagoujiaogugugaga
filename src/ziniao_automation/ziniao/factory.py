"""Composition helpers for wiring Settings/DB records to the adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .client import ZiniaoClient, ZiniaoClientConfig
from .controller import ZiniaoController, ZiniaoControllerConfig


def build_controller(
    *,
    host: str = "127.0.0.1",
    port: int = 16851,
    client_path: str | Path = Path(r"D:\紫鸟浏览器\ziniao\ziniao.exe"),
    company: str = "",
    username: str = "",
    password: str = "",
    credential_ref: str | None = None,
    credential_resolver: Callable[[], Mapping[str, str]] | None = None,
    credential_resolver_authoritative: bool = False,
    **controller_options: Any,
) -> ZiniaoController:
    """Construct the process-wide controller.

    When ``credential_ref`` is set, the secret is resolved from Windows
    Credential Manager and overrides the blank runtime password.

    ``credential_resolver`` is the live path: the controller is built once at
    startup, but the account is configured from the console afterwards, so any
    value captured here is stale for exactly the run the operator just set up.
    When supplied it is consulted on every call and wins over the values above.
    """
    client_config = ZiniaoClientConfig(
        host=host,
        port=port,
        company=company,
        username=username,
        password=password,
    )
    if credential_resolver is not None and credential_resolver_authoritative:
        # The live database/vault snapshot is the sole source of truth in the
        # installed service.  Reading ``credential_ref`` here would capture a
        # password that can become stale after the operator changes accounts.
        client = ZiniaoClient(
            client_config,
            credential_resolver=credential_resolver,
            credential_resolver_authoritative=True,
        )
    elif credential_ref:
        client = ZiniaoClient.from_credential_reference(
            client_config,
            credential_ref,
            credential_resolver=credential_resolver,
            credential_resolver_authoritative=credential_resolver_authoritative,
        )
    else:
        client = ZiniaoClient(
            client_config,
            credential_resolver=credential_resolver,
            credential_resolver_authoritative=credential_resolver_authoritative,
        )
    controller_config = ZiniaoControllerConfig(client_path=Path(client_path))
    return ZiniaoController(
        client,
        config=controller_config,
        **controller_options,
    )
