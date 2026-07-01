#!/usr/bin/env python3
"""Emit a drift_test_run.py plan for a mariadb-client gate or a single dev test.

mariadb-client's PLAN EMITTER — the small per-project piece kept when the shared
toolchain executor (`lib/tools/drift_test_run.py`) owns the plumbing. It owns
only POLICY (which files / deps / lanes); the executor owns mechanism (parallel
compile under the flocker pool, run scheduling, dedup, valgrind wrap, heartbeat,
host concurrency budget).

Gates:
  test   — unit + live(e2e) x {base,asan} build. Unit runs base/memcheck/asan in
           PARALLEL (DB-free). Live runs base/asan/memcheck SERIALIZED on the
           shared MariaDB instance via one `mode:serial group:<DB>` — one DB
           access at a time across this gate AND any concurrent cert lane.
  stress — build the RPC stress scenario against the DEPLOYED .zdmp packages,
           then run it under the DB mutex. (`drift deploy` is harness, before
           the plan; the compile + DB-serialized run are the plan.)
  perf   — BUILD-ONLY: compile the perf scenarios against the deployed packages
           in parallel under the pool. The serial idle-resource measurement
           (wire-capture proxy + baseline gate) is harness in
           `perf_baseline.py --measure-only`, which brackets the executor.
Dev:
  one --file F      — build + run one test (base), for fast iteration.
  compile --file F  — type-check one file against its artifact's sources (no run).

Mechanism the executor owns (we no longer hand-roll): the flocker pool, slot
waiting, valgrind incantation, heartbeat, and the budget. We keep only this
emitter (policy) plus the gate harness in the justfile that brackets it (DB must
be up; `deploy`; perf measurement).

Naming: every build `out` is namespaced `<artifact>-<leaf>-<stem>#<variant>` and
DOT-FREE (dashes) so cross-root same-leaf tests can't mis-dedup and pre-0.33.16
scratch-IR paths can't collide. `out` lands directly under {work} (the runner
mkdirs work/+logs/ but not an out subdir).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "drift" / "manifest.json"
LOCK = ROOT / "drift" / "lock.json"
TWB = "64"
# Deployed-package root for stress/perf (compile against signed .zdmp, not src).
# driftc requires an absolute --package-root.
PKG_ROOT = os.path.abspath(os.environ.get("DRIFT_PKG_ROOT", str(ROOT / "build" / "deploy")))
# Host-global mutex key naming the shared MariaDB instance (mdb114-a @ :34114).
DB_GROUP = "mariadb-mdb114-a"

# Unit roots (globbed for executable `fn main` entries).
UNIT_ROOTS = [
    ("mariadb-wire-proto", "packages/mariadb-wire-proto/tests/unit"),
    ("mariadb-rpc", "packages/mariadb-rpc/tests/unit"),
    ("mariadb-failpoint-proxy", "failpoint-proxy/tests/unit"),
]

# Live/e2e lists — CURATED and ORDERED (order is significant: serial on the DB).
# NOT a glob: the rpc e2e dir carries connect_state_handoff_probe_regression_test
# which is deliberately excluded. This list is the single source of truth (it was
# previously the justfile WIRE_LIVE_TESTS / RPC_LIVE_TESTS vars).
LIVE_TESTS = [
    ("mariadb-wire-proto", [
        "packages/mariadb-wire-proto/tests/e2e/com_query_smoke_test.drift",
        "packages/mariadb-wire-proto/tests/e2e/live_tcp_smoke_test.drift",
        "packages/mariadb-wire-proto/tests/e2e/live_proto_api_smoke_test.drift",
        "packages/mariadb-wire-proto/tests/e2e/live_session_state_test.drift",
        "packages/mariadb-wire-proto/tests/e2e/live_tcp_tx_test.drift",
        "packages/mariadb-wire-proto/tests/e2e/live_tcp_load_test.drift",
        "packages/mariadb-wire-proto/tests/e2e/live_metadata_suppression_test.drift",
    ]),
    ("mariadb-rpc", [
        "packages/mariadb-rpc/tests/e2e/connect_state_handoff_stage_isolation_test.drift",
        "packages/mariadb-rpc/tests/e2e/connect_state_handoff_regression_test.drift",
        "packages/mariadb-rpc/tests/e2e/live_rpc_smoke_test.drift",
        "packages/mariadb-rpc/tests/e2e/live_pool_smoke_test.drift",
        "packages/mariadb-rpc/tests/e2e/pool_release_discard_wakeup_regression_test.drift",
        "packages/mariadb-rpc/tests/e2e/pool_acquire_timeout_test.drift",
        "packages/mariadb-rpc/tests/e2e/pool_idle_close_recycle_test.drift",
        "packages/mariadb-rpc/tests/e2e/live_managed_smoke_test.drift",
        "packages/mariadb-rpc/tests/e2e/managed_acquire_timeout_test.drift",
        "packages/mariadb-rpc/tests/e2e/managed_release_wakeup_test.drift",
        "packages/mariadb-rpc/tests/e2e/managed_idle_close_recycle_test.drift",
    ]),
]

# Perf scenarios (name -> source) compiled against deployed packages. `name` MUST
# match perf_baseline.py SCENARIOS — the measure-only harness reads {work}/<name>.
PERF_SCENARIOS = [
    ("rpc_single_result", "perf/scenarios/rpc_single_result_perf.drift"),
    ("rpc_multi_result", "perf/scenarios/rpc_multi_result_perf.drift"),
    ("rpc_error", "perf/scenarios/rpc_error_perf.drift"),
]
STRESS_SRC = "tests/stress/rpc_stress_test.drift"


def manifest_versions():
    m = json.loads(MANIFEST.read_text())
    return {a["name"]: a["version"] for a in m.get("artifacts", [])}


def resolve_artifact(artifact):
    """(src_dirs, deps) for compiling against `artifact` FROM SOURCE — pulling in
    co-artifact deps' src dirs (rpc consumes wire-proto in-repo, so a rpc test
    compiles both src trees), and external deps as name@version pins."""
    manifest = json.loads(MANIFEST.read_text())
    arts = {a["name"]: a for a in manifest.get("artifacts", [])}
    target = arts.get(artifact)
    if not target:
        sys.exit(f"error: artifact {artifact!r} not in manifest")
    resolved = {}
    if LOCK.exists():
        lock = json.loads(LOCK.read_text())
        resolved = (lock.get("artifacts", {}).get(artifact, {}) or {}).get("resolved", {}) or {}
    src_dirs, deps = set(), []
    for mod in target.get("modules", []):
        src_dirs.add(os.path.dirname(mod))
    for dep in target.get("package_deps", []):
        name, ver = dep["name"], dep["version"]
        co = arts.get(name)
        if co:
            for mod in co.get("modules", []):
                src_dirs.add(os.path.dirname(mod))
        else:
            rv = resolved.get(name, {}).get("version") or ver
            deps.append(f"{name}@{rv}")
    return sorted(src_dirs), deps


def src_files(src_dirs):
    # Recursively collect (and dedup) every .drift under each src dir. The manifest
    # only enumerates PUBLISHED modules, but a package's src tree can carry
    # internal / test-support modules (e.g. mariadb-rpc's src/internal/) that a
    # test imports and that are not manifest modules — so we walk the whole tree
    # like a source build, not just the manifest-listed dirs. Dedup because the
    # manifest dirs can nest (src and src/command both resolve under src).
    seen = set()
    for d in src_dirs:
        p = ROOT / d
        if p.is_dir():
            for f in p.rglob("*.drift"):
                seen.add(str(f.relative_to(ROOT)))
    return sorted(seen)


def build_context(artifact):
    """(srcs, dep_flags) for a source build against `artifact`."""
    src_dirs, deps = resolve_artifact(artifact)
    dep_flags = []
    for d in deps:
        dep_flags += ["--dep", d]
    return src_files(src_dirs), dep_flags


def is_test_entry(rel):
    # drift >= 0.33.67 requires the --entry target to be `pub`, so `main` may be
    # declared `pub fn main` (older tests still use bare `fn main`).
    txt = (ROOT / rel).read_text(errors="ignore")
    return bool(re.search(r"^module\s+", txt, re.M)) and bool(re.search(r"^(?:pub\s+)?fn\s+main\(", txt, re.M))


def module_of(rel):
    m = re.search(r"^module\s+(.+?);?\s*$", (ROOT / rel).read_text(errors="ignore"), re.M)
    if not m:
        sys.exit(f"error: missing module declaration in {rel}")
    return m.group(1).strip().rstrip(";")


def _sanitize(sanitize):
    # Explicit, argv-determined selector (driftc --sanitize), never DRIFT_ASAN.
    return ["--sanitize", "address" if sanitize else "none"]


def src_build(out_name, srcs, dep_flags, entry, test_rel, sanitize=False):
    """Build job: compile test_rel + the artifact's src tree to {work}/<out_name>."""
    out = f"{{work}}/{out_name}"
    cmd = ["{driftc}", "--target-word-bits", TWB] + dep_flags + _sanitize(sanitize) + \
          ["--entry", entry] + srcs + [test_rel, "-o", out]
    return {"id": out_name, "out": out, "cmd": cmd}


