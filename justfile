# Environment:
#   DRIFT_TOOLCHAIN_ROOT — canonical toolchain root for certification gates
#                          (gates resolve $DRIFT_TOOLCHAIN_ROOT/bin/{drift,driftc})
#   DRIFTC               — path to driftc compiler (dev convenience fallback)
#   DRIFT_PKG_ROOT       — package library root for deploy/prepare (default: build/deploy)
#   DRIFT_SIGN_KEY_FILE  — Ed25519 signing key file: cert-claim signer for
#                          `just deploy`; author-claim signer for `just author-claim`

MANIFEST := "drift/manifest.json"
DEPLOY_DEST := env("DRIFT_PKG_ROOT", "build/deploy")
RPC_VERSION := `python3 -c "import json; m=json.load(open('drift/manifest.json')); print(next(a['version'] for a in m['artifacts'] if a['name']=='mariadb-rpc'))"`
WIRE_VERSION := `python3 -c "import json; m=json.load(open('drift/manifest.json')); print(next(a['version'] for a in m['artifacts'] if a['name']=='mariadb-wire-proto'))"`

# Cert gates run on the toolchain's shared scenario-agnostic executor
# (`$DRIFT_TOOLCHAIN_ROOT/lib/tools/drift_test_run.py`) driven by our plan emitter
# (`tools/emit_test_plan.py`). The executor owns the mechanism (parallel compile
# under the flocker pool, dedup, valgrind wrap, heartbeat, host concurrency
# budget); the emitter owns policy (which files/deps/lanes); the gate harness
# below owns resource bracketing (DB must be up; `deploy`; perf measurement).
# Test roots, live-test lists, and the DB resource key all live in the emitter now
# (its `UNIT_ROOTS` / `LIVE_TESTS` / `DB_GROUP`) — single source of truth.
#
# Budget: do NOT set DRIFT_TEST_JOBS to raise the pool — on 0.33.17 the executor's
# budget helper auto-detects full physical cores. Set it only to TRIM a
# RAM-constrained box (or mark a lane serial in the plan).
#
# Heartbeat cadence (seconds) for the executor's `--heartbeat` watchdog feed (and
# the perf gate's monitor, which must span its harness measurement step too).
HEARTBEAT_SECS := "30"

# --- Package lifecycle ---

# Resolve dependencies and write drift/lock.json.
prepare:
	#!/usr/bin/env bash
	set -euo pipefail
	if [[ -n "${DRIFT_TOOLCHAIN_ROOT:-}" ]]; then
	  DRIFT="${DRIFT_TOOLCHAIN_ROOT}/bin/drift"
	  [[ -x "$DRIFT" ]] || { echo "error: drift not found at $DRIFT" >&2; exit 1; }
	else
	  DRIFT="drift"
	fi
	"$DRIFT" prepare --dest "{{DEPLOY_DEST}}"

# Re-mint drift/mariadb-{wire-proto,rpc}.author-claim under the Foundation
# author key. Runs the manifest-aware drift-author publish (0.32.3+ only)
# once per library artifact, since the manifest declares two. Use after any
# source change that affects SCI (modules, assets, deps, version).
#
# The author-claims are then committed; the orchestrator emits the cert-claim
# during certification, so the author key never enters the deploy host.
# Override the seed via DRIFT_SIGN_KEY_FILE; DRIFT_LANG_ROOT defaults to
# ~/src/drift-lang.
author-claim:
	#!/usr/bin/env bash
	set -euo pipefail
	DRIFT_LANG_ROOT="${DRIFT_LANG_ROOT:-${HOME}/src/drift-lang}"
	KEY_FILE="${DRIFT_SIGN_KEY_FILE:-${HOME}/.config/drift/keys/default.seed}"
	[[ -d "${DRIFT_LANG_ROOT}/tools/drift_author" ]] || { echo "error: tools.drift_author not found at ${DRIFT_LANG_ROOT}" >&2; exit 1; }
	[[ -f "${KEY_FILE}" ]] || { echo "error: signing key not found: ${KEY_FILE}" >&2; exit 1; }
	for ART in mariadb-wire-proto mariadb-rpc; do
	  echo "[author-claim] minting drift/${ART}.author-claim"
	  PYTHONPATH="${DRIFT_LANG_ROOT}" python3 -m tools.drift_author publish \
	    --manifest "$(pwd)/drift/manifest.json" \
	    --artifact "${ART}" \
	    --key-file "${KEY_FILE}" \
	    --overwrite
	done

