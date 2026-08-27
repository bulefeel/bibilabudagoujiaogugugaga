from __future__ import annotations

import json
from pathlib import Path
import tomllib

from ziniao_automation import __version__
from ziniao_automation.version import build_commit


ROOT = Path(__file__).resolve().parents[2]


def test_release_number_has_one_hand_edited_source() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    installer = (ROOT / "installer/ziniao-automation.iss").read_text(encoding="utf-8")

    assert __version__ == "0.3.0"
    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "ziniao_automation.version.__version__"
    }
    assert project["build-system"]["requires"] == [
        "setuptools==84.0.0",
        "wheel==0.48.0",
    ]
    assert '#define AppVersion "' not in installer
    assert "#ifndef AppVersion" in installer


def test_packaged_build_info_supplies_the_diagnostic_commit(tmp_path: Path) -> None:
    (tmp_path / "build-info.json").write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "commit": "0123456789abcdef0123456789abcdef01234567",
            }
        ),
        encoding="utf-8",
    )
    build_commit.cache_clear()

    assert build_commit(tmp_path) == "0123456789ab"


def test_release_builder_emits_traceability_files_and_uses_the_lock() -> None:
    builder_path = ROOT / "installer/build.ps1"
    builder = builder_path.read_text(encoding="utf-8-sig")

    for required in (
        "Assert-CleanGitCheckout",
        "uv lock --check",
        "build-info.json",
        "Get-FileHash",
        "release-{0}.json",
        "ZiniaoAutomation-Setup-{0}.sha256",
        "/DAppVersion={0}",
        "Read-ToolchainDefinition",
        "Assert-InstallScriptPythonVersion",
        "Assert-ChildPath",
        "Ensure-VerifiedUv",
        "Assert-UvHash",
        "Invoke-WebRequest",
        "ZipFileExtensions]::ExtractToFile",
        "toolchain = [ordered]@{",
        "$env:PYTHONPATH = Join-Path $StageDir 'src'",
        "ZINIAO_RELEASE_STAGE_SRC",
        "actual.is_relative_to(expected)",
        "release-test-venv",
        "release-test-tmp",
        "release-test-cache",
        "sync --locked --extra dev --no-install-project",
        "python install ([string]$Toolchain.python_version)",
        "-m pytest",
        "--basetemp $releaseTestTemp",
        'cache_dir={0}',
        "暂存源码完整测试失败",
    ):
        assert required in builder
    assert builder.count("Remove-StageGeneratedCaches $StageDir") == 2
    assert builder.count("Assert-StageSafe $StageDir") == 2
    assert "暂存源码测试在 stage\\data 中生成了运行数据" in builder
    assert builder_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert (ROOT / "uv.lock").is_file()


def test_release_toolchain_is_pinned_and_staged() -> None:
    toolchain_path = ROOT / "installer/toolchain.json"
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    builder = (ROOT / "installer/build.ps1").read_text(encoding="utf-8-sig")

    assert toolchain == {
        "schema_version": 1,
        "python_version": "3.12.8",
        "uv": {
            "version": "0.5.18",
            "url": (
                "https://github.com/astral-sh/uv/releases/download/0.5.18/"
                "uv-x86_64-pc-windows-msvc.zip"
            ),
            "sha256": "1DDE041D07139CF92ABB0074BFC4E725C5568EEE31E9A4DDA53350605D0EB1AA",
        },
    }
    assert "'installer\\toolchain.json'" in builder
    assert "definition_sha256" in builder
    assert "Assert-InstallScriptPythonVersion $Toolchain" in builder


def test_installer_uses_the_lock_and_directs_beginners_to_web_settings() -> None:
    install = (ROOT / "Install.bat").read_text(encoding="utf-8-sig")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "sync --locked --no-dev" in install
    assert 'set "PYTHON_VERSION=3.12.8"' in install
    assert "python install %PYTHON_VERSION%" in install
    assert "venv --python %PYTHON_VERSION%" in install
    assert "sys.version_info[:3] == (3, 12, 8)" in install
    assert "init_database(e, backup_dir=s.backup_dir)" in install
    assert "系统诊断" in install
    assert "目前仍需命令行" not in readme
    assert "不需要重启后台" in readme


def test_inno_aborts_when_environment_install_fails() -> None:
    installer = (ROOT / "installer/ziniao-automation.iss").read_text(encoding="utf-8")
    run_section = installer.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]
    code_section = installer.split("[Code]", 1)[1]

    assert "Install.bat" not in run_section
    assert "procedure CurStepChanged(CurStep: TSetupStep);" in code_section
    assert "ewWaitUntilTerminated" in code_section
    assert "ResultCode <> 0" in code_section
    assert "RaiseException" in code_section
    assert "Install.bat" in code_section
