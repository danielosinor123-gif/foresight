# Foresight

### The system software of 2030 — built today, guided by the prediction

Foresight is **real, working system software** built directly from the
[System Software Futures](https://github.com/danielosinor123-gif/system-software-futures) study. The study
predicted which capabilities arrive, in what order, and what gaps they
close — Foresight implements the top of that list *now*, as a live
userland system layer with a **memory-safe Rust core**.

```
   prediction (what, gap, features, when)
                |
                v
   FORESIGHT (the software that does it)
```

---

## Prediction -> implementation map

| Predicted capability (impact / mainstream) | Foresight implementation | Status |
|---|---|---|
| **eBPF-style safe programmability** (65.7 / 2026) | `foresight-core verify`: a compiled, memory-safe Rust policy gate — **every action must pass it before execution**; demotion-only allow-list, protected-process list, malformed-input rejection | ✅ working |
| **Memory-safe & confidential cores** (46.7 / 2025) | The always-running core (sampler, anomaly engine, verifier) is written in **Rust** — no unsafe code, no panics on hostile input | ✅ working |
| **AI-aware OS scheduling** (36.6 / 2029) | Workload classifier (interactive / batch / ML / background) + verifier-gated **priority demotion** of compute-heavy non-interactive processes | ✅ working (userland) |
| **Energy-adaptive scheduling** (37.0 / 2023) | Battery-aware deferral advisories for batch/ML workloads on battery | ✅ working (advisory) |
| **Self-healing autonomic operations** (41.7 / 2026) | Memory-pressure + disk-pressure detection with proposed (verifier-gated) healing actions; full action audit log | ✅ working |
| **Predictive storage health** (31.7 / 2024) | SMART health via the Windows storage layer + **days-until-full disk projection** from monitor-session trends | ✅ working |
| **NL system administration** (36.4 / 2027) | Offline, deterministic intent engine answering system questions from **live telemetry** — no cloud, no LLM, no hallucination | ✅ working (seed) |
| **NPU / accelerator orchestration** (59.8 / 2029) | Accelerator inventory (GPU/NPU) + OS-level **shared model-cache registry** (per-app copies -> one system cache) | 🔶 seed |

## Architecture

```
        ┌────────────────────────────────────────────┐
        │            foresight-core (Rust)            │
        │  snapshot / monitor: telemetry + EWMA       │
        │  baselines + z-score anomalies + workload  │
        │  classification                             │
        │  verify: the compiled policy gate           │
        └──────────────┬─────────────────────────────┘
                       │ JSON / JSON-lines
        ┌──────────────┴─────────────────────────────┐
        │            foresight (Python)              │
        │  collectors: SMART, battery, GPU/NPU,      │
        │  model-cache registry                      │
        │  agent: recommend -> verify -> execute     │
        │  health: score + days-until-full           │
        │  nlshell: natural-language sysadmin         │
        └────────────────────────────────────────────┘
```

The loop that mirrors the eBPF model in userland:

**observe (Rust) -> propose (Python) -> VERIFY (Rust) -> execute -> log**

Nothing executes without a verifier-approved verdict, and the v1 policy
set is safe-by-default: **demotions only, no kills**.

## Quickstart

```bash
# 1) build the Rust core
cd foresight-core
cargo build --release

# 2) run it (from the project root)
python -m foresight status                 # full system report
python -m foresight ask "why is my laptop slow?"
python -m foresight ask "is my disk healthy?"
python -m foresight health                 # predictive health score
python -m foresight monitor --duration 60  # live stream + disk-trend projection
python -m foresight demo                   # scripted capability showcase
python -m foresight demo --execute         # apply approved demotions for real
echo '{"action":"terminate","pid":1234,...}' | python -m foresight verify   # watch it refuse
```

Dependencies: `pip install -r requirements.txt` (psutil only).

## What the verifier allows (v1 policy set)

| Action | Rules |
|---|---|
| `set_priority` | demotion-only (`below_normal`, `idle`); target must be named; protected system processes refuse; reserved pids refuse |
| `warn` / `noop` | informational, always allowed if reason + evidence present |
| `terminate` | **always rejected** — safe-by-default; a future policy set may revisit |
| any input | malformed JSON, missing/weak reason, or empty evidence -> rejected, never guessed |

## Verified demo moments (real output from this machine)

```
[REJECTED] terminate ... 'terminate is not in the allow-list'
[APPROVED] set_priority (opencode.exe, below_normal) - all policy checks passed
[REJECTED] set_priority svchost.exe - 'svchost.exe is a protected system process'

Health: 46.4/100 (strained)
  cpu load 100% | memory 86% | disk 84.7% used | SMART: Healthy

Q: why is my laptop slow?
   CPU is at 100%, memory at 86%. The heaviest processes right now:
     - svchost.exe (pid 8428): 232% CPU, 2082 MB - background
     - opencode.exe (pid 11016): 51% CPU, 294 MB - unknown
```

## Limitations (stated plainly)

- **Userland, not kernel**: priority changes via the OS API, not in-kernel scheduling; "AI-aware" is advisory + demotion today, kernel residency is the 2029 story.
- **Single-node**: fleet/self-assembly features (prediction #10) are roadmap.
- **NL shell is rule-based**: deterministic intent matching, not generative — chosen deliberately for zero-hallucination system control; LLM hybridization comes later behind the same verifier.
- **Terminate is refused by policy**: even Foresight can't talk the gate into a kill in v1.
- **Model cache is a registry**: no NPU runtimes were harmed (yet).

## Build note (this machine)

The Windows profile path contains a space and a WinGet-installed MinGW on
PATH breaks when linking through it. If `cargo build` fails with a
`default-manifest.o` / `collect2` error, strip MinGW from PATH for the
build so Rust uses its bundled toolchain:

```powershell
$env:PATH = ($env:PATH -split ';' | Where-Object { $_ -notmatch 'WinLibs|mingw' }) -join ';'
```

## Credits

Prediction: [*System Software Futures*](https://github.com/danielosinor123-gif/system-software-futures) (sister study) — capability cards,
mainstream-year forecasts and impact scores guided every design decision
here. The study said *what* and *when*; Foresight is the *how* — and the
kernel-resident sequel lives in
[foresight-sched](https://github.com/danielosinor123-gif/foresight-sched).

---

## Where to find this project

- **GitHub:** https://github.com/danielosinor123-gif/foresight
- **The arc:** [System Software Futures](https://github.com/danielosinor123-gif/system-software-futures) (the prediction) → **Foresight** (userland layer) → [foresight-sched](https://github.com/danielosinor123-gif/foresight-sched) (kernel-resident scheduler)
