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

wire-check FILE="packages/mariadb-wire-proto/src/lib.drift":
	bash -lc ': "${DRIFTC:?set DRIFTC to your driftc path}"; SRC_FILES="$(find packages/mariadb-wire-proto/src -type f -name "*.drift" | sort)"; "$DRIFTC" --target-word-bits 64 $SRC_FILES "{{FILE}}"'

wire-check-unit FILE:
	bash -lc ': "${DRIFTC:?set DRIFTC to your driftc path}"; SRC_FILES="$(find packages/mariadb-wire-proto/src -type f -name "*.drift" | sort)"; "$DRIFTC" --target-word-bits 64 $SRC_FILES "{{FILE}}"'
