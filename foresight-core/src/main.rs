//! Foresight native core - the memory-safe heart of the system layer.
//!
//! Guided by the System Software Futures prediction:
//!   * Memory-safe core in Rust        (prediction #4, impact 46.7)
//!   * Safe programmability: a compiled policy VERIFIER that every
//!     action must pass before execution - the userland analog of the
//!     eBPF verifier model                         (prediction #1, impact 65.7)
//!   * Workload classification feeding AI-aware scheduling inputs
//!                                                 (prediction #7)
//!   * EWMA baselines + z-score anomalies feeding self-healing and
//!     predictive-health layers                    (predictions #5, #9)
//!
//! Modes:
//!   foresight-core snapshot               one JSON system snapshot
//!   foresight-core monitor [--interval-ms N]
//!                                         JSON-lines event stream:
//!                                         sample / classify / anomaly
//!   foresight-core verify                 read an action JSON on stdin,
//!                                         emit a verdict JSON on stdout

use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{self, Read};
use std::thread::sleep;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use sysinfo::{Disks, ProcessesToUpdate, System};

// ---------------------------------------------------------------- baselines

#[derive(Clone)]
struct Ewma {
    alpha: f64,
    mean: f64,
    var: f64,
    n: u64,
}

impl Ewma {
    fn new(alpha: f64) -> Self {
        Ewma { alpha, mean: 0.0, var: 1.0, n: 0 }
    }

    fn update(&mut self, x: f64) {
        let a = self.alpha;
        let prev = self.mean;
        self.mean = a * x + (1.0 - a) * prev;
        let d = x - self.mean;
        self.var = a * d * d + (1.0 - a) * self.var;
        self.n += 1;
    }

    fn z(&self, x: f64) -> f64 {
        (x - self.mean) / (self.var + 1e-9).sqrt()
    }

    fn ready(&self) -> bool {
        self.n >= 20
    }
}

struct Baselines {
    map: HashMap<String, Ewma>,
}

impl Baselines {
    fn new() -> Self {
        Baselines { map: HashMap::new() }
    }

    /// Observe a metric; returns Some(z) once the baseline is warmed up.
    fn observe(&mut self, key: &str, x: f64) -> Option<f64> {
        let entry = self.map.entry(key.to_string()).or_insert(Ewma::new(0.2));
        entry.update(x);
        if entry.ready() {
            let z = entry.z(x);
            if z.abs() >= 3.0 {
                return Some(z);
            }
        }
        None
    }

    fn value(&self, key: &str) -> Option<f64> {
        self.map.get(key).map(|e| e.mean)
    }
}

// ------------------------------------------------------------- classifier

const INTERACTIVE: &[&str] = &[
    "chrome", "msedge", "firefox", "opera", "brave", "explorer", "code",
    "notepad", "winword", "excel", "powerpnt", "outlook", "discord",
    "slack", "teams", "windowsterminal", "windowsterminal", "cmd",
    "powershell", "pwsh", "obs64", "spotify", "vlc", "photoshop",
];
const ML: &[&str] = &[
    "ollama", "llama", "gguf", "kobold", "comfyui", "sd-webui",
    "stable-diffusion", "vllm", "llamacpp", "text-generation",
];
const ML_CMD: &[&str] = &[
    "ollama", "torch", "transformers", "llama", "gguf", "sd-", "diffus",
    "train", "inference", "ggml", "onnx",
];
const BATCH: &[&str] = &[
    "cargo", "rustc", "cl", "msbuild", "link.exe", "gcc", "g++", "make",
    "ffmpeg", "7z", "robocopy", "winget", "npm", "node", "pip", "tar",
    "java", "dotnet", "blender", "handbrake", "x264", "zip",
];
const BACKGROUND: &[&str] = &[
    "svchost", "runtimebroker", "searchindexer", "msmpeng", "audiodg",
    "dllhost", "conhost", "sihost", "taskhostw", "ctfmon", "dwm",
    "startmenuexperiencehost", "shellexperiencehost", "applicationframehost",
    "spoolsv", "wudfhost", "fontdrvhost", "widgetservice", "securityhealthservice",
];

