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

Case 3 (nth=2 targeting, the H1 app-usage shape — Singular emits a `start`
COMMIT then later a `complete` COMMIT, and only the SECOND is under test):
live_proxy_nth_commit_ambiguous_test.drift. Docs/failpoint-proxy-usage.md
tells downstream teams to arm `match.nth=2` for this shape; this proves it
against a real subprocess, not just control_test.drift's in-process
scenario_fire_nth2 unit test. Same pattern as case 2: the client test proves
the semantics (commit #1 Ok, commit #2 AmbiguousWrite, assert_all_fired
ok:true, status shows matched_nth==2), this harness checks its exit code and
the proxy's own log.

Case 4 (domain isolation — the exact routing pattern the docs also tell
downstream teams to rely on: one domain through the proxy, others direct):
live_proxy_domain_isolation_test.drift. Proves the domain-independent claim
the routing recommendation rests on: a connection that never touches the
proxy's data listener cannot affect (or be affected by) the failpoint
registry, while one that does touch it does. This repo has one dev DB, so it
proves this with a direct-vs-proxied connection to the SAME backend rather
than bookkeeper's actual two domains — simulating those is app/workflows-
owned, see docs/failpoint-proxy-usage.md.

Case 5 (drop-and-hold: the timeout-flavored ambiguous COMMIT, PLAN.md §14):
live_proxy_commit_timeout_ambiguous_test.drift. An app team asked whether a
HUNG/SLOW-network ambiguous commit (proxy holds the connection open,
responding to neither side, until the CLIENT's own commit-I/O timeout fires)
classifies the same way as the reset-flavored one cases 2-4 exercise (proxy
closes/resets immediately after forwarding). This proves it does: same
RpcCommitErrorKind::AmbiguousWrite, no hang (the client's own read timeout
is what fires, well before the proxy's own hold elapses and self-closes),
and the pool still discards the poisoned lease and reconnects cleanly.

Case 6/7 (graceful shutdown on SIGTERM/SIGINT — regression coverage for the
drift-lang signalfd busy-spin CORE_BUG this repo reported, fixed upstream in
certified `0.33.68+abi19`, PLUS this proxy's OWN `conc.await_signal()`
handling added in response to a downstream app team's report that even with
the reactor fix, mariadb-failpoint-proxy 0.1.0/0.2.1 never exited on SIGTERM
— it registered no waiter, so the signal was simply stashed and ignored
forever, matching `--help`'s old "no built-in shutdown flag" text). Starts a
fresh proxy, sends the signal, and asserts it exits with code 0 within
SHUTDOWN_EXIT_TIMEOUT_S (comfortably above the observed ~0.2s clean-exit
time, matching bookkeeper/uflowsd, but far below the ~7s the pre-fix
busy-spin needed before a SIGKILL fallback — a real regression back to
"SIGTERM does nothing" would blow this bound clearly, not pass by luck).
Also asserts the proxy's own log shows the full shutdown lifecycle:
shutdown_signal, accept_loop_stopped, control_accept_loop_stopped,
proxy_shutdown_complete.

Exit 0 if all seven cases pass; nonzero otherwise, with a message identifying
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
#
# Deliberately NOT 43306/43307, the ports shown as the illustrative example
# in docs/failpoint-proxy-usage.md: a real collision happened on this exact
# pair on a shared dev box (another concurrent session's proxy, almost
# certainly started by literally copying that doc example, was squatting on
# both ports with a different --backend-port, which made this repo's own
# `just test` fail with a cryptic listen_failed deep in the S8 gate's log).
# Using a distinct pair for THIS repo's own CI doesn't eliminate collision
# risk in general (any fixed number can collide with another fixed number —
# see docs/failpoint-proxy-usage.md's guidance to prefer unique/ephemeral
# ports per harness), but it does stop us colliding with anyone else who
# follows our own docs' copy-paste example, which is what just happened.
DATA_HOST = "127.0.0.1"
DATA_PORT = 45306
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 45307
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

EVENTS_PASSTHROUGH = ["proxy_start", "client_accept", "backend_connect", "commit_observed", "conn_close"]
# Cases that arm a failpoint additionally must show the fault actually fired
# in the real binary's own log, not just that the client-side test passed —
# the client tests' own assertions are client-side (RpcCommitErrorKind,
# assert_all_fired over control); this is the one thing only the proxy's own
# log can confirm, catching a logging regression in the actual binary that a
# client-only check would miss.
EVENTS_FIRED = ["proxy_start", "client_accept", "backend_connect", "commit_observed", "failpoint_fire", "conn_close"]

# (case name, human label for progress output, client test to run against a
# fresh proxy instance, required proxy-log events). Each client test is run
# UNMODIFIED — see the module docstring for what each already proves
# client-side; this harness adds only proxy-log verification on top.
CASES = [
    ("case1", "passthrough + lifecycle events",
     "packages/mariadb-rpc/tests/e2e/live_proxy_passthrough_smoke_test.drift", EVENTS_PASSTHROUGH),
    ("case2", "one-shot ambiguous COMMIT via real pool (S7)",
     "packages/mariadb-rpc/tests/e2e/live_proxy_pool_commit_ambiguous_test.drift", EVENTS_FIRED),
    ("case3", "nth=2 targeting via real subprocess (H1 shape)",
     "packages/mariadb-rpc/tests/e2e/live_proxy_nth_commit_ambiguous_test.drift", EVENTS_FIRED),
    ("case4", "domain isolation: direct traffic doesn't consume, proxied traffic does",
     "packages/mariadb-rpc/tests/e2e/live_proxy_domain_isolation_test.drift", EVENTS_FIRED),
    ("case5", "drop-and-hold: timeout-flavored ambiguous COMMIT",
     "packages/mariadb-rpc/tests/e2e/live_proxy_commit_timeout_ambiguous_test.drift", EVENTS_FIRED),
]

# Bound on time-to-exit after a shutdown signal. Comfortably above the
# observed ~0.2s clean-exit time (matches bookkeeper/uflowsd's own signal
# waiters), but far below the ~7s the pre-fix busy-spin needed before a
# SIGKILL fallback — a real regression back to "SIGTERM does nothing" blows
# this bound clearly, it doesn't pass by luck.
SHUTDOWN_EXIT_TIMEOUT_S = 3.0
REQUIRED_SHUTDOWN_EVENTS = [
    "shutdown_signal", "accept_loop_stopped", "control_accept_loop_stopped", "proxy_shutdown_complete",
]
# (case name, human label, signal to send). Each gets its own fresh proxy
# instance — see check_graceful_shutdown.
SHUTDOWN_SIGNALS = [
    ("case6", "graceful shutdown on SIGTERM", signal.SIGTERM),
    ("case7", "graceful shutdown on SIGINT", signal.SIGINT),
]


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
    """Poll control `health` until ok:true AND its data_listener/backend_listener
    match what WE expect, the subprocess exits early, or the deadline elapses.

    Checking those echoed fields (not just ok:true) matters: this is exactly
    how the port-collision incident in PLAN.md's §14 entry slipped past —
    another session's already-running proxy was squatting on our expected
    control port, so it happily answered ok:true for OUR health poll while
    OUR OWN freshly-spawned `proc` had already failed to bind (listen_failed)
    and was in the process of exiting. `proc.poll()` above is a real check,
    but it's a race: on the very first loop iteration `proc` may not have
    exited yet, so the wrong process's ok:true response can win before our
    own failure becomes visible. Comparing the listener strings closes that
    race — they only match when it's genuinely our subprocess answering, not
    a foreign one — and fails loudly with a clear diagnosis instead of
    silently proceeding to run a client test against the wrong proxy (whose
    failure, seen later, gives no hint that a port collision was the cause)."""
    expected = {
        "data_listener": f"{DATA_HOST}:{DATA_PORT}",
        "backend_listener": f"{BACKEND_HOST}:{BACKEND_PORT}",
    }
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if proc.poll() is not None:
            return False
        resp = control_request({"op": "health"})
        if resp and resp.get("ok"):
            mismatches = {k: (want, resp.get(k)) for k, want in expected.items() if resp.get(k) != want}
            if mismatches:
                _fail(
                    f"control health answered ok:true but NOT from the proxy we just started "
                    f"(PID {proc.pid}) — mismatched field(s) {mismatches}. A different process "
                    f"is almost certainly already listening on {CONTROL_HOST}:{CONTROL_PORT} "
                    f"(or {DATA_HOST}:{DATA_PORT}); see PLAN.md's port-collision entry."
                )
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


def run_case(name, label, binpath, work_dir, client_test, required_events):
    """Fresh proxy instance -> wait ready -> run one EXISTING client test
    unmodified -> stop the proxy -> assert its log shows the required
    lifecycle events. Shared by every entry in CASES."""
    print(f"[proxy-gate] {name}: {label}", file=sys.stderr)
    log_path = work_dir / f"proxy_{name}.jsonl"
    proc, log_f = start_proxy(binpath, log_path)
    try:
        if not wait_ready(proc, READY_TIMEOUT_S):
            print(log_path.read_text(errors="ignore"), file=sys.stderr)
            _fail(f"proxy did not become ready (control health) within timeout — {name}")
        rc = run_client_test(client_test, work_dir / f"{name}_client")
        if rc != 0:
            print(log_path.read_text(errors="ignore"), file=sys.stderr)
            _fail(f"{name} client test failed (exit {rc}): {client_test}")
    finally:
        stop_proxy(proc, log_f)
    assert_events_present(log_path, required_events)
    print(f"[proxy-gate] {name}: PASS", file=sys.stderr)


def check_graceful_shutdown(name, label, sig, binpath, work_dir):
    """Fresh proxy instance -> wait ready -> send `sig` directly (not the
    stop_proxy grace+SIGKILL-fallback helper — this measures the real,
    unfallback-assisted exit) -> assert it exits with code 0 within
    SHUTDOWN_EXIT_TIMEOUT_S -> assert its own log shows the full shutdown
    lifecycle. Regression coverage for both the drift-lang signalfd
    busy-spin CORE_BUG (fixed in certified 0.33.68+abi19) and this proxy's
    own conc.await_signal() handling — see the module docstring."""
    print(f"[proxy-gate] {name}: {label}", file=sys.stderr)
    log_path = work_dir / f"proxy_{name}.jsonl"
    proc, log_f = start_proxy(binpath, log_path)
    try:
        if not wait_ready(proc, READY_TIMEOUT_S):
            print(log_path.read_text(errors="ignore"), file=sys.stderr)
            _fail(f"proxy did not become ready (control health) within timeout — {name}")
        start = time.monotonic()
        proc.send_signal(sig)
        try:
            rc = proc.wait(timeout=SHUTDOWN_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            print(log_path.read_text(errors="ignore"), file=sys.stderr)
            proc.kill()
            proc.wait()
            _fail(f"{name}: proxy did not exit within {SHUTDOWN_EXIT_TIMEOUT_S}s of {sig.name} "
                  f"(busy-spin/no-shutdown regression?) — waited {elapsed:.2f}s")
        elapsed = time.monotonic() - start
        if rc != 0:
            print(log_path.read_text(errors="ignore"), file=sys.stderr)
            _fail(f"{name}: proxy exited {elapsed:.2f}s after {sig.name} but with nonzero code {rc}")
        print(f"[proxy-gate] {name}: exited {elapsed:.2f}s after {sig.name} (exit 0)", file=sys.stderr)
    finally:
        log_f.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert_events_present(log_path, REQUIRED_SHUTDOWN_EVENTS)
    print(f"[proxy-gate] {name}: PASS", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work-dir", required=True, help="scratch directory for the build + client-test artifacts + proxy logs")
    args = ap.parse_args()
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    binpath = build_proxy(work_dir / "build")
    for i, (name, label, client_test, required_events) in enumerate(CASES):
        if i > 0:
            time.sleep(BETWEEN_CASES_SETTLE_S)
        run_case(name, label, binpath, work_dir, client_test, required_events)
    for name, label, sig in SHUTDOWN_SIGNALS:
        time.sleep(BETWEEN_CASES_SETTLE_S)
        check_graceful_shutdown(name, label, sig, binpath, work_dir)
    print("[proxy-gate] all cases PASS", file=sys.stderr)


if __name__ == "__main__":
    main()