def pkg_build(out_name, dep_flags, entry, test_rel, sanitize=False):
    """Build job: compile test_rel against DEPLOYED packages (no src) to {work}/<out_name>."""
    out = f"{{work}}/{out_name}"
    cmd = ["{driftc}", "--target-word-bits", TWB, "--package-root", PKG_ROOT] + dep_flags + \
          _sanitize(sanitize) + ["--entry", entry] + [test_rel, "-o", out]
    return {"id": out_name, "out": out, "cmd": cmd}


def deployed_dep_flags():
    ver = manifest_versions()
    return ["--dep", f"mariadb-rpc@{ver['mariadb-rpc']}",
            "--dep", f"mariadb-wire-proto@{ver['mariadb-wire-proto']}"]


# ------------------------------------------------------------------ gate: test
def emit_test():
    build, run_unit, run_live = [], [], []

    # Unit — parallel run, base / memcheck (reuse base via wrap) / asan.
    for artifact, root in UNIT_ROOTS:
        srcs, dep_flags = build_context(artifact)
        leaf = os.path.basename(root)  # "unit"
        p = ROOT / root
        for tf in (sorted(p.glob("*.drift")) if p.is_dir() else []):
            rel = str(tf.relative_to(ROOT))
            if not is_test_entry(rel):
                continue
            qual = f"{artifact}-{leaf}-{tf.stem}"
            entry = f"{module_of(rel)}::main"
            build.append(src_build(f"{qual}#base", srcs, dep_flags, entry, rel))
            build.append(src_build(f"{qual}#asan", srcs, dep_flags, entry, rel, sanitize=True))
            run_unit.append({"id": f"{qual}#run-base", "cmd": [f"{{work}}/{qual}#base"], "needs": [f"{qual}#base"]})
            run_unit.append({"id": f"{qual}#run-memcheck", "cmd": [f"{{work}}/{qual}#base"], "needs": [f"{qual}#base"], "wrap": "memcheck"})
            run_unit.append({"id": f"{qual}#run-asan", "cmd": [f"{{work}}/{qual}#asan"], "needs": [f"{qual}#asan"]})

    # Live — build base+asan; run base/asan/memcheck SERIAL on one DB group. Lane
    # order base -> asan -> memcheck, curated file order within each lane (matches
    # the old serial gate). `order` sequences the single serial group.
    live_quals = []
    for artifact, files in LIVE_TESTS:
        srcs, dep_flags = build_context(artifact)
        for rel in files:
            if not is_test_entry(rel):
                sys.exit(f"error: {rel} is not an executable test entry")
            qual = f"{artifact}-e2e-{Path(rel).stem}"
            entry = f"{module_of(rel)}::main"
            build.append(src_build(f"{qual}#base", srcs, dep_flags, entry, rel))
            build.append(src_build(f"{qual}#asan", srcs, dep_flags, entry, rel, sanitize=True))
            live_quals.append(qual)

    order = 0
    for lane in ("base", "asan", "memcheck"):
        for qual in live_quals:
            if lane == "asan":
                job = {"id": f"{qual}#run-asan", "cmd": [f"{{work}}/{qual}#asan"], "needs": [f"{qual}#asan"]}
            elif lane == "memcheck":
                job = {"id": f"{qual}#run-memcheck", "cmd": [f"{{work}}/{qual}#base"], "needs": [f"{qual}#base"], "wrap": "memcheck"}
            else:
                job = {"id": f"{qual}#run-base", "cmd": [f"{{work}}/{qual}#base"], "needs": [f"{qual}#base"]}
            job.update({"mode": "serial", "group": DB_GROUP, "order": order})
            order += 1
            run_live.append(job)

    return {"name": "test", "phases": [
        {"name": "build", "jobs": build},
        {"name": "run-unit", "jobs": run_unit},
        {"name": "run-live", "jobs": run_live},
    ]}


