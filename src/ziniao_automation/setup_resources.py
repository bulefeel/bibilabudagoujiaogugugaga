"""Read-only Windows process identity for confirming cleanup after a crash.

No process is terminated. Only PID + creation time are saved; no CDP URL,
command line, user-data path, credential, or browser storage is retained.
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess


def _read(script):
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            return None
        return json.loads(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


async def capture_process(handle):
    # Only the controller-issued live handle is accepted, never HTTP input.
    port = getattr(handle, "debugging_port", None)
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    script = ("$ErrorActionPreference='Stop'; $ids=@(Get-NetTCPConnection -State Listen -LocalPort "
              + str(port) + " | Select-Object -ExpandProperty OwningProcess -Unique); "
              "if($ids.Count -ne 1){exit 2}; $p=Get-Process -Id $ids[0]; "
              "@{pid=$p.Id;created=$p.StartTime.ToUniversalTime().Ticks.ToString()} | ConvertTo-Json -Compress")
    value = await asyncio.to_thread(_read, script)
    return value if isinstance(value, dict) and type(value.get("pid")) is int and str(value.get("created", "")).isdigit() else None


async def process_released(proof):
    if not isinstance(proof, dict) or type(proof.get("pid")) is not int or proof["pid"] <= 0 or not str(proof.get("created", "")).isdigit():
        return False
    script = ("$ErrorActionPreference='Stop'; $p=Get-Process -Id " + str(proof["pid"])
              + " -ErrorAction SilentlyContinue; if($null -eq $p){'true'}else{"
              + "($p.StartTime.ToUniversalTime().Ticks.ToString() -ne '" + str(proof["created"]) + "').ToString().ToLower()}")
    return await asyncio.to_thread(_read, script) is True
