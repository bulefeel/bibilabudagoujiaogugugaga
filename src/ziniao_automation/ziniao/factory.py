"""Composition helpers for wiring Settings/DB records to the adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    **controller_options: Any,
) -> ZiniaoController:
    """Construct the process-wide controller.

    When ``credential_ref`` is set, the secret is resolved from Windows
    Credential Manager and overrides the blank runtime password.
    """
    client_config = ZiniaoClientConfig(
        host=host,
        port=port,
        company=company,
        username=username,
        password=password,
    )
    if credential_ref:
        client = ZiniaoClient.from_credential_reference(client_config, credential_ref)
    else:
        client = ZiniaoClient(client_config)
    controller_config = ZiniaoControllerConfig(client_path=Path(client_path))
    return ZiniaoController(
        client,
        config=controller_config,
        **controller_options,
    )
