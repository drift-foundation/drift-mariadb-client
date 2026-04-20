# Environment:
#   DRIFT_TOOLCHAIN_ROOT — canonical toolchain root for certification gates
#                          (gates resolve $DRIFT_TOOLCHAIN_ROOT/bin/{drift,driftc})
#   DRIFTC               — path to driftc compiler (dev convenience fallback)
#   DRIFT_PKG_ROOT       — package library root for deploy/prepare (default: build/deploy)
#   DRIFT_SIGN_KEY_FILE  — Ed25519 signing key file (required for deploy)

MANIFEST := "drift/manifest.json"
DEPLOY_DEST := env("DRIFT_PKG_ROOT", "build/deploy")
RPC_VERSION := `python3 -c "import json; m=json.load(open('drift/manifest.json')); print(next(a['version'] for a in m['artifacts'] if a['name']=='mariadb-rpc'))"`
WIRE_VERSION := `python3 -c "import json; m=json.load(open('drift/manifest.json')); print(next(a['version'] for a in m['artifacts'] if a['name']=='mariadb-wire-proto'))"`

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

# Build, sign, and publish both packages to DEPLOY_DEST.
# When called from certification gates, DRIFT_TOOLCHAIN_ROOT is set and drift
# is resolved from it. For standalone dev use, falls back to drift on PATH.
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
	"$DRIFT" deploy --dest "{{DEPLOY_DEST}}" {{ARGS}}

# --- Certification gates (orchestrator interface) ---
# These three commands are the repo's public certification surface.
# The orchestrator only cares about stable pass/fail via exit code.
# `stress` and `perf` depend on `deploy` — they compile against signed
# .zdmp packages, not local source roots. A deploy failure is a gate failure.

# Certification gate: correctness and memory safety (plain + ASAN + memcheck).
# Phase 1 (test-unit, no DB): plain + ASAN + memcheck run concurrently.
#   Per-pass compile parallelism defaults to nproc/3 to avoid triple-stacking
#   the compile storm; override via DRIFT_TEST_JOBS.
# Phase 2 (test-live, shared mdb114-a): three passes serial — the DB is a
#   shared resource and live tests mutate session/tx/metadata state.
# Requires DRIFT_TOOLCHAIN_ROOT. Resolves driftc exclusively from the toolchain root.
test:
	#!/usr/bin/env bash
	set -uo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?DRIFT_TOOLCHAIN_ROOT must be set for certification}"
	export DRIFTC="${DRIFT_TOOLCHAIN_ROOT}/bin/driftc"
	[[ -x "$DRIFTC" ]] || { echo "error: driftc not found at $DRIFTC" >&2; exit 1; }
	: "${DRIFT_TEST_JOBS:=$(( $(nproc) / 3 ))}"
	export DRIFT_TEST_JOBS
	LOG_DIR="$(mktemp -d -t drift-mdb-test-XXXXXX)"
	HB_PID=""
	cleanup() {
		[[ -n "${HB_PID}" ]] && kill "${HB_PID}" 2>/dev/null
		rm -rf "${LOG_DIR}"
	}
	trap cleanup EXIT
	echo "=== phase 1: test-unit — plain + asan + memcheck concurrent (DRIFT_TEST_JOBS=${DRIFT_TEST_JOBS} per pass, logs in ${LOG_DIR}) ==="
	( just test-unit                    > "${LOG_DIR}/unit-plain.log"    2>&1 ) & pid_plain=$!
	( DRIFT_ASAN=1     just test-unit   > "${LOG_DIR}/unit-asan.log"     2>&1 ) & pid_asan=$!
	( DRIFT_MEMCHECK=1 just test-unit   > "${LOG_DIR}/unit-memcheck.log" 2>&1 ) & pid_memcheck=$!
	(
		t=0
		while true; do
			sleep 10
			t=$((t+10))
			line="[hb ${t}s]"
			for pass in plain asan memcheck; do
				log="${LOG_DIR}/unit-${pass}.log"
				ran=$(grep -c '^run ' "${log}" 2>/dev/null); ran=${ran:-0}
				last=$(tail -n 1 "${log}" 2>/dev/null)
				line+=" ${pass}(ran=${ran}; ${last:-starting})"
			done
			echo "${line}"
		done
	) & HB_PID=$!
	status=0
	report() {
		local name="$1" pid="$2"
		if wait "${pid}"; then
			echo "=== unit-${name} — PASS ==="
		else
			echo "=== unit-${name} — FAIL ==="
			sed 's/^/[unit-'"${name}"'] /' "${LOG_DIR}/unit-${name}.log"
			status=1
		fi
	}
	report plain    "${pid_plain}"
	report asan     "${pid_asan}"
	report memcheck "${pid_memcheck}"
	kill "${HB_PID}" 2>/dev/null || true
	wait "${HB_PID}" 2>/dev/null || true
	[[ "${status}" -ne 0 ]] && exit "${status}"
	echo ""
	echo "=== phase 2: test-live — serial (shared DB at 127.0.0.1:34114) ==="
	echo "--- live: plain ---";    just test-live                    || exit 1
	echo "--- live: asan ---";     DRIFT_ASAN=1     just test-live   || exit 1
	echo "--- live: memcheck ---"; DRIFT_MEMCHECK=1 just test-live   || exit 1

