"""Foresight CLI.

Usage:
    python -m foresight status                 one full system report
    python -m foresight ask "why is my laptop slow"
    python -m foresight health                 predictive health report
    python -m foresight monitor --duration 60  live event stream + trend projections
    python -m foresight demo [--execute]       scripted capability showcase
    python -m foresight verify                 read action JSON on stdin -> verdict
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from . import agent, core, health, nlshell


def _hr(title: str) -> None:
    print()
    print("=" * 66)
    print(title)
    print("=" * 66)


def cmd_status(args) -> int:
    ctx = core.full_context()
    snap = ctx["snapshot"]
    _hr("FORESIGHT STATUS - live system report")
    print(f"CPU: {snap['cpu_total_pct']}%   Memory: {snap['mem']['used_pct']}% "
          f"({snap['mem']['used_gb']} / {snap['mem']['total_gb']} GB)")
    print(f"Battery: {ctx['battery']}")
    print("Disks:")
    for d in snap["disks"]:
        print(f"  {d['mount']}  {d['used_pct']}% used  ({d['free_gb']} GB free of {d['total_gb']} GB)")
    for h in ctx["disks_health"]:
        if h.get("status") == "unavailable":
            print(f"  SMART: {h['note']}")
        else:
            print(f"  SMART [{h['name']}]: {h['health']} ({h['type']}, {h['size_gb']} GB)")
    print("Accelerators:")
    for a in ctx["accelerators"]:
        kind = a.get("kind", "?").upper()
        print(f"  {kind}: {a.get('name')} ({a.get('status', a.get('driver', '?'))})")
    if not ctx["accelerators"]:
        print("  (none reported)")
    print(f"Model cache: {ctx['model_cache']}")
    _hr("TOP PROCESSES (classified)")
    for p in snap["processes"][:8]:
        print(f"  {p['name'][:34]:<34} cpu {p['cpu_pct']:>6.1f}%  mem {p['mem_mb']:>7.0f}MB  [{p['class']}]")
    if args.json:
        print(json.dumps(ctx, indent=2))
    return 0


def cmd_ask(args) -> int:
    ctx = core.full_context()
    print(nlshell.ask(args.question, ctx))
    return 0


def cmd_health(args) -> int:
    ctx = core.full_context()
    h = health.system_health(ctx)
    _hr(f"PREDICTIVE HEALTH - score {h['score']}/100 ({h['verdict']})")
    for c in h["components"]:
        print(f"  {c['component']:<22} load {c['load_pct']:>5}%   healthiness {c['healthiness']}")
    print("  Note: trend projections (days-until-full) come from monitor sessions.")
    return 0


def cmd_monitor(args) -> int:
    if not core.core_available():
        print("foresight-core not built; run cargo build --release first.")
        return 2
    _hr(f"FORESIGHT MONITOR - live stream ({args.duration}s)")
    proc = subprocess.Popen(
        [str(core.CORE_EXE), "monitor", "--interval-ms", str(max(args.interval_ms, 1000))],
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    samples: list[dict] = []
    deadline = time.time() + args.duration
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if time.time() > deadline:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "sample":
                samples.append(event)
                top = event.get("processes", [])
                top_str = ", ".join(
                    f"{p['name'][:18]}:{p['cpu_pct']:.0f}%" for p in top[:3]
                ) or "-"
                print(f"  [{len(samples):>3}] cpu {event['cpu_total_pct']:>5}%  mem "
                      f"{event['mem']['used_pct']:>5}%  | {top_str}")
            elif etype == "anomaly":
                print(f"  [ANOMALY] {event['key']}: value {event['value']:.1f}  (z = {event['z']:+.1f})")
            elif etype == "classify":
                print(f"  [CLASSIFY] {event['name']}: {event['class']} "
                      f"({event['confidence']:.0%}) - {event['reason']}")
    finally:
        proc.terminate()
    if len(samples) >= 4:
        proj = health.disk_trend_projection(samples)
        print("  Disk trend projection:")
        for mount, p in proj.items():
            if p.get("days_to_full") is not None:
                print(f"    {mount}: ~{p['days_to_full']} days until full "
                      f"({p['slope_pct_per_day']} %/day)")
            else:
                print(f"    {mount}: {p.get('note', 'stable')}")
    print(f"  Collected {len(samples)} samples.")
    return 0


def cmd_demo(args) -> int:
    execute = bool(getattr(args, "execute", False))
    _hr("FORESIGHT DEMO - the 2030 capabilities, running today")
    print("Built from the System Software Futures prediction:")
    print("  safe programmability | memory-safe Rust core | AI-aware +")
    print("  energy-adaptive scheduling | self-healing | predictive health |")
    print("  NL sysadmin | NPU-orchestration seed")

    _hr("1. OBSERVE  (Rust core -> live telemetry)")
    ctx = core.full_context()
    snap = ctx["snapshot"]
    print(f"  cpu {snap['cpu_total_pct']}% | mem {snap['mem']['used_pct']}% | "
          f"battery {ctx['battery'].get('percent', '-')}% | accelerators {len(ctx['accelerators'])}")
    print("  workload classification (top 5):")
    for p in snap["processes"][:5]:
        print(f"    {p['name'][:32]:<32} {p['class']:<12} cpu {p['cpu_pct']:>5}%  - {p['class_reason']}")

    _hr("2. DECIDE  (recommendations from telemetry)")
    report = agent.run_agent(execute=execute, ctx=ctx)
    if not report["results"]:
        print("  No policy actions needed right now - system within bounds.")
    for r in report["results"]:
        rec, verdict = r["recommendation"], r["verdict"]
        mark = "APPROVED" if verdict["verdict"] == "approved" else "REJECTED"
        print(f"  [{mark}] {rec['action']}: {rec['reason'][:90]}")
        if r.get("applied"):
            print(f"        -> executed: {r['applied']}")

    _hr("3. VERIFY (the gate is real - safe-by-default)")
    demo = agent.deliberate_reject_demo()
    print(f"  Request: {json.dumps(demo['recommendation'])}")
    print(f"  Verdict: {demo['verdict']['verdict'].upper()}")
    for reason in demo["verdict"].get("reasons", []):
        print(f"    - {reason}")

    _hr("4. PREDICT  (health score + component breakdown)")
    h = health.system_health(ctx)
    print(f"  Score: {h['score']}/100 ({h['verdict']})")
    for c in h["components"]:
        print(f"    {c['component']:<22} load {c['load_pct']:>5}%   healthiness {c['healthiness']}")

    _hr("5. ADMINISTER  (natural-language, offline, deterministic)")
    for q in ("why is my laptop slow?", "is my disk healthy?", "what accelerators do I have?"):
        print(f"  Q: {q}")
        answer = nlshell.ask(q, ctx)
        for ln in answer.splitlines():
            print(f"     {ln}")
        print()

    _hr("DONE")
    print(f"  Actions this run: {len(report['results'])} "
          f"({sum(1 for r in report['results'] if r['verdict']['verdict'] == 'approved')} approved, "
          f"{'EXECUTED' if execute else 'dry-run (use --execute to apply)'})")
    print(f"  All actions logged to: {core.LOG_DIR / 'actions.log'}")
    return 0


def cmd_verify(args) -> int:
    data = sys.stdin.read()
    print(json.dumps(core.verify_action(json.loads(data)) if data.strip() else
                     core.verify_action({"action": "", "reason": "", "evidence": {}}), indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="foresight", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("ask"); p.add_argument("question"); p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("health"); p.set_defaults(fn=cmd_health)
    p = sub.add_parser("monitor")
    p.add_argument("--duration", type=int, default=60)
    p.add_argument("--interval-ms", type=int, default=1000)
    p.set_defaults(fn=cmd_monitor)
    p = sub.add_parser("demo"); p.add_argument("--execute", action="store_true"); p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("verify"); p.set_defaults(fn=cmd_verify)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
