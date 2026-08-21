"""Find the installed Ziniao client without asking the operator to type a path.

The probe order and the registry fallback come from ``Ziniao-WebDriver.bat``,
which had this logic all along while ``Settings.ziniao_executable`` defaulted to
one hard-coded Chinese path.  Any machine that installed Ziniao somewhere else
therefore showed a red ``ziniao_path`` on the diagnostics page even though the
program was perfectly usable.

Nothing here runs at import time or inside ``Settings.__post_init__``: this
touches the disk and the registry, and putting it on every ``Settings()``
construction would slow the test suite down and make it depend on whatever is
installed on the machine running it.  ``Settings.from_env`` calls it once, only
when ``ZINIAO_EXECUTABLE`` is unset.
"""

from __future__ import annotations

import os
from pathlib import Path
import re

# Ziniao's own installer offers these; the first is its default.  Kept in
# probe order — the earliest hit wins.
_COMMON_PARENTS = (
    r"D:\紫鸟浏览器",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"D:\Program Files",
    r"D:",
)
_RELATIVE = Path("ziniao") / "ziniao.exe"

_UNINSTALL_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)
_ICON_SUFFIX = re.compile(r",\s*-?\d+\s*$")

DEFAULT_EXECUTABLE = Path(r"D:\紫鸟浏览器\ziniao\ziniao.exe")


def _candidates() -> list[Path]:
    found = [Path(parent) / _RELATIVE for parent in _COMMON_PARENTS]
    local = os.getenv("LOCALAPPDATA")
    if local:
        found.append(Path(local) / _RELATIVE)
    return found


def _from_uninstall_registry() -> Path | None:
    """Recover the path from whatever Ziniao told Windows at install time.

    Covers the installs that chose a directory nobody would guess.  Reads
    ``DisplayIcon`` because it points at the executable itself, while
    ``InstallLocation`` is frequently blank or a parent folder.
    """

    if os.name != "nt":
        return None
    import winreg

    roots = (
        (winreg.HKEY_CURRENT_USER, 0),
        (winreg.HKEY_LOCAL_MACHINE, 0),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    )
    for root, extra_access in roots:
        for key_path in _UNINSTALL_KEYS:
            try:
                key = winreg.OpenKey(
                    root, key_path, 0, winreg.KEY_READ | extra_access
                )
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, name) as entry:
                            icon, _ = winreg.QueryValueEx(entry, "DisplayIcon")
                    except OSError:
                        continue
                    text = _ICON_SUFFIX.sub("", str(icon or "").strip().strip('"'))
                    if not text.lower().endswith("ziniao.exe"):
                        continue
                    candidate = Path(text)
                    if candidate.is_file():
                        return candidate
    return None


def locate_ziniao_executable() -> Path | None:
    """The installed ``ziniao.exe``, or ``None`` when it cannot be found.

    Returning ``None`` rather than a guess is deliberate: the diagnostics page
    can then say "没找到，请手工填写" instead of showing a plausible-looking
    path that does not exist.
    """

    for candidate in _candidates():
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # A disconnected or permission-denied drive letter must not abort
            # the search; the next candidate may well be the right one.
            continue
    try:
        return _from_uninstall_registry()
    except Exception:
        # Registry shapes vary across Ziniao versions.  A failure to read one
        # is not a reason to fail the whole application start.
        return None


def resolve_ziniao_executable(override: str | Path | None = None) -> Path:
    """The path to use, preferring an explicit override, then a probe.

    Falls back to :data:`DEFAULT_EXECUTABLE` so callers always receive a Path;
    ``doctor``'s ``ziniao_path`` check then reports that it does not exist,
    which is the honest answer when nothing was found.
    """

    if override:
        return Path(override)
    return locate_ziniao_executable() or DEFAULT_EXECUTABLE
