"""ASGI entry point."""

from .composition import build_runtime
from .config import Settings
from .logging_config import configure_logging, purge_expired_artifacts
from .web import create_app

settings = Settings.from_env()
configure_logging(
    settings.log_dir,
    retention_days=settings.artifact_retention_days,
)
purge_expired_artifacts(
    tuple(
        path
        for path in (settings.evidence_dir, settings.log_dir, settings.backup_dir)
        if path is not None
    ),
    retention_days=settings.artifact_retention_days,
)
app = create_app(settings, runtime_factory=build_runtime)