# ---------------------------------------------------------------- gate: stress
def emit_stress():
    dep_flags = deployed_dep_flags()
    entry = f"{module_of(STRESS_SRC)}::main"
    build = [pkg_build("rpc_stress_test", dep_flags, entry, STRESS_SRC)]
    run = [{"id": "rpc_stress_test#run", "cmd": ["{work}/rpc_stress_test"], "needs": ["rpc_stress_test"],
            "mode": "serial", "group": DB_GROUP, "order": 0}]
    return {"name": "stress", "phases": [
        {"name": "build", "jobs": build},
        {"name": "run", "jobs": run},
    ]}


# ------------------------------------------------------- gate: perf (build-only)
def emit_perf():
    dep_flags = deployed_dep_flags()
    jobs = [pkg_build(name, dep_flags, f"{module_of(rel)}::main", rel) for name, rel in PERF_SCENARIOS]
    return {"name": "perf-build", "phases": [{"name": "build", "jobs": jobs}]}


# ----------------------------------------------------------- dev: one / compile
def infer_artifact(rel):
    if rel.startswith("packages/mariadb-wire-proto/"):
        return "mariadb-wire-proto"
    if rel.startswith("packages/mariadb-rpc/"):
        return "mariadb-rpc"
    if rel.startswith("failpoint-proxy/"):
        return "mariadb-failpoint-proxy"
    sys.exit(f"error: cannot infer artifact for {rel} (expected packages/mariadb-*/... or failpoint-proxy/...)")