# Read-only trust preflight: validates author claims, SCI equality, and trust
# grants against drift/manifest.json (what `drift deploy` will check). Run it to
# confirm the repo is deploy-ready without actually deploying.
trust-check:
	#!/usr/bin/env bash
	set -euo pipefail
	if [[ -n "${DRIFT_TOOLCHAIN_ROOT:-}" ]]; then
	  DRIFT="${DRIFT_TOOLCHAIN_ROOT}/bin/drift"
	  [[ -x "$DRIFT" ]] || { echo "error: drift not found at $DRIFT" >&2; exit 1; }
	else
	  DRIFT="drift"
	fi
	"$DRIFT" trust check

# Always runs both author-claim + prepare: `prepare` is idempotent (a no-op when
# deps are unchanged) and `author-claim --overwrite` is deterministic (a no-op
# for an artifact whose source didn't change), so over-running is free and
# removes the guesswork. Run `just test` separately first — reseal does not test.
#
# Re-mint author-claims + re-resolve lock + trust-check; run before committing a version bump.
reseal:
	@just author-claim
	@just prepare
	@just trust-check
	@echo "[reseal] done — review & commit: drift/manifest.json, drift/lock.json, drift/*.author-claim"

# Build, sign, and publish both packages to DEPLOY_DEST.
# When called from certification gates, DRIFT_TOOLCHAIN_ROOT is set and drift
# is resolved from it. For standalone dev use, falls back to drift on PATH.
#
# Under trust-v1 (0.32.x+), this consumes drift/<pkg>.author-claim (committed)
# and emits cert-claim sidecars per artifact. The cert-claim requires either
# real cert-suite evidence or the explicit no-evidence sentinel.
#
# If ARGS contain any --cert-suite-* flag we leave them alone; otherwise we
# default to `--cert-suite-id mariadb-client/dev --cert-suite-no-evidence` so
# the inner-dev loop works without orchestrator context.
#
# Scope of that default:
#   - `just deploy ...ARGS` from the orch (passes its own --cert-suite-* flags):
#     the default is bypassed; the orch's evidence-bearing claim is used.
#   - `just stress` / `just perf` (depend on bare `deploy`, no ARGS): always
#     use the dev default. These gates never produce evidence-bearing claims
#     locally; release certification is an orchestrator-side flow that calls
#     `drift deploy` directly with --cert-suite-id / --cert-suite-evidence-sha256.
deploy *ARGS:
	#!/usr/bin/env bash
	set -euo pipefail
	if [[ -n "${DRIFT_TOOLCHAIN_ROOT:-}" ]]; then
	  DRIFT="${DRIFT_TOOLCHAIN_ROOT}/bin/drift"
	  [[ -x "$DRIFT" ]] || { echo "error: drift not found at $DRIFT" >&2; exit 1; }
	else
	  DRIFT="drift"
	fi
	mkdir -p "{{DEPLOY_DEST}}"
	EXTRA=""
	if [[ "{{ARGS}}" != *--cert-suite* ]]; then
	  EXTRA="--cert-suite-id mariadb-client/dev --cert-suite-no-evidence"
	fi
	"$DRIFT" deploy --dest "{{DEPLOY_DEST}}" ${EXTRA} {{ARGS}}

# --- Certification gates (orchestrator interface) ---
# These three commands are the repo's public certification surface.
# The orchestrator only cares about stable pass/fail via exit code.
# `stress` and `perf` depend on `deploy` — they compile against signed
# .zdmp packages, not local source roots. A deploy failure is a gate failure.

