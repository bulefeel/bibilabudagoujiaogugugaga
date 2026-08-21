"""Application configuration with conservative, loopback-only defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    """Runtime settings; secrets live in Windows Credential Manager."""

    project_root: Path = field(default_factory=lambda: PROJECT_ROOT)
    data_dir: Path | None = None
    evidence_dir: Path | None = None
    log_dir: Path | None = None
    backup_dir: Path | None = None
    database_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    ziniao_executable: Path = Path(r"D:\紫鸟浏览器\ziniao\ziniao.exe")
    ziniao_host: str = "127.0.0.1"
    ziniao_port: int = 16851
    session_hours: int = 12
    auth_wait_minutes: int = 30
    artifact_retention_days: int = 90
    cookie_secure: bool = False
    create_schema_on_start: bool = True
    testing: bool = False

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).resolve()
        self.data_dir = Path(self.data_dir or self.project_root / "data").resolve()
        self.evidence_dir = Path(self.evidence_dir or self.data_dir / "evidence").resolve()
        self.log_dir = Path(self.log_dir or self.data_dir / "logs").resolve()
        self.backup_dir = Path(self.backup_dir or self.data_dir / "backups").resolve()
        self.ziniao_executable = Path(self.ziniao_executable)
        if self.host != "127.0.0.1":
            raise ValueError("管理后台只允许监听 127.0.0.1")
        if self.ziniao_host not in {"127.0.0.1", "localhost"}:
            raise ValueError("紫鸟控制接口必须使用本机回环地址")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("后台端口不合法")
        if not 1 <= int(self.ziniao_port) <= 65535:
            raise ValueError("紫鸟端口不合法")
        if self.auth_wait_minutes < 1:
            raise ValueError("人工验证等待时间至少为 1 分钟")
        if self.database_url is None:
            database_path = (self.data_dir / "ziniao-automation.db").as_posix()
            self.database_url = f"sqlite:///{database_path}"

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(os.getenv("ZINIAO_PROJECT_ROOT", str(PROJECT_ROOT)))
        # Probe for the real install instead of assuming Ziniao's default
        # directory.  Imported here rather than at module scope so the probe
        # runs only on this path — it touches the disk and the registry, and
        # ``Settings()`` is constructed constantly in the test suite.
        from .ziniao.locate import resolve_ziniao_executable

        executable = resolve_ziniao_executable(os.getenv("ZINIAO_EXECUTABLE"))
        return cls(
            project_root=root,
            data_dir=Path(os.environ["ZINIAO_DATA_DIR"]) if os.getenv("ZINIAO_DATA_DIR") else None,
            database_url=os.getenv("ZINIAO_DATABASE_URL") or None,
            host=os.getenv("ZINIAO_WEB_HOST", "127.0.0.1"),
            port=int(os.getenv("ZINIAO_WEB_PORT", "8765")),
            ziniao_executable=executable,
            ziniao_host=os.getenv("ZINIAO_CONTROL_HOST", "127.0.0.1"),
            ziniao_port=int(os.getenv("ZINIAO_CONTROL_PORT", "16851")),
            session_hours=int(os.getenv("ZINIAO_SESSION_HOURS", "12")),
            auth_wait_minutes=int(os.getenv("ZINIAO_AUTH_WAIT_MINUTES", "30")),
            artifact_retention_days=int(os.getenv("ZINIAO_RETENTION_DAYS", "90")),
            cookie_secure=_env_bool("ZINIAO_COOKIE_SECURE", False),
            create_schema_on_start=_env_bool("ZINIAO_CREATE_SCHEMA", True),
            testing=_env_bool("ZINIAO_TESTING", False),
        )

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.evidence_dir, self.log_dir, self.backup_dir):
            assert directory is not None
            directory.mkdir(parents=True, exist_ok=True)
