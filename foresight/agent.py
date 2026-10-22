"""Decision layer: recommendations -> Rust verifier -> guarded execution.

The loop that mirrors the eBPF model in userland:
    1. observe telemetry (Rust core)
    2. propose an action (this module)
    3. VERIFY it against the compiled policy (Rust core)
    4. only then execute, and always log

Nothing in this module executes anything without a verdict == approved
from the Rust verifier. Demotions only; the v1 policy set has no kill.
"""
from __future__ import annotations

import os
import time

import psutil

from . import core

BATCHY = {"batch", "ml"}


def _self_pids() -> set[int]:
    me = os.getpid()
    parent = psutil.Process(me).ppid() if me > 0 else 0
    return {me, parent}


def build_recommendations(ctx: dict) -> list[dict]:
    """Turn live telemetry into proposed actions (all must be verified)."""
    snap = ctx["snapshot"]
    recs: list[dict] = []
    self_pids = _self_pids()
    ts = snap.get("ts", int(time.time() * 1000))

    # 1) workload-aware scheduling: demote sustained compute-heavy
    #    non-interactive processes (AI-aware scheduling prediction #7)
    for p in snap.get("processes", []):
        pid = p.get("pid", 0)
        if pid in self_pids:
            continue
        cls = p.get("class", "unknown")
        cpu = p.get("cpu_pct", 0)
        if cls in BATCHY and cpu >= 55.0:
            recs.append(
                {
                    "action": "set_priority",
                    "pid": pid,
                    "name": p.get("name", "?"),
                    "priority": "below_normal",
                    "reason": (
                        f"{p.get('name')} is classified {cls} and is consuming "
                        f"{cpu:.0f}% CPU; demoting to below-normal keeps the "
                        f"interactive experience smooth without stopping the work."
                    ),
                    "evidence": {
                        "cpu_pct": cpu,
                        "class": cls,
                        "class_reason": p.get("class_reason", ""),
                        "mem_mb": p.get("mem_mb", 0),
                        "ts": ts,
                    },
                }
            )

    # 2) energy-adaptive advisory (prediction #6): on battery, heavy
    #    compute should be deferred, not just demoted
    bat = ctx.get("battery", {})
    compute_load = sum(
        p.get("cpu_pct", 0) for p in snap.get("processes", [])
        if p.get("class") in BATCHY
    )
    if bat.get("present") and not bat.get("plugged") and compute_load > 40.0:
        recs.append(
            {
                "action": "noop",
                "reason": (
                    f"On battery ({bat.get('percent')}%) with {compute_load:.0f}% "
                    f"CPU in batch/ML workloads - defer non-urgent compute to "
                    f"mains power (energy-adaptive policy)."
                ),
                "evidence": {
                    "battery_pct": bat.get("percent"),
                    "batch_cpu_total": compute_load,
                    "ts": ts,
                },
            }
        )

    # 3) memory-pressure warning (self-healing signals, prediction #5)
    mem_pct = snap.get("mem", {}).get("used_pct", 0)
    if mem_pct >= 88.0:
        recs.append(
            {
                "action": "warn",
                "reason": (
                    f"Memory pressure at {mem_pct:.0f}% - close heavy apps or "
                    f"free memory before the system starts swapping."
                ),
                "evidence": {"mem_used_pct": mem_pct, "ts": ts},
            }
        )

    # 4) disk-pressure warning (predictive health, prediction #9)
    for d in snap.get("disks", []):
        if d.get("used_pct", 0) >= 90.0:
            recs.append(
                {
                    "action": "warn",
                    "reason": (
                        f"Disk {d.get('mount')} is {d.get('used_pct'):.0f}% full "
                        f"({d.get('free_gb')} GB free) - clean up large files soon."
                    ),
                    "evidence": {
                        "mount": d.get("mount"),
                        "used_pct": d.get("used_pct"),
                        "ts": ts,
                    },
                }
            )

    return recs


def apply_priority(pid: int, priority: str) -> dict:
    """Execute a verified set_priority action via psutil (Windows)."""
    try:
        p = psutil.Process(pid)
        mapping = {
            "below_normal": getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", None),
            "idle": getattr(psutil, "IDLE_PRIORITY_CLASS", None),
        }
        value = mapping.get(priority)
        if value is None:
            return {"applied": False, "note": f"priority '{priority}' not supported on this OS"}
        p.nice(value)
        return {"applied": True, "pid": pid, "priority": priority}
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        return {"applied": False, "note": str(e)}


def run_agent(execute: bool = False, ctx: dict | None = None) -> dict:
    """Full agent loop: observe -> recommend -> verify -> (execute) -> log."""
    ctx = ctx or core.full_context()
    recs = build_recommendations(ctx)

    results = []
    for rec in recs:
        verdict = core.verify_action(rec)
        applied = None
        if verdict.get("verdict") == "approved" and execute and rec["action"] == "set_priority":
            applied = apply_priority(rec["pid"], rec.get("priority", "below_normal"))
        results.append({"recommendation": rec, "verdict": verdict, "applied": applied})
        core.log_action(
            {
                "ts": int(time.time() * 1000),
                "recommendation": rec,
                "verdict": verdict,
                "executed": bool(applied and applied.get("applied")),
            }
        )

    return {
        "context_summary": {
            "cpu_pct": ctx["snapshot"].get("cpu_total_pct"),
            "mem_pct": ctx["snapshot"].get("mem", {}).get("used_pct"),
            "battery": ctx.get("battery"),
        },
        "results": results,
        "executed": execute,
    }


def deliberate_reject_demo() -> dict:
    """A request the verifier must refuse - proves the gate is real."""
    bad_action = {
        "action": "terminate",
        "pid": 4828,
        "name": "someapp.exe",
        "reason": "it is using a lot of CPU right now",
        "evidence": {"cpu_pct": 91.0},
    }
    return {"recommendation": bad_action, "verdict": core.verify_action(bad_action)}
