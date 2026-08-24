"""Ziniao local WebDriver integration."""

from .client import ZiniaoClient, ZiniaoClientConfig
from .controller import ZiniaoController, ZiniaoControllerConfig
from .credentials import (
    CredentialStoreError,
    credential_exists,
    credential_matches,
    delete_generic_credential,
    read_generic_credential,
    write_generic_credential,
)
from .doctor import DoctorReport, ZiniaoDoctor
from .errors import (
    AuthWaitCancelled,
    AuthWaitExpired,
    CdpHealthError,
    ZiniaoApiError,
    ZiniaoConnectionError,
    ZiniaoCredentialError,
    ZiniaoError,
    ZiniaoLaunchError,
)
from .factory import build_controller
from .locks import ExecutionLocks
from .models import (
    BrowserProfile,
    CdpHealth,
    ProfileSelector,
    ZiniaoBrowserHandle,
    ZiniaoLaunchProof,
)

__all__ = [
    "BrowserProfile",
    "AuthWaitCancelled",
    "AuthWaitExpired",
    "CdpHealth",
    "CdpHealthError",
    "CredentialStoreError",
    "DoctorReport",
    "ExecutionLocks",
    "ProfileSelector",
    "ZiniaoApiError",
    "ZiniaoBrowserHandle",
    "ZiniaoClient",
    "ZiniaoClientConfig",
    "ZiniaoConnectionError",
    "ZiniaoCredentialError",
    "ZiniaoController",
    "ZiniaoControllerConfig",
    "ZiniaoDoctor",
    "ZiniaoError",
    "ZiniaoLaunchError",
    "ZiniaoLaunchProof",
    "credential_exists",
    "credential_matches",
    "delete_generic_credential",
    "read_generic_credential",
    "write_generic_credential",
    "build_controller",
]