# Certification gate: correctness and memory safety (base + sanitizer + memcheck).
# Runs on the toolchain's shared `drift_test_run` executor (mechanism), driven by
# `tools/emit_test_plan.py` (policy). The emitted plan has three phases:
#   build    — every unit + live test x {base (--sanitize none), asan
#              (--sanitize address)}, compiled in PARALLEL under the flocker pool;
#              memcheck needs no build (it reuses the base binary via wrap).
#   run-unit — base / memcheck (valgrind-wrapped base) / asan, in PARALLEL.
#   run-live — base / asan / memcheck, all in ONE `mode:serial` group on the DB
#              resource key, so the shared mdb114-a is accessed one-at-a-time
#              across this gate AND any concurrent cert lane.
# The executor's `--heartbeat` feeds a stdout-inactivity watchdog through the
# silent compile / valgrind stretches; its budget auto-detects full physical
# cores (we do NOT set DRIFT_TEST_JOBS). Requires DRIFT_TOOLCHAIN_ROOT (the
# executor resolves driftc/flocker from its own toolchain root); mdb114-a up.
test:
	#!/usr/bin/env bash
	set -uo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?DRIFT_TOOLCHAIN_ROOT must be set for certification}"
	RUNNER="${DRIFT_TOOLCHAIN_ROOT}/lib/tools/drift_test_run.py"
	[[ -f "$RUNNER" ]] || { echo "error: shared executor not found at $RUNNER (need toolchain >= 0.33.17)" >&2; exit 1; }
	WORK="$(mktemp -d -t drift-mdb-test-XXXXXX)"
	trap 'rm -rf "$WORK"' EXIT
	python3 tools/emit_test_plan.py test --out "$WORK/plan.json"
	python3 "$RUNNER" --plan "$WORK/plan.json" --work-dir "$WORK" --heartbeat {{HEARTBEAT_SECS}}

# Certification gate: RPC-level protocol contamination stress (needs DB).
# `deploy` (harness) publishes the signed .zdmp packages; the emitted plan then
# compiles the stress scenario against them under the flocker pool (build phase)
# and runs it in a `mode:serial` group on the DB resource key — shared with the
# test gate's live phase, so they never hit mdb114-a concurrently. The executor's
# `--heartbeat` covers the silent compile/run. Requires DRIFT_TOOLCHAIN_ROOT.
stress: deploy
	#!/usr/bin/env bash
	set -uo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?DRIFT_TOOLCHAIN_ROOT must be set for certification}"
	RUNNER="${DRIFT_TOOLCHAIN_ROOT}/lib/tools/drift_test_run.py"
	[[ -f "$RUNNER" ]] || { echo "error: shared executor not found at $RUNNER (need toolchain >= 0.33.17)" >&2; exit 1; }
	WORK="$(mktemp -d /tmp/drift-stress.XXXXXX)"
	trap 'rm -rf "$WORK"' EXIT
	python3 tools/emit_test_plan.py stress --out "$WORK/plan.json"
	python3 "$RUNNER" --plan "$WORK/plan.json" --work-dir "$WORK" --heartbeat {{HEARTBEAT_SECS}}