def emit_one(rel):
    if not is_test_entry(rel):
        sys.exit(f"error: {rel} is not an executable test entry (module + fn main)")
    srcs, dep_flags = build_context(infer_artifact(rel))
    name = Path(rel).stem
    build = [src_build(name, srcs, dep_flags, f"{module_of(rel)}::main", rel)]
    run = [{"id": f"{name}#run", "cmd": [f"{{work}}/{name}"], "needs": [name]}]
    return {"name": "one", "phases": [{"name": "build", "jobs": build}, {"name": "run", "jobs": run}]}


def emit_compile(rel):
    srcs, dep_flags = build_context(infer_artifact(rel))
    extra = [] if rel in srcs else [rel]  # don't pass a src file twice
    cmd = ["{driftc}", "--target-word-bits", TWB] + dep_flags + _sanitize(False) + srcs + extra
    return {"name": "compile", "phases": [{"name": "compile", "jobs": [{"id": "compile-check", "cmd": cmd}]}]}


def emit_app(app_name):
    """Build-only plan for a kind:app artifact FROM LOCAL SOURCE — no deploy,
    signing, or publishing. Sources + deps come from drift/manifest.json; the
    entry symbol is the artifact's entry_point. Output binary lands at
    {work}/<app_name>; the caller copies it out to a persistent path."""
    manifest = json.loads(MANIFEST.read_text())
    art = {a["name"]: a for a in manifest.get("artifacts", [])}.get(app_name)
    if not art:
        sys.exit(f"error: artifact {app_name!r} not in manifest")
    if art.get("kind") != "app":
        sys.exit(f"error: artifact {app_name!r} is not kind:app")
    entry = art.get("entry_point")
    if not entry:
        sys.exit(f"error: app {app_name!r} has no entry_point in the manifest")
    srcs, dep_flags = build_context(app_name)
    out = f"{{work}}/{app_name}"
    cmd = ["{driftc}", "--target-word-bits", TWB] + dep_flags + ["--entry", entry] + srcs + ["-o", out]
    job = {"id": app_name, "out": out, "cmd": cmd}
    return {"name": "app", "phases": [{"name": "build", "jobs": [job]}]}


def main():
    ap = argparse.ArgumentParser(description="Emit a drift_test_run.py plan for a mariadb-client gate.")
    ap.add_argument("gate", choices=["test", "stress", "perf", "one", "compile", "app"])
    ap.add_argument("--file", help="test/source file (for one|compile)")
    ap.add_argument("--app", help="app artifact name (for app)")
    ap.add_argument("--out", default="-", help="output path for the plan JSON (default: stdout)")
    args = ap.parse_args()
    if args.gate == "app":
        if not args.app:
            sys.exit("error: --app required for app")
        plan = emit_app(args.app)
    elif args.gate in ("one", "compile"):
        if not args.file:
            sys.exit("error: --file required for one|compile")
        plan = emit_one(args.file) if args.gate == "one" else emit_compile(args.file)
    else:
        plan = {"test": emit_test, "stress": emit_stress, "perf": emit_perf}[args.gate]()
    text = json.dumps(plan, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text)
        n = sum(len(p["jobs"]) for p in plan["phases"])
        print(f"wrote {args.out}: {plan['name']} plan, {n} jobs across {len(plan['phases'])} phase(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
