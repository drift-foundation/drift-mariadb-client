# Local MariaDB dev instances (isolated under tmp_db_instances/<instance>/runtime and tmp_db_instances/<instance>/config).
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

# Drift compiler helpers (expects DRIFTC in environment).
driftc-help:
	bash -lc ': "${DRIFTC:?set DRIFTC to your driftc path}"; "$DRIFTC" --help'

wire-compile-check FILE="packages/mariadb-wire-proto/src/lib.drift":
	@tools/drift_test_runner.sh compile --src-root packages/mariadb-wire-proto/src --file "{{FILE}}" --target-word-bits 64

wire-compile-check-unit FILE:
	@tools/drift_test_runner.sh compile-one --src-root packages/mariadb-wire-proto/src --file "{{FILE}}" --target-word-bits 64

wire-check:
	@tools/drift_test_runner.sh run-all --src-root packages/mariadb-wire-proto/src --test-root packages/mariadb-wire-proto/tests/unit --target-word-bits 64

wire-check-unit FILE:
	@tools/drift_test_runner.sh run-one --src-root packages/mariadb-wire-proto/src --test-file "{{FILE}}" --target-word-bits 64

wire-smoke:
	@tools/drift_test_runner.sh run-one --src-root packages/mariadb-wire-proto/src --test-file packages/mariadb-wire-proto/tests/e2e/com_query_smoke_test.drift --target-word-bits 64

wire-live:
	@tools/drift_test_runner.sh run-one --src-root packages/mariadb-wire-proto/src --test-file packages/mariadb-wire-proto/tests/e2e/live_tcp_smoke_test.drift --target-word-bits 64

wire-live-load:
	@tools/drift_test_runner.sh run-one --src-root packages/mariadb-wire-proto/src --test-file packages/mariadb-wire-proto/tests/e2e/live_tcp_load_test.drift --target-word-bits 64

wire-live-tx:
	@tools/drift_test_runner.sh run-one --src-root packages/mariadb-wire-proto/src --test-file packages/mariadb-wire-proto/tests/e2e/live_tcp_tx_test.drift --target-word-bits 64

# Capture raw wire bytes through a local TCP MITM proxy.
# Example:
# just wire-capture handshake_mdb114a 34115 34114
wire-capture SCENARIO LISTEN_PORT TARGET_PORT TARGET_HOST="127.0.0.1":
	@python3 tools/wire_capture_proxy.py --scenario "{{SCENARIO}}" --listen-port "{{LISTEN_PORT}}" --target-host "{{TARGET_HOST}}" --target-port "{{TARGET_PORT}}"

# List available capture runs under tests/fixtures/scenarios/bin.
wire-capture-list:
	@bash -lc 'find tests/fixtures/scenarios/bin -mindepth 2 -maxdepth 2 -type d 2>/dev/null | sort || true'

# Convert one capture run into packetized fixtures for deterministic replay.
wire-fixture-extract SCENARIO RUN_ID:
	@python3 tools/wire_fixture_extract.py --scenario "{{SCENARIO}}" --run-id "{{RUN_ID}}"
	@python3 tools/write_scenario_sql.py --scenario "{{SCENARIO}}" --run-id "{{RUN_ID}}"
