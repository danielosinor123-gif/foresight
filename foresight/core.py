"""Foresight core bridge: Rust native core + Windows-specific collectors.

Division of labor (guided by the prediction):
  - Rust core (foresight-core.exe): snapshot/monitor/verify - the
    memory-safety-critical, always-running components.
  - This module: ecosystem collectors that need Python's reach - physical
    disk health, battery, accelerator inventory, the model-cache registry
    (seed of NPU orchestration) - plus a clean bridge to the Rust binary.
"""
from __future__ import annotations

import json
import os
import pathlib
import platform
import subprocess

import psutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE_EXE = ROOT / "foresight-core" / "target" / "release" / "foresight-core.exe"

IS_WINDOWS = platform.system() == "Windows"
FORESIGHT_HOME = pathlib.Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Foresight"
MODEL_CACHE = FORESIGHT_HOME / "model-cache"
LOG_DIR = FORESIGHT_HOME / "logs"


def core_available() -> bool:
    return CORE_EXE.exists()


def _require_core() -> None:
    if not core_available():
        raise RuntimeError(
            "foresight-core.exe not found. Build it with:\n"
            "  cd foresight-core\n"
            "  cargo build --release\n"
            "(if the build fails, strip WinLibs mingw from PATH first)"
        )


def snapshot() -> dict:
    """One full system snapshot from the Rust core."""
    _require_core()
    out = subprocess.run(
        [str(CORE_EXE), "snapshot"], capture_output=True, text=True, timeout=30
    )
    if out.returncode != 0:
        raise RuntimeError(f"foresight-core snapshot failed: {out.stderr[:500]}")
    return json.loads(out.stdout)


def verify_action(action: dict) -> dict:
    """Send a proposed action through the compiled Rust policy verifier."""
    _require_core()
    payload = json.dumps(action)
    out = subprocess.run(
        [str(CORE_EXE), "verify"], input=payload,
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0:
        return {
            "verdict": "rejected",
            "reasons": [f"verifier error: {out.stderr[:200]}"],
        }
    return json.loads(out.stdout)


def _powershell(cmd: str) -> str | None:
    if not IS_WINDOWS:
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
        return None
    except Exception:
        return None


def disk_health() -> list[dict]:
    """Physical disk health via Windows storage layer (SMART-based)."""
    raw = _powershell(
        "Get-PhysicalDisk | Select-Object FriendlyName, MediaType, HealthStatus, "
        "@{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}} | ConvertTo-Json"
    )
    if raw is None:
        return [{"status": "unavailable", "note": "Get-PhysicalDisk not accessible"}]
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return [
        {
            "name": d.get("FriendlyName", "disk"),
            "type": d.get("MediaType", "?"),
            "health": d.get("HealthStatus", "?"),
            "size_gb": d.get("SizeGB", 0),
        }
        for d in data
    ]


def battery() -> dict:
    bat = psutil.sensors_battery()
    if bat is None:
        return {"present": False}
    return {
        "present": True,
        "percent": round(bat.percent, 1),
        "plugged": bool(bat.power_plugged),
    }


def accelerators() -> list[dict]:
    """GPU/NPU inventory - the seed of the NPU-orchestration capability."""
    accels: list[dict] = []
    raw = _powershell(
        "Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, "
        "@{n='VRAM_GB';e={[math]::Round($_.AdapterRAM/1GB,1)}} | ConvertTo-Json"
    )
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            accels.extend(
                {
                    "kind": "gpu",
                    "name": d.get("Name", "?"),
                    "driver": d.get("DriverVersion", "?"),
                    "vram_gb": d.get("VRAM_GB", 0),
                }
                for d in data
            )
        except Exception:
            pass
    raw = _powershell(
        "Get-PnpDevice -PresentOnly | Where-Object {$_.FriendlyName -cmatch "
        "'NPU|Neural|AI Boost|Copilot'} | Select-Object FriendlyName, Status | ConvertTo-Json"
    )
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            accels.extend(
                {
                    "kind": "npu",
                    "name": d.get("FriendlyName", "?"),
                    "status": d.get("Status", "?"),
                }
                for d in data
                if d.get("FriendlyName")
            )
        except Exception:
            pass
    return accels


# ------------------------------------------------- model-cache registry
# (the seed of 'NPU / Accelerator Orchestration': OS-level, shared,
# per-accelerator model cache instead of per-app copies)

def model_cache_info() -> dict:
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    manifest_path = MODEL_CACHE / "manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps({"models": {}}, indent=2), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    total_mb = 0.0
    for meta in manifest["models"].values():
        total_mb += float(meta.get("size_mb", 0))
    return {
        "path": str(MODEL_CACHE),
        "entries": len(manifest["models"]),
        "total_mb": round(total_mb, 1),
    }


def register_model(name: str, size_mb: float, source: str = "local") -> dict:
    """Register a model in the shared accelerator cache."""
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    manifest_path = MODEL_CACHE / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["models"][name] = {"size_mb": size_mb, "source": source}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"registered": name, "size_mb": size_mb}


def log_action(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "actions.log", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def full_context() -> dict:
    """Snapshot + Windows collectors in one context dict."""
    snap = snapshot()
    return {
        "snapshot": snap,
        "disks_health": disk_health(),
        "battery": battery(),
        "accelerators": accelerators(),
        "model_cache": model_cache_info(),
    }
