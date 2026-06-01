#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CAPTURE_ROOT = ROOT / "perf" / "captures"
RESULT_ROOT = ROOT / "perf" / "results"
BASELINE_ROOT = ROOT / "perf" / "baselines"
DEPLOY_DEST = ROOT / os.environ.get("DRIFT_PKG_ROOT", "build/deploy")
MANIFEST_PATH = ROOT / "drift" / "manifest.json"
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 34114
PROXY_PORT = 34115
TARGET_WORD_BITS = "64"
MACHINE_ID_PATH = Path("/etc/machine-id")

# Wire metrics to gate on (stable across runs on the same host).
# elapsed_ms is excluded — too noisy for pass/fail.
GATED_METRICS = ("bytes_written", "bytes_read", "packets_written", "packets_read")
REGRESSION_THRESHOLD = 0.05  # 5% above baseline = fail


@dataclass(frozen=True)
class Scenario:
    name: str
    file: Path
    iterations: int


SCENARIOS = [
    Scenario("rpc_single_result", ROOT / "perf" / "scenarios" / "rpc_single_result_perf.drift", 25),
    Scenario("rpc_multi_result", ROOT / "perf" / "scenarios" / "rpc_multi_result_perf.drift", 25),
    Scenario("rpc_error", ROOT / "perf" / "scenarios" / "rpc_error_perf.drift", 25),
]


def _resolve_driftc() -> str:
    """Resolve driftc from DRIFT_TOOLCHAIN_ROOT (preferred) or DRIFTC (fallback)."""
    root = os.environ.get("DRIFT_TOOLCHAIN_ROOT", "")
    if root:
        path = os.path.join(root, "bin", "driftc")
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise SystemExit(f"error: driftc not found at {path}")
        return path
    value = os.environ.get("DRIFTC", "")
    if not value:
        raise SystemExit("error: set DRIFT_TOOLCHAIN_ROOT or DRIFTC")
    return value


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"error: {name} must be set")
    return value


def _read_manifest_versions() -> dict[str, str]:
    """Extract artifact versions from drift/manifest.json."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {a["name"]: a["version"] for a in manifest.get("artifacts", [])}


def _verify_deploy(versions: dict[str, str]) -> None:
    """Check that deployed .zdmp packages exist for all artifacts."""
    for name, ver in versions.items():
        zdmp = DEPLOY_DEST / name / ver / f"{name}.zdmp"
        if not zdmp.exists():
            raise SystemExit(
                f"error: deployed package not found: {zdmp.relative_to(ROOT)}\n"
                f"  run: just deploy"
            )


def _module_of(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("module "):
            return line[len("module ") :].strip().rstrip(";")
    raise SystemExit(f"error: missing module declaration in {path}")


def _resolve_flocker() -> str | None:
    """Resolve the toolchain's flocker (host-global slot pool). None if absent."""
    root = os.environ.get("DRIFT_TOOLCHAIN_ROOT", "")
    if root:
        path = os.path.join(root, "bin", "flocker")
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _compile_jobs() -> int:
    """Compile-slot pool size: shared cert contract (DRIFT_TEST_JOBS) > legacy > nproc."""
    for var in ("DRIFT_TEST_JOBS", "DRIFT_BUILD_JOBS"):
        v = os.environ.get(var, "")
        if v.isdigit() and int(v) > 0:
            return int(v)
    return os.cpu_count() or 4


def _compile_cmd(driftc: str, versions: dict[str, str], scenario: Scenario, out_dir: Path) -> tuple[list[str], Path]:
    """Build the driftc command for one perf scenario (deployed .zdmp packages),
    wrapped in the shared flocker pool when available so parallel compiles never
    oversubscribe the host across callers or cert lanes."""
    bin_path = out_dir / scenario.name
    entry = f"{_module_of(scenario.file)}::main"
    cmd = [
        driftc, "--target-word-bits", TARGET_WORD_BITS,
        "--package-root", str(DEPLOY_DEST),
        "--dep", f"mariadb-rpc@{versions['mariadb-rpc']}",
        "--dep", f"mariadb-wire-proto@{versions['mariadb-wire-proto']}",
        "--entry", entry,
        str(scenario.file),
        "-o", str(bin_path),
    ]
    flocker = _resolve_flocker()
    if flocker:
        cmd = [flocker, "--key", "drift-jobs", "-j", str(_compile_jobs()), "--"] + cmd
    return cmd, bin_path