# Certification gate: performance regression check against machine-keyed baseline.
# `deploy` (harness) publishes the packages; the emitted BUILD-ONLY plan compiles
# the perf scenarios against them in PARALLEL under the executor's flocker pool
# (phase 1). Measurement (phase 2) is HARNESS — perf_baseline.py --measure-only
# runs the prebuilt binaries SERIALLY under an exclusive `flocker -j1 --key
# mariadb-perf-measure` (the wire-capture proxy port / measured host), threading
# the baseline gate (which the executor deliberately won't do). A recipe-level
# `flocker --heartbeat` monitor spans BOTH the executor build and the harness
# measurement on live stdout. Only bytes/packets are gated (elapsed_ms excluded),
# so a busy machine's slower timings won't regress the gate.
perf *ARGS: deploy
	#!/usr/bin/env bash
	set -uo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?DRIFT_TOOLCHAIN_ROOT must be set for certification}"
	export DRIFTC="${DRIFT_TOOLCHAIN_ROOT}/bin/driftc"
	RUNNER="${DRIFT_TOOLCHAIN_ROOT}/lib/tools/drift_test_run.py"
	FLK="${DRIFT_TOOLCHAIN_ROOT}/bin/flocker"
	[[ -f "$RUNNER" && -x "$FLK" ]] || { echo "error: executor/flocker not found under $DRIFT_TOOLCHAIN_ROOT (need toolchain >= 0.33.17)" >&2; exit 1; }
	WORK="$(mktemp -d /tmp/drift-perf.XXXXXX)"
	HB_PID=""
	cleanup() { [[ -n "$HB_PID" ]] && kill "$HB_PID" 2>/dev/null; rm -rf "$WORK"; }
	trap cleanup EXIT
	"$FLK" --key mdb-perf-hb -j 1 --heartbeat {{HEARTBEAT_SECS}} -- sleep 86400 & HB_PID=$!
	echo "=== perf phase 1: compile scenarios (parallel, executor flocker pool) ==="
	python3 tools/emit_test_plan.py perf --out "$WORK/plan.json"
	python3 "$RUNNER" --plan "$WORK/plan.json" --work-dir "$WORK" || exit 1
	echo "=== perf phase 2: measure (serial, exclusive flocker -j1 --key mariadb-perf-measure) ==="
	"$FLK" --key mariadb-perf-measure -j 1 -- python3 tools/perf_baseline.py --measure-only --bin-dir "$WORK" {{ARGS}} || exit 1

# Record a new perf baseline for this machine (keyed by /etc/machine-id).
# Same executor-build / exclusive-harness-measure split as `perf`, but records.
perf-record-baseline: deploy
	#!/usr/bin/env bash
	set -uo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?DRIFT_TOOLCHAIN_ROOT must be set for certification}"
	export DRIFTC="${DRIFT_TOOLCHAIN_ROOT}/bin/driftc"
	RUNNER="${DRIFT_TOOLCHAIN_ROOT}/lib/tools/drift_test_run.py"
	FLK="${DRIFT_TOOLCHAIN_ROOT}/bin/flocker"
	[[ -f "$RUNNER" && -x "$FLK" ]] || { echo "error: executor/flocker not found under $DRIFT_TOOLCHAIN_ROOT (need toolchain >= 0.33.17)" >&2; exit 1; }
	WORK="$(mktemp -d /tmp/drift-perf.XXXXXX)"
	HB_PID=""
	cleanup() { [[ -n "$HB_PID" ]] && kill "$HB_PID" 2>/dev/null; rm -rf "$WORK"; }
	trap cleanup EXIT
	"$FLK" --key mdb-perf-hb -j 1 --heartbeat {{HEARTBEAT_SECS}} -- sleep 86400 & HB_PID=$!
	echo "=== perf-record phase 1: compile scenarios (parallel, executor flocker pool) ==="
	python3 tools/emit_test_plan.py perf --out "$WORK/plan.json"
	python3 "$RUNNER" --plan "$WORK/plan.json" --work-dir "$WORK" || exit 1
	echo "=== perf-record phase 2: measure + record (serial, exclusive flocker -j1 --key mariadb-perf-measure) ==="
	"$FLK" --key mariadb-perf-measure -j 1 -- python3 tools/perf_baseline.py --measure-only --bin-dir "$WORK" --record-baseline || exit 1

# --- Dev workflows (not certification gates) ---
# The shared executor runs these too — a one-test or compile-check plan from the
# same emitter (tools/emit_test_plan.py). The old per-suite / per-test recipes
# (test-unit, test-live, wire-check, rpc-live-*, build-*, run-*-prebuilt, …) are
# retired: emit a one-test plan for any file instead.