fn lower_name(name: &str) -> String {
    name.to_lowercase()
}

/// Classify a process into a workload class with a human-readable reason.
fn classify(name: &str, cmd: &str) -> (&'static str, f64, String) {
    let n = lower_name(name);
    let c = cmd.to_lowercase();

    if ML.iter().any(|m| n.contains(m)) || ML_CMD.iter().any(|m| c.contains(m)) {
        return (
            "ml",
            0.85,
            format!("name/commandline matches ML/AI signature ({})", n),
        );
    }
    if BATCH.iter().any(|m| n.starts_with(m) || n.contains(&format!("{}.", m))) {
        return (
            "batch",
            0.8,
            format!("name matches build/batch/media-encode signature ({})", n),
        );
    }
    if INTERACTIVE.iter().any(|m| n.starts_with(m)) {
        return (
            "interactive",
            0.9,
            format!("name matches user-facing application ({})", n),
        );
    }
    if BACKGROUND.iter().any(|m| n.contains(m)) {
        return (
            "background",
            0.85,
            format!("name matches system service ({})", n),
        );
    }
    ("unknown", 0.3, "no signature match".to_string())
}

// --------------------------------------------------------------- verifier

const PROTECTED: &[&str] = &[
    "system", "registry", "idle", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "explorer.exe", "dwm.exe", "fontdrvhost.exe", "sihost.exe",
    "ctfmon.exe", "audiodg.exe", "runtimebroker.exe", "searchindexer.exe",
    "msmpeng.exe", "conhost.exe", "spoolsv.exe", "wudfhost.exe",
    "foresight-core.exe",
];

const ALLOWED_PRIORITIES: &[&str] = &["below_normal", "idle"];
const ALLOWED_ACTIONS: &[&str] = &["set_priority", "warn", "noop"];

fn is_protected(name: &str) -> bool {
    let n = lower_name(name);
    PROTECTED.iter().any(|p| n == *p)
}

/// The compiled-in policy gate: every proposed action must pass through
/// this before the executor (Python layer) may run it. Safe-by-default:
/// unknown or malformed requests are rejected, never guessed at.
fn verify(input: &str) -> Value {
    let reject = |reasons: Vec<String>| -> Value {
        json!({
            "verdict": "rejected",
            "reasons": reasons,
            "policy": "foresight-core v0.1 verifier (safe-by-default)",
        })
    };

    let parsed: Result<Value, _> = serde_json::from_str(input);
    let req = match parsed {
        Ok(v) => v,
        Err(e) => {
            return reject(vec![format!("malformed action JSON: {}", e)]);
        }
    };

    let mut reasons: Vec<String> = Vec::new();

    let action = req.get("action").and_then(|v| v.as_str()).unwrap_or("");
    if action.is_empty() {
        reasons.push("missing 'action'".into());
    } else if !ALLOWED_ACTIONS.contains(&action) {
        reasons.push(format!(
            "action '{}' is not in the allow-list {:?}",
            action, ALLOWED_ACTIONS
        ));
    }
    if action == "terminate" {
        reasons.push(
            "terminate is not permitted under the v1 policy set (safe-by-default)"
                .into(),
        );
    }

    let reason = req.get("reason").and_then(|v| v.as_str()).unwrap_or("");
    if reason.chars().count() < 10 {
        reasons.push("'reason' must be a human-readable string of >= 10 chars".into());
    }

    let evidence = req.get("evidence");
    match evidence {
        Some(Value::Object(m)) if !m.is_empty() => {}
        _ => reasons.push("'evidence' must be a non-empty JSON object with telemetry".into()),
    }

    if action == "set_priority" {
        let pid = req.get("pid").and_then(|v| v.as_u64()).unwrap_or(0);
        if pid == 0 {
            reasons.push("'pid' is required for set_priority".into());
        }
        if pid <= 4 {
            reasons.push(format!("pid {} is a reserved system pid", pid));
        }
        let priority = req.get("priority").and_then(|v| v.as_str()).unwrap_or("");
        if !ALLOWED_PRIORITIES.contains(&priority) {
            reasons.push(format!(
                "priority '{}' not allowed; demotion-only set: {:?}",
                priority, ALLOWED_PRIORITIES
            ));
        }
        let name = req.get("name").and_then(|v| v.as_str()).unwrap_or("");
        if name.is_empty() {
            reasons.push("'name' is required for set_priority (target must be identified)".into());
        } else if is_protected(name) {
            reasons.push(format!("'{}' is a protected system process", name));
        }
    }

    if !reasons.is_empty() {
        return reject(reasons);
    }

    json!({
        "verdict": "approved",
        "action": action,
        "reasons": ["all policy checks passed"],
        "policy": "foresight-core v0.1 verifier (safe-by-default)",
    })
}