# Certification gate: RPC-level protocol contamination stress (needs DB).
# Requires DRIFT_TOOLCHAIN_ROOT. Compiles against deployed signed .zdmp packages.
stress: deploy
	#!/usr/bin/env bash
	set -euo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?DRIFT_TOOLCHAIN_ROOT must be set for certification}"
	DRIFTC="${DRIFT_TOOLCHAIN_ROOT}/bin/driftc"
	[[ -x "$DRIFTC" ]] || { echo "error: driftc not found at $DRIFTC" >&2; exit 1; }
	tmp="$(mktemp -d /tmp/drift-stress.XXXXXX)"
	trap "rm -rf '$tmp'" EXIT
	echo "compile rpc_stress_test.drift (from packages)"
	"$DRIFTC" --target-word-bits 64 \
	  --package-root "{{DEPLOY_DEST}}" \
	  --dep "mariadb-rpc@{{RPC_VERSION}}" \
	  --dep "mariadb-wire-proto@{{WIRE_VERSION}}" \
	  --entry "tests.stress.rpc_stress_test::main" \
	  tests/stress/rpc_stress_test.drift \
	  -o "$tmp/stress-test"
	echo "run rpc_stress_test.drift"
	if [[ "${DRIFT_MEMCHECK:-0}" == "1" ]]; then
	  valgrind --tool=memcheck --error-exitcode=97 --leak-check=full "$tmp/stress-test"
	elif [[ "${DRIFT_MASSIF:-0}" == "1" ]]; then
	  valgrind --tool=massif --error-exitcode=97 "$tmp/stress-test"
	else
	  "$tmp/stress-test"
	fi

# Certification gate: performance regression check against machine-keyed baseline.
# Requires DRIFT_TOOLCHAIN_ROOT. Compiles against deployed signed .zdmp packages.
perf *ARGS: deploy
	#!/usr/bin/env bash
	set -euo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?DRIFT_TOOLCHAIN_ROOT must be set for certification}"
	export DRIFTC="${DRIFT_TOOLCHAIN_ROOT}/bin/driftc"
	[[ -x "$DRIFTC" ]] || { echo "error: driftc not found at $DRIFTC" >&2; exit 1; }
	python3 tools/perf_baseline.py {{ARGS}}

