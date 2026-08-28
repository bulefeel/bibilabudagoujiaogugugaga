"""Minimal Windows Credential Manager access without extra packages."""

from __future__ import annotations

from collections.abc import Mapping
import hmac
import json
import os
from typing import Any

from .errors import ZiniaoError


class CredentialStoreError(ZiniaoError):
    """An opaque credential reference was not readable."""


# The Credential Manager entry names.  They live here rather than in ``cli``
# because the web console now writes the same two entries; keeping them next to
# the reader/writer stops the two callers from drifting onto different names and
# silently configuring nothing.
DEFAULT_ZINIAO_TARGET = "ziniao-automation/ziniao/main"
DEFAULT_FEISHU_TARGET = "ziniao-automation/feishu/main"
DEFAULT_AI_TARGET = "ziniao-automation/ai/main"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_CREDENTIAL_BLOB_BYTES = 2560
ALLOWED_FIELDS = frozenset(
    {
        "company",
        "username",
        "password",
        "app_id",
        "app_secret",
        "chat_id",
        # Cloud classifier key for the feedback removal workflow.
        "api_key",
    }
)


def read_generic_credential(target: str) -> dict[str, str]:
    """Resolve a generic credential without exposing its secret to logs.

    The credential blob may be a JSON object with company/username/password,
    or a plain password accompanied by Credential Manager's username field.
    """
    name = str(target).strip()
    if not name:
        raise ValueError("credential target must not be empty")
    if os.name != "nt":
        raise CredentialStoreError("Windows Credential Manager requires Windows")
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    pointer = ctypes.POINTER(CREDENTIALW)()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
    ]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    cred_free.restype = None
    if not cred_read(name, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        code = ctypes.get_last_error()
        raise CredentialStoreError(
            f"Windows credential reference is unreadable (error {code})"
        )
    try:
        record = pointer.contents
        blob = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
        secret = _decode_blob(blob)
        username = record.UserName or ""
    finally:
        cred_free(pointer)
    try:
        decoded: Any = json.loads(secret)
    except (json.JSONDecodeError, TypeError):
        decoded = None
    if isinstance(decoded, dict):
        return {
            str(key): str(value)
            for key, value in decoded.items()
            if value is not None
            and str(key) in ALLOWED_FIELDS
        }
    return {"username": str(username), "password": secret}


def write_generic_credential(
    target: str,
    secret: Mapping[str, str] | str,
    *,
    username: str = "ziniao-automation",
) -> None:
    """Create or replace a generic Windows credential.

    The caller receives no secret-bearing return value. The opaque blob is
    wiped from its mutable ctypes buffer after ``CredWriteW`` returns.
    """
    name = str(target).strip()
    if not name:
        raise ValueError("credential target must not be empty")
    if os.name != "nt":
        raise CredentialStoreError("Windows Credential Manager requires Windows")
    payload = _serialise_secret(secret)
    if not payload:
        raise ValueError("credential secret must not be empty")
    if len(payload) > MAX_CREDENTIAL_BLOB_BYTES:
        raise ValueError("credential secret is too large for Windows Credential Manager")

    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    buffer = ctypes.create_string_buffer(len(payload))
    ctypes.memmove(buffer, payload, len(payload))
    record = CREDENTIALW()
    record.Type = CRED_TYPE_GENERIC
    record.TargetName = name
    record.Comment = "Ziniao Automation local secret"
    record.CredentialBlobSize = len(payload)
    record.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    record.Persist = CRED_PERSIST_LOCAL_MACHINE
    record.UserName = str(username) or "ziniao-automation"
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    cred_write.restype = wintypes.BOOL
    try:
        if not cred_write(ctypes.byref(record), 0):
            code = ctypes.get_last_error()
            raise CredentialStoreError(f"Windows credential write failed (error {code})")
    finally:
        ctypes.memset(buffer, 0, len(payload))


def delete_generic_credential(target: str) -> bool:
    """Delete one named generic credential; return False when absent."""
    name = str(target).strip()
    if not name:
        raise ValueError("credential target must not be empty")
    if os.name != "nt":
        raise CredentialStoreError("Windows Credential Manager requires Windows")
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_delete = advapi32.CredDeleteW
    cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    cred_delete.restype = wintypes.BOOL
    if cred_delete(name, CRED_TYPE_GENERIC, 0):
        return True
    code = ctypes.get_last_error()
    if code == ERROR_NOT_FOUND:
        return False
    raise CredentialStoreError(f"Windows credential delete failed (error {code})")


def credential_exists(target: str) -> bool:
    try:
        resolved = read_generic_credential(target)
        return bool(resolved.get("password") or resolved.get("app_secret"))
    except (CredentialStoreError, ValueError):
        return False


def credential_matches(target: str, expected: Mapping[str, str]) -> bool:
    """Confirm that a just-written credential contains every expected value.

    Some endpoint-security products have been observed returning success from
    ``CredWriteW`` while leaving the previous record untouched.  Checking only
    that a password exists therefore produces a false success, especially when
    the operator changes just the password.  This helper compares in memory
    and never returns or logs either side.
    """

    try:
        actual = read_generic_credential(target)
    except (CredentialStoreError, ValueError, OSError):
        return False
    for key, value in expected.items():
        actual_value = actual.get(str(key))
        if actual_value is None or not hmac.compare_digest(
            str(actual_value).encode("utf-8"), str(value).encode("utf-8")
        ):
            return False
    return True


def _decode_blob(blob: bytes) -> str:
    if not blob:
        return ""
    if b"\x00" in blob:
        try:
            return blob.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            pass
    try:
        return blob.decode("utf-8").rstrip("\x00")
    except UnicodeDecodeError:
        return blob.decode("utf-16-le", errors="strict").rstrip("\x00")


def _serialise_secret(secret: Mapping[str, str] | str) -> bytes:
    if isinstance(secret, str):
        text = secret
    elif isinstance(secret, Mapping):
        clean = {
            str(key): str(value)
            for key, value in secret.items()
            if value is not None
        }
        text = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    else:
        raise TypeError("credential secret must be text or a mapping")
    return text.encode("utf-16-le")