// --------------------------------------------------------------- snapshot

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

struct Ctx {
    sys: System,
    baselines: Baselines,
    seen: HashMap<u32, bool>,
}

impl Ctx {
    fn new() -> Self {
        Ctx { sys: System::new(), baselines: Baselines::new(), seen: HashMap::new() }
    }

    fn prime_cpu(&mut self) {
        self.sys.refresh_cpu_usage();
        sleep(Duration::from_millis(300));
        self.sys.refresh_cpu_usage();
    }

    fn refresh(&mut self) {
        self.sys.refresh_cpu_usage();
        self.sys.refresh_memory();
        self.sys.refresh_processes(ProcessesToUpdate::All, true);
    }

    fn top_processes(&mut self, limit: usize) -> Vec<Value> {
        let mut procs: Vec<(u32, String, String, f64, f64, String)> = Vec::new();
        for (pid, p) in self.sys.processes() {
            let name = p.name().to_string_lossy().to_string();
            let cmd = p
                .cmd()
                .iter()
                .map(|c| c.to_string_lossy().to_string())
                .collect::<Vec<_>>()
                .join(" ");
            procs.push((
                pid.as_u32(),
                name,
                cmd,
                p.cpu_usage() as f64,
                p.memory() as f64 / (1024.0 * 1024.0),
                p.status().to_string(),
            ));
        }
        procs.sort_by(|a, b| b.3.partial_cmp(&a.3).unwrap_or(std::cmp::Ordering::Equal));
        procs
            .into_iter()
            .take(limit)
            .map(|(pid, name, cmd, cpu, mem_mb, status)| {
                let (class, confidence, class_reason) = classify(&name, &cmd);
                json!({
                    "pid": pid,
                    "name": name,
                    "cpu_pct": (cpu * 10.0).round() / 10.0,
                    "mem_mb": (mem_mb * 10.0).round() / 10.0,
                    "status": status,
                    "class": class,
                    "class_confidence": confidence,
                    "class_reason": class_reason,
                    "cmd": {
                        "head": cmd.chars().take(160).collect::<String>(),
                        "truncated": cmd.chars().count() > 160,
                    },
                })
            })
            .collect()
    }

    fn system_sample(&mut self) -> Value {
        let disks = Disks::new_with_refreshed_list();
        let disk_vals: Vec<Value> = disks
            .iter()
            .map(|d| {
                let total = d.total_space() as f64;
                let avail = d.available_space() as f64;
                let used_pct = if total > 0.0 { (1.0 - avail / total) * 100.0 } else { 0.0 };
                json!({
                    "mount": d.mount_point().to_string_lossy(),
                    "total_gb": (total / 1e9 * 10.0).round() / 10.0,
                    "free_gb": (avail / 1e9 * 10.0).round() / 10.0,
                    "used_pct": (used_pct * 10.0).round() / 10.0,
                })
            })
            .collect();

        let total = self.sys.total_memory() as f64 / (1024.0 * 1024.0 * 1024.0);
        let used = self.sys.used_memory() as f64 / (1024.0 * 1024.0 * 1024.0);
        let cpu_total = self.sys.global_cpu_usage() as f64;

        json!({
            "cpu_total_pct": (cpu_total * 10.0).round() / 10.0,
            "mem": {
                "total_gb": (total * 10.0).round() / 10.0,
                "used_gb": (used * 10.0).round() / 10.0,
                "used_pct": ((used / total.max(0.001) * 1000.0).round() / 10.0),
            },
            "disks": disk_vals,
        })
    }
}

