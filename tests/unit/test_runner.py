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


def _log_settings(tmp_path: Path) -> SimpleNamespace:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return SimpleNamespace(log_dir=log_dir)


def test_last_error_lifts_the_newest_error_out_of_the_jsonl(tmp_path: Path) -> None:
    """Start.bat and the desktop launcher both need this line, not a file path.

    Telling someone who has never seen JSON to "查看 ziniao-automation.jsonl" is
    not an instruction they can follow.  The sentence they need is already in
    the file; the whole point of this helper is to put it on screen for them.
    """

    settings = _log_settings(tmp_path)
    (settings.log_dir / "ziniao-automation.jsonl").write_text(
        "\n".join(
            [
                '{"level":"INFO","message":"启动中"}',
                '{"level":"ERROR","message":"旧的错误"}',
                '{"level":"INFO","message":"又一条无关的"}',
                '{"level":"ERROR","message":"runner exited during startup",'
                '"exception_type":"OSError","exception_message":"端口被占用"}',
                '{"level":"WARNING","message":"这条不算错误"}',
            ]
        ),
        encoding="utf-8",
    )

    reported = runner.last_error(settings)

    assert "runner exited during startup" in reported
    assert "OSError" in reported
    assert "端口被占用" in reported
    assert "旧的错误" not in reported, "必须取最近的那条，不是第一条"


def test_last_error_is_empty_rather_than_misleading_when_nothing_is_wrong(
    tmp_path: Path,
) -> None:
    """An empty answer lets the caller say "日志里没有错误"，which is the truth.

    A placeholder string would be printed as though it were the cause, and the
    operator would go chasing it.
    """

    settings = _log_settings(tmp_path)
    (settings.log_dir / "ziniao-automation.jsonl").write_text(
        '{"level":"INFO","message":"一切正常"}\n', encoding="utf-8"
    )

    assert runner.last_error(settings) == ""


def test_last_error_survives_a_missing_or_unreadable_log(tmp_path: Path) -> None:
    """This runs on the failure path; it must never fail on top of the failure."""

    assert runner.last_error(_log_settings(tmp_path)) == ""


def test_last_error_reads_only_the_tail_of_a_large_log(tmp_path: Path) -> None:
    """The log is unbounded; reading it whole on every failed start is wasteful.

    Seeking mid-file can split a UTF-8 sequence, so the first (partial) line is
    dropped — this pins that the drop never eats the record we were after.
    """

    settings = _log_settings(tmp_path)
    filler = '{"level":"INFO","message":"%s"}' % ("填充" * 200)
    lines = [filler] * 400 + [
        '{"level":"ERROR","message":"最后的错误","exception_type":"RuntimeError"}'
    ]
    path = settings.log_dir / "ziniao-automation.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    assert path.stat().st_size > 64_000

    reported = runner.last_error(settings)

    assert "最后的错误" in reported
    assert "RuntimeError" in reported
