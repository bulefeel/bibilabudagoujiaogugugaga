from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ziniao_automation.config import Settings
from ziniao_automation.models import SystemSetting, ZiniaoAccount
from ziniao_automation.web import create_app


def test_diagnostics_distinguishes_registered_from_readable_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'diagnostics.db').as_posix()}",
        testing=True,
    )
    monkeypatch.setattr(
        "ziniao_automation.web.credential_exists",
        lambda target: target == "feishu-readable",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.sessions() as session:
            session.add(
                ZiniaoAccount(
                    display_name="account",
                    credential_ref="ziniao-missing",
                )
            )
            session.add(
                SystemSetting(
                    key="feishu",
                    value={
                        "enabled": True,
                        "credential_ref": "feishu-readable",
                    },
                )
            )
            session.commit()
        response = client.post(
            "/auth/bootstrap",
            json={
                "username": "operator",
                "password": "Local-Ledger-2026!",
                "confirm_password": "Local-Ledger-2026!",
            },
        )
        assert response.status_code == 200
        page = client.get("/diagnostics")
        assert page.status_code == 200
        assert 'diag-icon">ZN' in page.text
        assert 'diag-icon">FS' in page.text
        assert 'class="bad"' in page.text
        assert 'class="ok"' in page.text