// ------------------------------------------------------------------- main

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mode = args.get(1).map(|s| s.as_str()).unwrap_or("snapshot");

    match mode {
        "verify" => {
            let mut input = String::new();
            if io::stdin().read_to_string(&mut input).is_err() {
                println!("{}", json!({"verdict": "rejected", "reasons": ["stdin read failed"]}));
                std::process::exit(2);
            }
            println!("{}", verify(&input));
        }
        "snapshot" => {
            let mut ctx = Ctx::new();
            ctx.prime_cpu();
            ctx.refresh();
            let mut sample = ctx.system_sample();
            sample["ts"] = json!(now_ms());
            sample["type"] = json!("snapshot");
            sample["processes"] = json!(ctx.top_processes(12));
            println!("{}", serde_json::to_string_pretty(&sample).unwrap());
        }
        "monitor" => {
            let mut interval_ms: u64 = 1000;
            if let Some(pos) = args.iter().position(|a| a == "--interval-ms") {
                if let Some(v) = args.get(pos + 1).and_then(|s| s.parse::<u64>().ok()) {
                    interval_ms = v.clamp(200, 60_000);
                }
            }
            let mut ctx = Ctx::new();
            ctx.prime_cpu();
            let stdout = io::stdout();
            let mut out = stdout.lock();
            loop {
                ctx.refresh();
                let mut sample = ctx.system_sample();
                sample["type"] = json!("sample");
                sample["ts"] = json!(now_ms());
                let top = ctx.top_processes(8);
                sample["processes"] = json!(top.clone());
                println!("{}", serde_json::to_string(&sample).unwrap());
                use io::Write;
                let _ = out.flush();

                // anomalies on system-level metrics
                let cpu = sample["cpu_total_pct"].as_f64().unwrap_or(0.0);
                let mem_pct = sample["mem"]["used_pct"].as_f64().unwrap_or(0.0);
                if let Some(z) = ctx.baselines.observe("sys.cpu", cpu) {
                    println!(
                        "{}",
                        json!({"type": "anomaly", "key": "sys.cpu", "value": cpu, "z": z, "ts": now_ms()})
                    );
                }
                if let Some(z) = ctx.baselines.observe("sys.mem", mem_pct) {
                    println!(
                        "{}",
                        json!({"type": "anomaly", "key": "sys.mem", "value": mem_pct, "z": z, "ts": now_ms()})
                    );
                }

                // per-process baselines + first-seen classification
                for p in top.iter() {
                    let pid = p["pid"].as_u64().unwrap_or(0) as u32;
                    let name = p["name"].as_str().unwrap_or("").to_string();
                    let cpu_p = p["cpu_pct"].as_f64().unwrap_or(0.0);
                    let key = format!("proc:{}:{}:cpu", pid, lower_name(&name));
                    if let Some(z) = ctx.baselines.observe(&key, cpu_p) {
                        println!(
                            "{}",
                            json!({"type": "anomaly", "key": key, "value": cpu_p, "z": z, "ts": now_ms()})
                        );
                    }
                    if !ctx.seen.contains_key(&pid) {
                        ctx.seen.insert(pid, true);
                        println!(
                            "{}",
                            json!({
                                "type": "classify",
                                "pid": pid,
                                "name": name,
                                "class": p["class"],
                                "confidence": p["class_confidence"],
                                "reason": p["class_reason"],
                                "ts": now_ms(),
                            })
                        );
                    }
                }
                sleep(Duration::from_millis(interval_ms));
            }
        }
        other => {
            eprintln!("unknown mode '{}': use snapshot | monitor | verify", other);
            std::process::exit(2);
        }
    }
}
