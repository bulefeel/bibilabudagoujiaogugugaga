"""Read-only local diagnostics for the administration page."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import ZiniaoClient
from .controller import is_ziniao_process_running


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checked_at: datetime
    healthy: bool
    checks: tuple[DoctorCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "healthy": self.healthy,
            "checks": [asdict(check) for check in self.checks],
        }


class ZiniaoDoctor:
    """Checks files, TCP, HTTP and credentials without opening a store."""

    def __init__(
        self,
        client: ZiniaoClient,
        *,
        client_path: Path,
        data_root: Path | None = None,
        credential_probe: Any | None = None,
    ) -> None:
        self.client = client
        self.client_path = Path(client_path)
        self.data_root = Path(data_root) if data_root else None
        self.credential_probe = credential_probe

    async def run(self) -> DoctorReport:
        # TCP and HTTP may each take a short timeout; run them concurrently so
        # the diagnostics page remains responsive when Ziniao is offline.
        port_check, api_check, credentials_check = await asyncio.gather(
            self._port_check(),
            self._api_check(),
            self._credentials_check(),
        )
        checks = [
            self._path_check(),
            self._process_check(),
            port_check,
            api_check,
            self._data_root_check(),
            credentials_check,
        ]
        required_names = {
            "ziniao_path",
            "ziniao_port",
            "ziniao_api",
            "data_root",
            "credentials",
        }
        healthy = all(check.ok for check in checks if check.name in required_names)
        return DoctorReport(
            checked_at=datetime.now(UTC),
            healthy=healthy,
            checks=tuple(checks),
        )

    def _path_check(self) -> DoctorCheck:
        ok = self.client_path.is_file()
        return DoctorCheck(
            name="ziniao_path",
            ok=ok,
            message="紫鸟程序路径有效" if ok else "紫鸟程序路径不存在",
            details={"path": str(self.client_path)},
        )

    def _process_check(self) -> DoctorCheck:
        ok = is_ziniao_process_running()
        return DoctorCheck(
            name="ziniao_process",
            ok=ok,
            message="紫鸟进程正在运行" if ok else "紫鸟进程当前未运行",
        )

    async def _port_check(self) -> DoctorCheck:
        host = self.client.config.host
        port = self.client.config.port
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=1.5,
            )
            writer.close()
            await writer.wait_closed()
            ok = True
        except (OSError, TimeoutError):
            ok = False
        return DoctorCheck(
            name="ziniao_port",
            ok=ok,
            message=f"{host}:{port} 可连接" if ok else f"{host}:{port} 未监听",
            details={"host": host, "port": port},
        )

    async def _api_check(self) -> DoctorCheck:
        try:
            response = await self.client.probe()
        except Exception as exc:
            return DoctorCheck("ziniao_api", False, "WebDriver API 无响应", {"error_type": type(exc).__name__})
        code = response.status_code
        if code == "0":
            message = "WebDriver API 正常"
            ok = True
        elif code == "-10003":
            message = "API 已响应，但账号权限或登录状态需要检查"
            ok = False
        else:
            message = f"API 返回状态码 {code or 'missing'}"
            ok = False
        # Never include the response body: old clients may echo credentials.
        return DoctorCheck("ziniao_api", ok, message, {"status_code": code})

    def _data_root_check(self) -> DoctorCheck:
        if self.data_root is None:
            return DoctorCheck("data_root", True, "未配置独立数据目录检查")
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
            probe = self.data_root / ".doctor-write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            ok = True
        except OSError:
            ok = False
        on_d_drive = self.data_root.drive.upper() == "D:"
        ok = ok and on_d_drive
        return DoctorCheck(
            "data_root",
            ok,
            "D 盘数据目录可写" if ok else "数据目录不可写或不在 D 盘",
            {"path": str(self.data_root)},
        )

    async def _credentials_check(self) -> DoctorCheck:
        if self.credential_probe is None:
            configured = bool(self.client.config.username and self.client.config.password)
            return DoctorCheck(
                "credentials",
                configured,
                "紫鸟凭据已配置" if configured else "紫鸟凭据尚未配置",
                {"source": "runtime"},
            )
        try:
            result = self.credential_probe()
            if hasattr(result, "__await__"):
                result = await result
            ok = bool(result)
        except Exception:
            ok = False
        return DoctorCheck(
            "credentials",
            ok,
            "Windows 凭据引用可读取" if ok else "Windows 凭据引用不可读取",
            {"source": "windows_credential_manager"},
        )
