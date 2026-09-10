"""Application and build identity.

``__version__`` is the only hand-edited release number in the repository.
Setuptools reads it for package metadata, while the installer build script
passes the same value to Inno Setup.  A release build also writes
``build-info.json`` beside the source tree so an installed copy can identify
the exact Git commit without shipping ``.git``.
"""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import subprocess


__version__ = "0.6.3"

_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _valid_commit(value: object) -> str | None:
    commit = str(value or "").strip()
    return commit[:12].lower() if _COMMIT_PATTERN.fullmatch(commit) else None


@lru_cache(maxsize=8)
def build_commit(project_root: str | Path | None = None) -> str:
    """Return the release Git commit without trusting arbitrary display text."""

    configured = _valid_commit(os.getenv("ZINIAO_BUILD_COMMIT"))
    if configured:
        return configured

    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        metadata = json.loads((root / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        metadata = {}
    packaged = _valid_commit(metadata.get("commit") if isinstance(metadata, dict) else None)
    if packaged:
        return packaged

    # Developer checkout fallback.  Installed packages do not include .git and
    # take the inexpensive build-info path above.
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return _valid_commit(completed.stdout) or "unknown"

