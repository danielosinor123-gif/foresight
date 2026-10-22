"""Natural-language system administration (prediction #8, seeded early).

A deterministic, offline intent engine: questions are matched to intents
by keyword scoring and answered from LIVE telemetry - no cloud, no LLM,
no hallucination risk. The prediction says this capability goes mainstream
in ~2027; Foresight ships the seed today.
"""
from __future__ import annotations

import re

from . import agent, core, health


def _fmt_pct(v: float) -> str:
    return f"{v:.0f}%"


def _top(snapshot: list[dict], key: str, n: int = 5) -> list[dict]:
    return sorted(snapshot, key=lambda p: p.get(key, 0), reverse=True)[:n]


def _slow_answer(ctx: dict) -> str:
    snap = ctx["snapshot"]
    top_cpu = _top(snap["processes"], "cpu_pct", 3)
    lines = [
        f"CPU is at {_fmt_pct(snap['cpu_total_pct'])}, memory at "
        f"{_fmt_pct(snap['mem']['used_pct'])}.",
        "The heaviest processes right now:",
    ]
    for p in top_cpu:
        lines.append(
            f"  - {p['name']} (pid {p['pid']}): {p['cpu_pct']:.0f}% CPU, "
            f"{p['mem_mb']:.0f} MB - {p['class']}"
        )
    recs = agent.build_recommendations(ctx)
    setp = [r for r in recs if r["action"] == "set_priority"]
    if setp:
        lines.append(
            f"Policy proposal: demote {len(setp)} batch/ML process(es) to "
            f"below-normal priority (pending verifier approval)."
        )
    else:
        lines.append("No workload-class policy action needed right now.")
    return "\n".join(lines)


def _disk_answer(ctx: dict, projections=None) -> str:
    snap = ctx["snapshot"]
    lines = ["Disk status:"]
    for d in snap["disks"]:
        lines.append(
            f"  - {d['mount']}: {d['used_pct']:.0f}% used, "
            f"{d['free_gb']:.0f} GB free of {d['total_gb']:.0f} GB"
        )
    for h in ctx.get("disks_health", []):
        if h.get("status") == "unavailable":
            continue
        lines.append(f"  - SMART [{h.get('name', '?')}]: {h.get('health', '?')}")
    if projections:
        for mount, proj in projections.items():
            if proj.get("days_to_full"):
                lines.append(
                    f"  - Projection {mount}: ~{proj['days_to_full']} days until "
                    f"full at the current trend ({proj['slope_pct_per_day']}%/day)"
                )
    else:
        lines.append(
            "  (trend projection needs a monitoring session: "
            "python -m foresight monitor --duration 60)"
        )
    return "\n".join(lines)


def _battery_answer(ctx: dict) -> str:
    bat = ctx.get("battery", {})
    if not bat.get("present"):
        return "No battery detected - this looks like a desktop or the battery is not reporting."
    snap = ctx["snapshot"]
    state = "charging / on mains" if bat.get("plugged") else "on battery"
    top = _top(snap["processes"], "cpu_pct", 3)
    lines = [
        f"Battery: {bat.get('percent')}%, {state}.",
        "Biggest power draws (CPU is the best proxy):",
    ]
    for p in top:
        lines.append(f"  - {p['name']}: {p['cpu_pct']:.0f}% CPU ({p['class']})")
    if not bat.get("plugged"):
        lines.append(
            "Energy-adaptive policy: consider deferring batch/ML workloads "
            "until you are back on mains power."
        )
    return "\n".join(lines)


def _memory_answer(ctx: dict) -> str:
    snap = ctx["snapshot"]
    mem = snap["mem"]
    top = _top(snap["processes"], "mem_mb", 5)
    lines = [
        f"Memory: {mem['used_gb']:.1f} / {mem['total_gb']:.1f} GB "
        f"({mem['used_pct']:.0f}% used).",
        "Largest consumers:",
    ]
    for p in top:
        lines.append(f"  - {p['name']}: {p['mem_mb']:.0f} MB ({p['class']})")
    return "\n".join(lines)


def _accelerator_answer(ctx: dict) -> str:
    accels = ctx.get("accelerators", [])
    cache = ctx.get("model_cache", {})
    if not accels:
        lines = ["No GPU/NPU reported via WMI."]
    else:
        lines = ["Accelerators visible to the system:"]
        for a in accels:
            if a.get("kind") == "npu":
                lines.append(f"  - NPU: {a.get('name')} ({a.get('status')})")
            else:
                lines.append(
                    f"  - GPU: {a.get('name')} - driver {a.get('driver')}"
                )
    lines.append(
        f"Model-cache registry: {cache.get('entries', 0)} model(s), "
        f"{cache.get('total_mb', 0)} MB at {cache.get('path', '?')}"
    )
    lines.append("(the OS-level shared model cache is the seed of NPU orchestration)")
    return "\n".join(lines)


