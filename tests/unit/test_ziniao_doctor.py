from pathlib import Path
from types import SimpleNamespace

import pytest

from ziniao_automation.ziniao.doctor import ZiniaoDoctor


class Client:
    config = SimpleNamespace(host="127.0.0.1", port=16851, username="u", password="p")

    async def probe(self):
        return SimpleNamespace(status_code="0")


@pytest.mark.asyncio
async def test_doctor_never_returns_credentials(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "ziniao.exe"
    executable.write_bytes(b"")
    # Simulate the project's required D-drive data root on every test OS by
    # bypassing that platform-specific check below.
    doctor = ZiniaoDoctor(Client(), client_path=executable)

    async def port():
        from ziniao_automation.ziniao.doctor import DoctorCheck
        return DoctorCheck("ziniao_port", True, "ok")

    monkeypatch.setattr(doctor, "_port_check", port)
    report = await doctor.run()
    rendered = str(report.as_dict())
    assert "password" not in rendered.lower()
    assert "username" not in rendered.lower()
    assert report.healthy is True
