"""Predictive system health: score + days-until-full disk projection.

Implements the 'Predictive Storage Health' prediction (#9): react to
trends, not thresholds. Days-until-full needs at least two samples of the
same mount over time - the monitor command collects them; a single
snapshot honestly reports 'insufficient history'.
"""
from __future__ import annotations

import numpy as np


def disk_trend_projection(samples: list[dict]) -> dict[str, dict]:
    """From monitor samples, project days until each disk is full.

    samples: [{ts (ms), disks: [{mount, used_pct, ...}]}]
    Returns {mount: {"days_to_full": float|None, "slope_pct_per_day": float}}
    """
    by_mount: dict[str, list[tuple[float, float]]] = {}
    for s in samples:
        for d in s.get("disks", []):
            by_mount.setdefault(d["mount"], []).append(
                (s["ts"] / 86400000.0, d["used_pct"])
            )

    projections: dict[str, dict] = {}
    for mount, series in by_mount.items():
        series.sort()
        if len(series) < 4:
            projections[mount] = {"days_to_full": None, "note": "insufficient history (need >= 4 samples)"}
            continue
        x = np.array([p[0] for p in series])
        y = np.array([p[1] for p in series])
        slope, intercept = np.polyfit(x, y, 1)
        if slope <= 1e-6:
            projections[mount] = {
                "days_to_full": None,
                "slope_pct_per_day": round(float(slope), 4),
                "note": "usage stable or shrinking",
            }
            continue
        # days from the last observed point to 100%
        last_x, last_y = x[-1], y[-1]
        days = (100.0 - last_y) / slope
        projections[mount] = {
            "days_to_full": round(float(days), 1),
            "slope_pct_per_day": round(float(slope), 3),
        }
    return projections


def system_health(ctx: dict, projections: dict[str, dict] | None = None) -> dict:
    """Composite system health score (0-100, higher = healthier)."""
    snap = ctx["snapshot"]
    components: list[dict] = []

    def add(name: str, pct_used: float, weight: float, worst: float = 95.0) -> None:
        # healthiness: full inverted-linear up to `worst`, then drops fast
        if pct_used <= worst:
            healthiness = 1.0 - 0.6 * (pct_used / worst)
        else:
            healthiness = 0.4 - 0.4 * ((pct_used - worst) / (100 - worst))
        components.append(
            {
                "component": name,
                "load_pct": round(pct_used, 1),
                "healthiness": round(max(0.0, min(1.0, healthiness)), 2),
                "weight": weight,
            }
        )

    add("cpu", snap.get("cpu_total_pct", 0), 0.2)
    add("memory", snap.get("mem", {}).get("used_pct", 0), 0.25, worst=92.0)
    disks = snap.get("disks", [])
    max_disk = max((d.get("used_pct", 0) for d in disks), default=0)
    add("disk (max usage)", max_disk, 0.35, worst=90.0)

    # SMART/physical health from the storage layer
    smart_bad = any(
        str(d.get("health", "")).lower() in ("unhealthy", "warning", "degraded")
        for d in ctx.get("disks_health", [])
    )
    smart_known = any(
        str(d.get("health", "")).lower() == "healthy"
        for d in ctx.get("disks_health", [])
    )
    if smart_bad:
        components.append(
            {"component": "disk SMART", "load_pct": 100, "healthiness": 0.1, "weight": 0.2}
        )
    elif smart_known:
        components.append(
            {"component": "disk SMART", "load_pct": 0, "healthiness": 1.0, "weight": 0.2}
        )
    else:
        components.append(
            {"component": "disk SMART (unknown)", "load_pct": 0, "healthiness": 0.7, "weight": 0.2}
        )

    score = 100.0 * sum(
        c["healthiness"] * c["weight"] for c in components
    ) / max(sum(c["weight"] for c in components), 1e-9)

    return {
        "score": round(score, 1),
        "components": components,
        "disk_projections": projections or {},
        "verdict": (
            "healthy" if score >= 70 else "strained" if score >= 45 else "critical"
        ),
    }
