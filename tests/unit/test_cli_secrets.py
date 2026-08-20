from pathlib import Path

import pytest

from ziniao_automation import cli
from ziniao_automation.config import Settings


def test_configure_feishu_never_prints_secret(monkeypatch, capsys, tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data")
    values = iter(["APP_ID", "oc_chat"])
    monkeypatch.setattr("builtins.input", lambda _: next(values))
    monkeypatch.setattr(cli, "_confirmed_secret", lambda *args, **kwargs: "APP_SECRET")
    written = {}
    monkeypatch.setattr(cli, "write_generic_credential", lambda target, secret, username: written.update(secret))
    monkeypatch.setattr(cli, "credential_exists", lambda target: True)
    monkeypatch.setattr(cli, "_database", lambda _: (_Engine(), _Sessions()))
    assert cli._configure_feishu(settings, "credential/ref") == 0
    output = capsys.readouterr().out
    assert "APP_SECRET" not in output
    assert written["app_secret"] == "APP_SECRET"


def test_repair_feishu_reuses_non_secret_database_metadata(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(
        cli,
        "_read_feishu_metadata",
        lambda _: {
            "app_id": "SAVED_APP_ID",
            "chat_id": "SAVED_CHAT_ID",
            "credential_ref": "saved/credential/ref",
        },
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _: (_ for _ in ()).throw(AssertionError("metadata must be reused")),
    )
    monkeypatch.setattr(
        cli, "_confirmed_secret", lambda *args, **kwargs: "NEW_SECRET"
    )
    written = {}
    monkeypatch.setattr(
        cli,
        "write_generic_credential",
        lambda target, secret, username: written.update(
            {"target": target, "secret": secret, "username": username}
        ),
    )
    monkeypatch.setattr(cli, "credential_exists", lambda target: True)
    monkeypatch.setattr(cli, "_database", lambda _: (_Engine(), _Sessions()))

    assert cli._configure_feishu(
        settings, "default/ref", reuse_metadata=True
    ) == 0

    assert written["target"] == "saved/credential/ref"
    assert written["username"] == "SAVED_APP_ID"
    assert written["secret"] == {
        "app_id": "SAVED_APP_ID",
        "app_secret": "NEW_SECRET",
        "chat_id": "SAVED_CHAT_ID",
    }
    assert "NEW_SECRET" not in capsys.readouterr().out


class _Engine:
    def dispose(self):
        pass


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, *args):
        return None

    def add(self, value):
        self.value = value

    def commit(self):
        pass


class _Sessions:
    def __call__(self):
        return _Session()