# Build + run a single test through the executor (fast inner-loop iteration).
#   just check-one packages/mariadb-rpc/tests/e2e/live_rpc_smoke_test.drift
check-one FILE:
	#!/usr/bin/env bash
	set -uo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?set DRIFT_TOOLCHAIN_ROOT to a toolchain >= 0.33.17}"
	RUNNER="${DRIFT_TOOLCHAIN_ROOT}/lib/tools/drift_test_run.py"
	[[ -f "$RUNNER" ]] || { echo "error: shared executor not found at $RUNNER (need toolchain >= 0.33.17)" >&2; exit 1; }
	WORK="$(mktemp -d /tmp/drift-mdb-one.XXXXXX)"
	trap 'rm -rf "$WORK"' EXIT
	python3 tools/emit_test_plan.py one --file "{{FILE}}" --out "$WORK/plan.json"
	python3 "$RUNNER" --plan "$WORK/plan.json" --work-dir "$WORK"

# Type-check a single file against its artifact's sources (no entry, no run).
#   just compile-check packages/mariadb-rpc/src/lib.drift
compile-check FILE:
	#!/usr/bin/env bash
	set -uo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?set DRIFT_TOOLCHAIN_ROOT to a toolchain >= 0.33.17}"
	RUNNER="${DRIFT_TOOLCHAIN_ROOT}/lib/tools/drift_test_run.py"
	[[ -f "$RUNNER" ]] || { echo "error: shared executor not found at $RUNNER (need toolchain >= 0.33.17)" >&2; exit 1; }
	WORK="$(mktemp -d /tmp/drift-mdb-compile.XXXXXX)"
	trap 'rm -rf "$WORK"' EXIT
	python3 tools/emit_test_plan.py compile --file "{{FILE}}" --out "$WORK/plan.json"
	python3 "$RUNNER" --plan "$WORK/plan.json" --work-dir "$WORK"

# --- Local MariaDB dev instances ---

db-create INSTANCE HOST_PORT="" IMAGE="mariadb:11.4":
	tools/db_instance.sh create "{{INSTANCE}}" "{{HOST_PORT}}" "{{IMAGE}}"

db-up INSTANCE:
	tools/db_instance.sh up "{{INSTANCE}}"

db-down INSTANCE:
	tools/db_instance.sh down "{{INSTANCE}}"

db-ps INSTANCE:
	tools/db_instance.sh ps "{{INSTANCE}}"

db-logs INSTANCE:
	tools/db_instance.sh logs "{{INSTANCE}}"

db-rm INSTANCE:
	tools/db_instance.sh rm "{{INSTANCE}}"

db-sql INSTANCE SQL:
	tools/db_instance.sh sql "{{INSTANCE}}" "{{SQL}}"

db-load-schema INSTANCE SQL_FILE="tests/fixtures/appdb_schema.sql":
	tools/db_instance.sh schema-load "{{INSTANCE}}" "{{SQL_FILE}}"

# --- Utilities ---

driftc-help:
	bash -lc ': "${DRIFTC:?set DRIFTC to your driftc path}"; "$DRIFTC" --help'

# Capture raw wire bytes through a local TCP MITM proxy.
wire-capture SCENARIO LISTEN_PORT TARGET_PORT TARGET_HOST="127.0.0.1":
	@python3 tools/wire_capture_proxy.py --scenario "{{SCENARIO}}" --listen-port "{{LISTEN_PORT}}" --target-host "{{TARGET_HOST}}" --target-port "{{TARGET_PORT}}"

wire-capture-list:
	@bash -lc 'find tests/fixtures/scenarios/bin -mindepth 2 -maxdepth 2 -type d 2>/dev/null | sort || true'

wire-fixture-extract SCENARIO RUN_ID:
	@python3 tools/wire_fixture_extract.py --scenario "{{SCENARIO}}" --run-id "{{RUN_ID}}"
	@python3 tools/write_scenario_sql.py --scenario "{{SCENARIO}}" --run-id "{{RUN_ID}}"