# Record a new perf baseline for this machine (keyed by /etc/machine-id).
# Requires DRIFT_TOOLCHAIN_ROOT.
perf-record-baseline: deploy
	#!/usr/bin/env bash
	set -euo pipefail
	: "${DRIFT_TOOLCHAIN_ROOT:?DRIFT_TOOLCHAIN_ROOT must be set for certification}"
	export DRIFTC="${DRIFT_TOOLCHAIN_ROOT}/bin/driftc"
	[[ -x "$DRIFTC" ]] || { echo "error: driftc not found at $DRIFTC" >&2; exit 1; }
	python3 tools/perf_baseline.py --record-baseline

# --- Dev workflows (not certification gates) ---
# Lighter-weight, source-level workflows for the inner dev loop.
# Compile from local source roots via the manifest (no deploy required).

# Unit tests only (no DB required).
test-unit:
	@just wire-check
	@just rpc-check

# Live/e2e tests only (needs running MariaDB instance).
test-live:
	@just wire-smoke
	@just wire-live
	@just wire-live-api
	@just wire-live-state
	@just wire-live-tx
	@just wire-live-load
	@just wire-live-metadata
	@just rpc-live-connect-state-stage
	@just rpc-live-connect-state-regression
	@just rpc-live

# --- Wire-proto tests ---

wire-check:
	@tools/drift_test_parallel_runner.sh run-all \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --test-root packages/mariadb-wire-proto/tests/unit \
	  --target-word-bits 64

wire-check-unit FILE:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --test-file "{{FILE}}" \
	  --target-word-bits 64

wire-compile-check FILE="packages/mariadb-wire-proto/src/lib.drift":
	@tools/drift_test_parallel_runner.sh compile \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --file "{{FILE}}" \
	  --target-word-bits 64

wire-smoke:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --test-file packages/mariadb-wire-proto/tests/e2e/com_query_smoke_test.drift \
	  --target-word-bits 64

wire-live:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --test-file packages/mariadb-wire-proto/tests/e2e/live_tcp_smoke_test.drift \
	  --target-word-bits 64

wire-live-api:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --test-file packages/mariadb-wire-proto/tests/e2e/live_proto_api_smoke_test.drift \
	  --target-word-bits 64

wire-live-state:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --test-file packages/mariadb-wire-proto/tests/e2e/live_session_state_test.drift \
	  --target-word-bits 64

wire-live-tx:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --test-file packages/mariadb-wire-proto/tests/e2e/live_tcp_tx_test.drift \
	  --target-word-bits 64

wire-live-load:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --test-file packages/mariadb-wire-proto/tests/e2e/live_tcp_load_test.drift \
	  --target-word-bits 64

wire-live-metadata:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-wire-proto \
	  --test-file packages/mariadb-wire-proto/tests/e2e/live_metadata_suppression_test.drift \
	  --target-word-bits 64

# --- RPC tests ---

rpc-check:
	@tools/drift_test_parallel_runner.sh run-all \
	  --manifest {{MANIFEST}} --artifact mariadb-rpc \
	  --test-root packages/mariadb-rpc/tests/unit \
	  --target-word-bits 64

rpc-check-unit FILE:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-rpc \
	  --test-file "{{FILE}}" \
	  --target-word-bits 64

rpc-check-config:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-rpc \
	  --test-file packages/mariadb-rpc/tests/unit/rpc_config_validation_test.drift \
	  --target-word-bits 64

rpc-live:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-rpc \
	  --test-file packages/mariadb-rpc/tests/e2e/live_rpc_smoke_test.drift \
	  --target-word-bits 64

rpc-live-connect-state-regression:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-rpc \
	  --test-file packages/mariadb-rpc/tests/e2e/connect_state_handoff_regression_test.drift \
	  --target-word-bits 64

rpc-live-connect-state-stage:
	@tools/drift_test_parallel_runner.sh run-one \
	  --manifest {{MANIFEST}} --artifact mariadb-rpc \
	  --test-file packages/mariadb-rpc/tests/e2e/connect_state_handoff_stage_isolation_test.drift \
	  --target-word-bits 64

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