def _health_answer(ctx: dict, projections=None) -> str:
    h = health.system_health(ctx, projections)
    lines = [f"System health score: {h['score']}/100 ({h['verdict']})"]
    for c in h["components"]:
        lines.append(
            f"  - {c['component']}: load {c['load_pct']}% "
            f"(component healthiness {c['healthiness']})"
        )
    return "\n".join(lines)


def _explain_answer(ctx: dict, name: str) -> str:
    target = name.lower().replace(".exe", "")
    matches = [
        p for p in ctx["snapshot"]["processes"]
        if target in p["name"].lower()
    ]
    if not matches:
        return f"No running process matches '{name}' in the current top set."
    lines = []
    for p in matches:
        lines.append(f"{p['name']} (pid {p['pid']}):")
        lines.append(
            f"  - Workload class: {p['class']} "
            f"(confidence {p['class_confidence']:.0%}) - {p['class_reason']}"
        )
        lines.append(f"  - CPU {p['cpu_pct']:.0f}%, memory {p['mem_mb']:.0f} MB, status {p['status']}")
        head = p.get("cmd", {}).get("head", "")
        if head:
            lines.append(f"  - Command: {head}")
        if p["class"] in ("batch", "ml") and p["cpu_pct"] >= 55:
            lines.append("  - Policy: eligible for below-normal priority demotion (verifier-gated).")
        else:
            lines.append("  - Policy: no action required.")
    return "\n".join(lines)


def _fix_answer(ctx: dict) -> str:
    report = agent.run_agent(execute=False, ctx=ctx)
    lines = ["Proposed actions (dry run - nothing executed):"]
    if not report["results"]:
        return "Nothing needs fixing right now by current policy."
    for r in report["results"]:
        rec, verdict = r["recommendation"], r["verdict"]
        lines.append(f"  - [{verdict['verdict'].upper()}] {rec['action']}: {rec['reason']}")
    lines.append("Run 'python -m foresight demo --execute' to apply approved actions for real.")
    return "\n".join(lines)


def _help_answer() -> str:
    return (
        "I can answer questions about this machine from live telemetry. Try:\n"
        "  - 'why is my laptop slow' / 'what is using CPU'\n"
        "  - 'is my disk healthy' / 'how much disk space is left'\n"
        "  - 'battery' / 'what is draining power'\n"
        "  - 'memory' / 'who is using RAM'\n"
        "  - 'explain chrome' (any process name)\n"
        "  - 'what GPUs or NPUs do I have'\n"
        "  - 'system health'\n"
        "  - 'fix it' / 'clean up' (proposes verifier-gated actions)\n"
        "This is a deterministic, offline intent engine - no cloud, no LLM."
    )


_INTENTS = [
    (re.compile(r"\b(slow|lag|performance|hot|sluggish|freeze)\b", re.I), "slow"),
    (re.compile(r"\b(disk|storage|space|full|ssd|drive)\b", re.I), "disk"),
    (re.compile(r"\b(battery|power|energy|charging|drain)\b", re.I), "battery"),
    (re.compile(r"\b(memory|ram)\b", re.I), "memory"),
    (re.compile(r"\b(gpu|npus?|accelerators?|neural|copilot)\b", re.I), "accelerators"),
    (re.compile(r"\b(health|status overall|condition)\b", re.I), "health"),
    (re.compile(r"\b(explain|what is|tell me about)\b", re.I), "explain"),
    (re.compile(r"\b(fix|heal|clean|optimi[sz]e|speed up)\b", re.I), "fix"),
    (re.compile(r"\b(help|what can you|capabilit)\b", re.I), "help"),
]


def ask(question: str, ctx: dict | None = None, projections=None) -> str:
    """Answer a natural-language system question from live telemetry."""
    ctx = ctx or core.full_context()

    intent = None
    for pattern, name in _INTENTS:
        if pattern.search(question):
            intent = name
            break

    if intent == "slow":
        return _slow_answer(ctx)
    if intent == "disk":
        return _disk_answer(ctx, projections)
    if intent == "battery":
        return _battery_answer(ctx)
    if intent == "memory":
        return _memory_answer(ctx)
    if intent == "accelerators":
        return _accelerator_answer(ctx)
    if intent == "health":
        return _health_answer(ctx, projections)
    if intent == "explain":
        words = question.split()
        target = ""
        for i, w in enumerate(words):
            if w.lower() in ("explain", "is", "about", "this"):
                rest = words[i + 1:]
                if rest:
                    target = rest[0].strip("?'\".")
                    break
        if not target:
            target = words[-1].strip("?'\".")
        return _explain_answer(ctx, target)
    if intent == "fix":
        return _fix_answer(ctx)
    return _help_answer()