def _compile_all(driftc: str, versions: dict[str, str], out_dir: Path) -> dict[str, Path]:
    """Compile EVERY measured binary up front, in PARALLEL. Compilation is
    isolation-agnostic (never needs an idle host), so it saturates the machine;
    flocker bounds total concurrency. Returns {scenario_name: bin_path}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    procs: list[tuple[Scenario, Path, subprocess.Popen[bytes]]] = []
    for scenario in SCENARIOS:
        cmd, bin_path = _compile_cmd(driftc, versions, scenario, out_dir)
        procs.append((scenario, bin_path, subprocess.Popen(cmd, cwd=ROOT)))
    binaries: dict[str, Path] = {}
    failures: list[str] = []
    for scenario, bin_path, proc in procs:
        if proc.wait() != 0:
            failures.append(scenario.name)
        else:
            binaries[scenario.name] = bin_path
    if failures:
        raise SystemExit(f"error: perf compile failed: {', '.join(sorted(failures))}")
    return binaries


def _start_proxy(scenario: Scenario) -> tuple[subprocess.Popen[str], Path]:
    cmd = [
        sys.executable,
        str(ROOT / "tools" / "wire_capture_proxy.py"),
        "--scenario",
        scenario.name,
        "--listen-port",
        str(PROXY_PORT),
        "--target-port",
        str(TARGET_PORT),
        "--target-host",
        TARGET_HOST,
        "--output-root",
        str(CAPTURE_ROOT),
    ]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    run_dir: Path | None = None
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            rc = proc.poll()
            raise SystemExit(f"error: capture proxy exited early (rc={rc})")
        if line.startswith("[wire-capture] output: "):
            run_dir = Path(line.strip().split(": ", 1)[1])
        if line.startswith("[wire-capture] waiting for one client connection..."):
            break
    if run_dir is None:
        raise SystemExit("error: capture proxy did not report output directory")
    return proc, run_dir


def _load_events(run_dir: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    with (run_dir / "events.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                events.append(json.loads(line))
    return events


def _build_stream(run_dir: Path, events: list[dict[str, object]], direction: str) -> bytes:
    chunks: list[bytes] = []
    for event in events:
        if event["direction"] != direction:
            continue
        chunks.append((run_dir / str(event["file"])).read_bytes())
    return b"".join(chunks)


def _count_packets(stream: bytes) -> int:
    off = 0
    packets = 0
    while off < len(stream):
        if off + 4 > len(stream):
            raise SystemExit("error: truncated packet header in perf capture")
        payload_len = stream[off] | (stream[off + 1] << 8) | (stream[off + 2] << 16)
        packet_len = 4 + payload_len
        if off + packet_len > len(stream):
            raise SystemExit("error: truncated packet payload in perf capture")
        packets += 1
        off += packet_len
    return packets


def _read_summary(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))


def _run_scenario(bin_path: Path, scenario: Scenario) -> dict[str, object]:
    proxy, run_dir = _start_proxy(scenario)
    start_ns = time.monotonic_ns()
    try:
        subprocess.run([str(bin_path)], check=True, cwd=ROOT)
    finally:
        extra = ""
        if proxy.stdout is not None:
            extra = proxy.stdout.read()
        rc = proxy.wait(timeout=5)
        if rc != 0:
            raise SystemExit(f"error: capture proxy failed for {scenario.name} (rc={rc})\n{extra}")
    elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
    summary = _read_summary(run_dir)
    events = _load_events(run_dir)
    c2s_stream = _build_stream(run_dir, events, "c2s")
    s2c_stream = _build_stream(run_dir, events, "s2c")
    return {
        "name": scenario.name,
        "iterations": scenario.iterations,
        "elapsed_ms": elapsed_ms,
        "bytes_written": int(summary["bytes_c2s"]),
        "bytes_read": int(summary["bytes_s2c"]),
        "packets_written": _count_packets(c2s_stream),
        "packets_read": _count_packets(s2c_stream),
        "capture_duration_ms": int(summary["duration_ms"]),
        "capture_run_dir": str(run_dir.relative_to(ROOT)),
    }


def _get_machine_id() -> str:
    """Read /etc/machine-id as the canonical machine identity. Fail closed if absent."""
    if not MACHINE_ID_PATH.exists():
        raise SystemExit(
            f"error: {MACHINE_ID_PATH} not found — cannot determine machine identity\n"
            f"  perf baselines are keyed by machine-id for exact host pinning"
        )
    return MACHINE_ID_PATH.read_text(encoding="utf-8").strip()


def _baseline_path(machine_id: str) -> Path:
    return BASELINE_ROOT / f"{machine_id}.json"


def _load_baseline(machine_id: str) -> dict[str, dict[str, int]] | None:
    path = _baseline_path(machine_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("scenarios", {})


def _record_baseline(machine_id: str, results: list[dict[str, object]]) -> Path:
    BASELINE_ROOT.mkdir(parents=True, exist_ok=True)
    scenarios: dict[str, dict[str, int]] = {}
    for r in results:
        scenarios[str(r["name"])] = {m: int(r[m]) for m in GATED_METRICS}
    payload = {
        "machine_id": machine_id,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scenarios": scenarios,
    }
    path = _baseline_path(machine_id)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _check_baseline(machine_id: str, results: list[dict[str, object]]) -> int:
    baseline = _load_baseline(machine_id)
    if baseline is None:
        print(f"[perf] FAIL: no baseline for machine-id {machine_id!r}", file=sys.stderr)
        print(f"[perf]   run: just perf-record-baseline", file=sys.stderr)
        return 1

    failed = False
    for result in results:
        name = str(result["name"])
        expected = baseline.get(name)
        if expected is None:
            print(f"[perf] FAIL: scenario {name!r} not in baseline for machine-id {machine_id!r}", file=sys.stderr)
            failed = True
            continue
        for metric in GATED_METRICS:
            actual = int(result[metric])
            base_val = int(expected[metric])
            if base_val == 0:
                if actual != 0:
                    print(f"[perf] FAIL: {name}.{metric}: expected 0, got {actual}", file=sys.stderr)
                    failed = True
                continue
            pct = (actual - base_val) / base_val
            if pct > REGRESSION_THRESHOLD:
                print(
                    f"[perf] FAIL: {name}.{metric}: {actual} vs baseline {base_val} (+{pct:.1%} > {REGRESSION_THRESHOLD:.0%})",
                    file=sys.stderr,
                )
                failed = True
            elif pct < -REGRESSION_THRESHOLD:
                print(f"[perf] NOTE: {name}.{metric}: {actual} vs baseline {base_val} ({pct:+.1%}, improved)")
    return 1 if failed else 0


def _arg_value(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        raise SystemExit(f"error: {flag} requires a value")
    return None


def main() -> int:
    # Phase split (certifiable-test-gates methodology): the perf gate compiles
    # every measured binary in PARALLEL, then measures SERIALLY on an exclusive
    # key. The just recipe drives the two as separate steps so the measurement
    # runs under `flocker -j1 --key` (idle-resource) while compilation saturates
    # the shared pool. --compile-only / --measure-only select a single phase;
    # absent both, this runs the whole flow in one process (dev convenience).
    record_mode = "--record-baseline" in sys.argv
    compile_only = "--compile-only" in sys.argv
    measure_only = "--measure-only" in sys.argv
    if compile_only and measure_only:
        raise SystemExit("error: --compile-only and --measure-only are mutually exclusive")
    bin_dir_arg = _arg_value("--bin-dir")

    driftc = _resolve_driftc()
    versions = _read_manifest_versions()

    # ---- Compile-only phase: parallel build into a persistent --bin-dir ----
    if compile_only:
        if bin_dir_arg is None:
            raise SystemExit("error: --compile-only requires --bin-dir DIR")
        _verify_deploy(versions)
        binaries = _compile_all(driftc, versions, Path(bin_dir_arg))
        print(f"[perf] compiled {len(binaries)} scenarios -> {bin_dir_arg}")
        return 0

    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    CAPTURE_ROOT.mkdir(parents=True, exist_ok=True)
    machine_id = _get_machine_id()
    version = subprocess.run([driftc, "--version"], check=True, cwd=ROOT, capture_output=True, text=True).stdout.strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")

    # ---- Measure-only phase: run prebuilt binaries from --bin-dir, serial ----
    if measure_only:
        if bin_dir_arg is None:
            raise SystemExit("error: --measure-only requires --bin-dir DIR")
        bin_dir = Path(bin_dir_arg)
        binaries = {}
        for scenario in SCENARIOS:
            bp = bin_dir / scenario.name
            if not bp.exists():
                raise SystemExit(f"error: prebuilt perf binary not found: {bp} (run --compile-only first)")
            binaries[scenario.name] = bp
        results = [_run_scenario(binaries[scenario.name], scenario) for scenario in SCENARIOS]
    else:
        # ---- Combined (dev): parallel compile + serial measure in one process ----
        _verify_deploy(versions)
        with tempfile.TemporaryDirectory(prefix="drift-perf-bin-") as tmp_dir:
            binaries = _compile_all(driftc, versions, Path(tmp_dir))
            results = [_run_scenario(binaries[scenario.name], scenario) for scenario in SCENARIOS]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "toolchain": {
            "driftc": driftc,
            "version": version,
        },
        "target": {
            "host": TARGET_HOST,
            "port": TARGET_PORT,
            "proxy_port": PROXY_PORT,
        },
        "scenarios": results,
    }
    out_path = RESULT_ROOT / f"{stamp}.json"
    latest_path = RESULT_ROOT / "latest.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(out_path, latest_path)
    print(f"[perf] wrote {out_path.relative_to(ROOT)}")
    for result in results:
        print("[perf] " + f"{result['name']}: elapsed_ms={result['elapsed_ms']} bytes_w={result['bytes_written']} bytes_r={result['bytes_read']} packets_w={result['packets_written']} packets_r={result['packets_read']}")

    if record_mode:
        path = _record_baseline(machine_id, results)
        print(f"[perf] recorded baseline: {path.relative_to(ROOT)}")
        return 0

    # Gate: compare against machine-keyed baseline.
    return _check_baseline(machine_id, results)


if __name__ == "__main__":
    raise SystemExit(main())
