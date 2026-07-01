#!/usr/bin/env python3
"""S8: automated proxy-PROCESS gate coverage for mariadb-failpoint-proxy.

Everything else in the gate (`just test`'s unit/e2e phases, S5/S6's
control_test.drift, S7's live_proxy_pool_commit_ambiguous_test.drift)
exercises the proxy's LOGIC in-process or drives it manually. Nothing before
this ran the actual CERTIFIED BINARY as a real subprocess automatically —
see work/mariadb-rpc-failpoints/PROXY-GATE-HARNESS.md, whose status line
calls this "REQUIRED before treating mariadb-failpoint-proxy as
certification-ready downstream test tooling".

This script:
  1. builds the proxy from local source (mirrors `just build-app` — no
     deploy/sign/publish);
  2. starts it as a real subprocess against mdb114-a, stderr captured to a
     file (the proxy logs structured JSON Lines there — see main.drift);
  3. waits for readiness via the control-plane `health` op (not just a raw
     connect — health means the DATA listener is bound, see control.drift);
  4. drives an EXISTING manual e2e test through it as a real TCP client — the
     tests already assert everything they can see from the client side; this
     harness does not re-implement those assertions, it wraps them and adds
     the one thing only an outside observer can check: the proxy's own log.

Case 1 (plain passthrough): live_proxy_passthrough_smoke_test.drift (nothing
armed — connect/auth/query/commit must all pass through unchanged), plus
this harness asserts the proxy's stderr contains every expected lifecycle
event at least once: proxy_start, client_accept, backend_connect,
commit_observed, conn_close.

Case 2 (one-shot ambiguous COMMIT, now that S7 exists):
live_proxy_pool_commit_ambiguous_test.drift, run against a FRESH proxy
instance. That test already arms the one-shot failpoint over raw TCP
control, asserts RpcCommitErrorKind::AmbiguousWrite via an exhaustive match,
asserts fired-exactly-once via assert_all_fired, and proves a clean-reconnect
recovery commit (see PLAN.md's S7 entry) — this harness does not duplicate
any of those CLIENT-side assertions, it just runs the test against a
gate-managed subprocess and checks its exit code. It DOES additionally
assert the proxy's own log shows the fault actually fired in the real
binary: proxy_start, client_accept, backend_connect, commit_observed,
failpoint_fire, conn_close — the one thing only an outside observer of the
process (not S7's client-side checks) can confirm, catching a logging
regression in the actual binary.

Exit 0 if both cases pass; nonzero otherwise, with a message identifying
which case/assertion failed. Tears the proxy subprocess down reliably
(SIGTERM, grace period, SIGKILL fallback) even when a case fails — including
when the shared executor itself hangs building the proxy or running a
client test: both are wrapped in a bounded timeout (see _run_plan) that
kills the executor's whole process group on expiry, so a hang can't keep
this script (and the proxy subprocess it's supervising) alive forever.

Invoked from `just test` (see justfile) under `flocker --key mariadb-mdb114-a`
so it doesn't race a concurrent gate's own DB access — same resource-key
convention as tools/emit_test_plan.py's DB_GROUP.
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import emit_test_plan  # noqa: E402

APP_NAME = "mariadb-failpoint-proxy"
# Same fixed ports the manual S4/S7 smoke tests hardcode (they are not
# parameterized via argv) — this harness must use the identical values so
# those existing test files connect to the subprocess it starts.
DATA_HOST = "127.0.0.1"
DATA_PORT = 43306
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 43307
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 34114  # mdb114-a, per emit_test_plan.py's DB_GROUP comment

READY_TIMEOUT_S = 10.0
PROC_STOP_GRACE_S = 3.0
# Listening sockets don't linger in TIME_WAIT the way actively-closed
# connections do, so restarting on the same ports right after a clean exit
# is expected to be safe — this is just cheap insurance.
BETWEEN_CASES_SETTLE_S = 0.3
# Bounds on the shared executor's own subprocess (compile + run of the proxy
# itself / of one client test) — generous relative to observed times (proxy
# build ~17s, one client compile+run ~20s) but real bounds, since a hang here
# would otherwise keep the proxy subprocess alive forever (see _run_plan).
BUILD_TIMEOUT_S = 180
CLIENT_RUN_TIMEOUT_S = 120

REQUIRED_EVENTS_CASE1 = ["proxy_start", "client_accept", "backend_connect", "commit_observed", "conn_close"]
# Case 2 additionally must show the fault actually fired in the real
# binary's own log, not just that the client-side test passed — S7's
# assertions are client-side (RpcCommitErrorKind, assert_all_fired over
# control); this is the one thing only the proxy's own log can confirm, and
# it catches a logging regression in the actual binary that a client-only
# check would miss.
REQUIRED_EVENTS_CASE2 = ["proxy_start", "client_accept", "backend_connect", "commit_observed", "failpoint_fire", "conn_close"]

CLIENT_TEST_CASE1 = "packages/mariadb-rpc/tests/e2e/live_proxy_passthrough_smoke_test.drift"
CLIENT_TEST_CASE2 = "packages/mariadb-rpc/tests/e2e/live_proxy_pool_commit_ambiguous_test.drift"


def _fail(msg):
    print(f"[proxy-gate] error: {msg}", file=sys.stderr)
    sys.exit(1)


def _require_toolchain():
    root = os.environ.get("DRIFT_TOOLCHAIN_ROOT")
    if not root:
        _fail("DRIFT_TOOLCHAIN_ROOT must be set (need toolchain >= 0.33.67)")
    runner = Path(root) / "lib" / "tools" / "drift_test_run.py"
    if not runner.is_file():
        _fail(f"shared executor not found at {runner} (need toolchain >= 0.33.17)")
    return runner


def _run_plan(runner, plan, work_dir, timeout_s):
    """Write `plan` to work_dir/plan.json and run it through the shared
    executor (mirrors every `just check-one`/`build-app`-style recipe).
    Returns the executor's exit code, or None if it exceeded `timeout_s` (a
    hung compile/run must not leave the caller's `finally: stop_proxy(...)`
    unreached forever — see PLAN.md's S8 review notes). On timeout, the whole
    process GROUP is killed, not just the direct child: the executor spawns
    compiler/test-binary children of its own that a child-only kill would
    orphan still running."""
    work_dir.mkdir(parents=True, exist_ok=True)
    plan_path = work_dir / "plan.json"
    plan_path.write_text(json.dumps(plan))
    proc = subprocess.Popen(
        [sys.executable, str(runner), "--plan", str(plan_path), "--work-dir", str(work_dir)],
        start_new_session=True,
    )
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        print(f"[proxy-gate] executor timed out after {timeout_s}s — killing its process group", file=sys.stderr)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return None


def build_proxy(work_dir):
    """Build the proxy from local source (mirrors `just build-app`: no
    deploy/sign/publish). Returns the path to the built binary."""
    runner = _require_toolchain()
    plan = emit_test_plan.emit_app(APP_NAME)
    rc = _run_plan(runner, plan, work_dir, BUILD_TIMEOUT_S)
    if rc != 0:
        _fail(f"proxy build failed (executor exit {rc})")
    binpath = work_dir / APP_NAME
    if not binpath.is_file():
        _fail(f"proxy binary not found at {binpath} after a reported-clean build")
    return binpath


def run_client_test(rel_path, work_dir):
    """Compile + run one EXISTING Drift e2e test file (mirrors `just
    check-one`, unmodified). Returns its exit code (or None on timeout — see
    _run_plan)."""
    runner = _require_toolchain()
    plan = emit_test_plan.emit_one(rel_path)
    return _run_plan(runner, plan, work_dir, CLIENT_RUN_TIMEOUT_S)


def control_request(op_obj, timeout=2.0):
    """One raw-TCP JSON-Lines control request/response (PLAN.md §7). Returns
    the parsed response dict, or None on any transport failure — callers
    decide the retry policy (readiness polling tolerates connect failures;
    nothing else here needs to)."""
    try:
        with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=timeout) as s:
            s.sendall((json.dumps(op_obj) + "\n").encode("utf-8"))
            s.settimeout(timeout)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    return None
                buf += chunk
            return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except OSError:
        return None


def wait_ready(proc, deadline_s):
    """Poll control `health` until ok:true, the subprocess exits early, or
    the deadline elapses."""
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if proc.poll() is not None:
            return False
        resp = control_request({"op": "health"})
        if resp and resp.get("ok"):
            return True
        time.sleep(0.05)
    return False


def start_proxy(binpath, log_path):
    log_f = open(log_path, "wb")
    proc = subprocess.Popen(
        [
            str(binpath),
            "--data-host", DATA_HOST, "--data-port", str(DATA_PORT),
            "--backend-host", BACKEND_HOST, "--backend-port", str(BACKEND_PORT),
            "--control-host", CONTROL_HOST, "--control-port", str(CONTROL_PORT),
        ],
        stdout=log_f, stderr=log_f, stdin=subprocess.DEVNULL,
    )
    return proc, log_f


def stop_proxy(proc, log_f):
    try:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=PROC_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=PROC_STOP_GRACE_S)
    finally:
        log_f.close()


def assert_events_present(log_path, expected_events):
    text = log_path.read_text(errors="ignore")
    missing = [ev for ev in expected_events if f'"ev":"{ev}"' not in text]
    if missing:
        _fail(f"proxy log missing expected event(s) {missing} — see {log_path}")


def case1(binpath, work_dir):
    print("[proxy-gate] case 1: passthrough + lifecycle events", file=sys.stderr)
    log_path = work_dir / "proxy_case1.jsonl"
    proc, log_f = start_proxy(binpath, log_path)
    try:
        if not wait_ready(proc, READY_TIMEOUT_S):
            print(log_path.read_text(errors="ignore"), file=sys.stderr)
            _fail("proxy did not become ready (control health) within timeout — case 1")
        rc = run_client_test(CLIENT_TEST_CASE1, work_dir / "case1_client")
        if rc != 0:
            _fail(f"case 1 client test failed (exit {rc}): {CLIENT_TEST_CASE1}")
    finally:
        stop_proxy(proc, log_f)
    assert_events_present(log_path, REQUIRED_EVENTS_CASE1)
    print("[proxy-gate] case 1: PASS", file=sys.stderr)


def case2(binpath, work_dir):
    print("[proxy-gate] case 2: one-shot ambiguous COMMIT via real pool", file=sys.stderr)
    log_path = work_dir / "proxy_case2.jsonl"
    proc, log_f = start_proxy(binpath, log_path)
    try:
        if not wait_ready(proc, READY_TIMEOUT_S):
            print(log_path.read_text(errors="ignore"), file=sys.stderr)
            _fail("proxy did not become ready (control health) within timeout — case 2")
        rc = run_client_test(CLIENT_TEST_CASE2, work_dir / "case2_client")
        if rc != 0:
            print(log_path.read_text(errors="ignore"), file=sys.stderr)
            _fail(f"case 2 client test failed (exit {rc}): {CLIENT_TEST_CASE2}")
    finally:
        stop_proxy(proc, log_f)
    assert_events_present(log_path, REQUIRED_EVENTS_CASE2)
    print("[proxy-gate] case 2: PASS", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work-dir", required=True, help="scratch directory for the build + client-test artifacts + proxy logs")
    args = ap.parse_args()
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    binpath = build_proxy(work_dir / "build")
    case1(binpath, work_dir)
    time.sleep(BETWEEN_CASES_SETTLE_S)
    case2(binpath, work_dir)
    print("[proxy-gate] all cases PASS", file=sys.stderr)


if __name__ == "__main__":
    main()
