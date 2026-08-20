from pathlib import Path
from types import SimpleNamespace

import ziniao_automation.runner as runner

from ziniao_automation.runner import _clear_codex_parent_markers, _read_pid, _write_pid


def test_pid_file_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "server.pid"
    _write_pid(target, 123)
    assert _read_pid(target) == 123


def test_invalid_pid_file_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "server.pid"
    target.write_text("not-a-pid", encoding="ascii")
    assert _read_pid(target) is None


def test_codex_parent_markers_are_not_inherited_by_service(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_SHELL", "1")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")
    monkeypatch.setenv("UNRELATED_LOCAL_SETTING", "kept")

    _clear_codex_parent_markers()

    import os

    assert "CODEX_SHELL" not in os.environ
    assert "CODEX_THREAD_ID" not in os.environ
    assert os.environ["UNRELATED_LOCAL_SETTING"] == "kept"


def test_stop_force_kills_only_the_verified_project_pid(
    tmp_path: Path, monkeypatch
) -> None:
    settings = SimpleNamespace(data_dir=tmp_path, project_root=tmp_path)
    pid_file = tmp_path / "run" / "server.pid"
    _write_pid(pid_file, 321)
    observed_commands: list[list[str]] = []

    monkeypatch.setattr(
        runner,
        "Settings",
        SimpleNamespace(from_env=lambda: settings),
    )
    monkeypatch.setattr(
        runner,
        "_executable_for_pid",
        lambda pid: tmp_path / ".venv" / "Scripts" / "pythonw.exe",
    )
    monkeypatch.setattr(runner, "_is_managed_python", lambda path, current: True)

    def deny_sigterm(pid: int, sig: int) -> None:
        raise PermissionError("fixture Windows pythonw denial")

    def record_taskkill(command, **kwargs):
        observed_commands.append(list(command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.os, "kill", deny_sigterm)
    monkeypatch.setattr("subprocess.run", record_taskkill)

    assert runner.stop() == 0
    assert observed_commands == [["taskkill.exe", "/PID", "321", "/F"]]
    assert not pid_file.exists()
