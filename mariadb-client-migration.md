# MariaDB Client Migration Bundle

This file captures the changes we made in the wrong repo so you can copy them into `drift-mariadb-client`.

## 1) `work-progress.md` (copy to new repo root)

```md
# MariaDB Client Work Progress

## Goal

Provide a Drift-native MariaDB client focused on Stored Procedure calls, with clear separation between protocol mechanics and RPC-style usage.

## Pinned architecture

Two packages in one repository:

1. `mariadb-wire-proto`
- Owns wire protocol concerns only.
- Responsibilities:
  - packet framing/deframing
  - handshake and capability negotiation (MVP-constrained)
  - auth flow (MVP-constrained plugin set)
  - command/response state machine (`COM_QUERY` first)
  - result/OK/ERR packet decoding
- No business-level API for “call procedure”.

2. `mariadb-rpc`
- SP-oriented API built on `mariadb-wire-proto`.
- Responsibilities:
  - `call(proc_name, args)` style surface
  - SQL call construction for stored procedures (MVP)
  - mapping protocol results to Drift-friendly return shapes
  - error tagging suitable for machine handling
- No direct packet logic.

## Why split into two packages

- Keeps low-level protocol isolated and testable.
- Allows iterative replacement/extension of RPC behavior without destabilizing protocol code.
- Lets future users consume raw wire package for non-SP use cases.

## MVP constraints (explicit)

- Server: controlled MariaDB version(s).
- Auth: basic constrained mode(s) only.
- TLS: disabled in MVP.
- Operations: Stored Procedure invocation only (`COM_QUERY` path first).
- Concurrency model: integrates with Drift virtual-thread runtime through existing network I/O primitives.

## Proposed phases

### Phase 0: Contract pinning
- Finalize package names and public module ids.
- Pin `mariadb-rpc` API signatures and error tags.
- Pin supported auth plugin(s) and server capability assumptions.

### Phase 1: Wire foundations (`mariadb-wire-proto`)
- Packet reader/writer + length-encoded primitives.
- Handshake/auth happy path.
- `COM_QUERY` request + OK/ERR/resultset decode.
- Deterministic parser tests with fixed binary fixtures.

### Phase 2: RPC layer (`mariadb-rpc`)
- Stored procedure call builder.
- Arg encoding rules (MVP subset).
- Result mapping for common SP return patterns.
- Error tag normalization.

### Phase 3: Integration/hardening
- E2E with real MariaDB instance in controlled config.
- Negative tests: auth fail, malformed response, server error packets.
- Stress/concurrency smoke via virtual threads.

## Initial test plan

- Unit (`mariadb-wire-proto`):
  - packet codec roundtrip
  - handshake decode
  - ERR/OK/resultset packet parsing
- Unit (`mariadb-rpc`):
  - proc-call SQL generation
  - arg encoding/escaping for pinned subset
  - response mapping
- E2E:
  - connect + call simple SP
  - SP returning scalar/resultset
  - server-side error propagation with stable tags

## Open decisions to pin next

1. Exact `mariadb-rpc` public API signatures.
2. Supported argument types in MVP.
3. Transaction semantics in MVP (explicitly out or minimal support).
4. Connection lifecycle/pooling shape (single connection first vs pool-first).

## Status

- Planned and pinned at architecture level.
- Implementation not started.
```

## 2) `tools/db_instance.sh` (copy to new repo)

Create `tools/db_instance.sh` and mark executable (`chmod +x tools/db_instance.sh`):

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${ROOT_DIR}/tmp_db_instance"
CONFIG_ROOT="${ROOT_DIR}/db_instances_config"

usage() {
	echo "usage:"
	echo "  $0 create <instance> [host_port] [image]"
	echo "  $0 up <instance>"
	echo "  $0 down <instance>"
	echo "  $0 ps <instance>"
	echo "  $0 logs <instance>"
	echo "  $0 rm <instance>"
	echo "  $0 sql <instance> <sql>"
	exit 1
}

compose_cmd() {
	if docker compose version >/dev/null 2>&1; then
		echo "docker compose"
		return
	fi
	if command -v docker-compose >/dev/null 2>&1; then
		echo "docker-compose"
		return
	fi
	echo "error: docker compose is not available" >&2
	exit 1
}

require_instance() {
	local instance="$1"
	if [[ -z "${instance}" ]]; then
		echo "error: instance is required" >&2
		usage
	fi
}

slot_index() {
	local slot="$1"
	local code
	code=$(printf "%d" "'${slot}")
	echo $((code - 96))
}

derived_port() {
	local instance="$1"
	if [[ "${instance}" =~ ^mdb([0-9]+)-([a-z])$ ]]; then
		local ver="${BASH_REMATCH[1]}"
		local slot="${BASH_REMATCH[2]}"
		local idx
		idx=$(slot_index "${slot}")
		if ((idx < 1)); then
			echo "error: invalid slot in instance '${instance}'" >&2
			exit 1
		fi
		# Predictable port layout:
		# - version bucket by mdb<ver>-*
		# - slot offsets in +5 steps (a=+0, b=+5, c=+10, ...)
		# Example: mdb114-a -> 34114, mdb114-b -> 34119, mdb114-c -> 34124.
		echo $((34000 + ver + (idx - 1) * 5))
		return
	fi
	echo "error: cannot derive host port for '${instance}', expected pattern mdb<version>-<slot> (example: mdb114-a)" >&2
	exit 1
}

render_env() {
	local instance="$1"
	local host_port="$2"
	cat <<EOF
INSTANCE_NAME=${instance}
HOST_PORT=${host_port}
ROOT_PASSWORD=rootpw
APP_DB=appdb
APP_USER=app
APP_PASSWORD=apppw
IMAGE=mariadb:11.4
EOF
}

render_my_cnf() {
	cat <<'EOF'
[mysqld]
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
skip_name_resolve=ON
bind-address=0.0.0.0
EOF
}

render_compose() {
	local runtime_dir="$1"
	local config_dir="$2"
	cat <<EOF
services:
  db:
    image: \${IMAGE}
    container_name: \${INSTANCE_NAME}
    ports:
      - "\${HOST_PORT}:3306"
    environment:
      MARIADB_ROOT_PASSWORD: \${ROOT_PASSWORD}
      MARIADB_DATABASE: \${APP_DB}
      MARIADB_USER: \${APP_USER}
      MARIADB_PASSWORD: \${APP_PASSWORD}
    volumes:
      - ${runtime_dir}/data:/var/lib/mysql
      - ${runtime_dir}/log:/var/log/mysql
      - ${runtime_dir}/tmp:/tmp
      - ${config_dir}/conf.d:/etc/mysql/conf.d
      - ${config_dir}/init:/docker-entrypoint-initdb.d
    command: ["--bind-address=0.0.0.0"]
    restart: unless-stopped
EOF
}

compose_for() {
	local instance="$1"
	local runtime_dir="${RUNTIME_ROOT}/${instance}"
	local config_dir="${CONFIG_ROOT}/${instance}"
	local env_file="${runtime_dir}/run.env"
	local compose_file="${config_dir}/compose.yaml"
	if [[ ! -f "${env_file}" ]]; then
		echo "error: missing ${env_file}, run create first" >&2
		exit 1
	fi
	if [[ ! -f "${compose_file}" ]]; then
		echo "error: missing ${compose_file}, run create first" >&2
		exit 1
	fi
	echo "$(compose_cmd) --env-file ${env_file} -f ${compose_file}"
}

cmd_create() {
	local instance="$1"
	local host_port="${2:-}"
	local image="${3:-mariadb:11.4}"
	require_instance "${instance}"
	if [[ -z "${host_port}" ]]; then
		host_port="$(derived_port "${instance}")"
	fi
	local runtime_dir="${RUNTIME_ROOT}/${instance}"
	local config_dir="${CONFIG_ROOT}/${instance}"
	mkdir -p "${runtime_dir}/data" "${runtime_dir}/log" "${runtime_dir}/tmp" "${config_dir}/conf.d" "${config_dir}/init"
	render_env "${instance}" "${host_port}" > "${runtime_dir}/run.env"
	sed -i "s|^IMAGE=.*|IMAGE=${image}|" "${runtime_dir}/run.env"
	render_my_cnf > "${config_dir}/conf.d/my.cnf"
	render_compose "${runtime_dir}" "${config_dir}" > "${config_dir}/compose.yaml"
	echo "created instance '${instance}'"
	echo "runtime: ${runtime_dir}"
	echo "config:  ${config_dir}"
	echo "port:    ${host_port}"
	echo "image:   ${image}"
}

cmd_up() {
	local instance="$1"
	require_instance "${instance}"
	local cmd
	cmd="$(compose_for "${instance}")"
	${cmd} up -d
}

cmd_down() {
	local instance="$1"
	require_instance "${instance}"
	local cmd
	cmd="$(compose_for "${instance}")"
	${cmd} down
}

cmd_ps() {
	local instance="$1"
	require_instance "${instance}"
	local cmd
	cmd="$(compose_for "${instance}")"
	${cmd} ps
}

cmd_logs() {
	local instance="$1"
	require_instance "${instance}"
	local cmd
	cmd="$(compose_for "${instance}")"
	${cmd} logs -f --tail=200
}

cmd_rm() {
	local instance="$1"
	require_instance "${instance}"
	local cmd
	cmd="$(compose_for "${instance}")"
	${cmd} down -v --remove-orphans
	rm -rf "${RUNTIME_ROOT}/${instance}" "${CONFIG_ROOT}/${instance}"
	echo "removed instance '${instance}'"
}

cmd_sql() {
	local instance="$1"
	local sql="$2"
	require_instance "${instance}"
	if [[ -z "${sql}" ]]; then
		echo "error: sql is required" >&2
		usage
	fi
	local runtime_dir="${RUNTIME_ROOT}/${instance}"
	local env_file="${runtime_dir}/run.env"
	if [[ ! -f "${env_file}" ]]; then
		echo "error: missing ${env_file}, run create first" >&2
		exit 1
	fi
	# shellcheck disable=SC1090
	source "${env_file}"
	docker exec -i "${INSTANCE_NAME}" mariadb -uroot "-p${ROOT_PASSWORD}" -e "${sql}"
}

main() {
	local action="${1:-}"
	case "${action}" in
		create)
			shift
			cmd_create "${1:-}" "${2:-}" "${3:-}"
			;;
		up)
			shift
			cmd_up "${1:-}"
			;;
		down)
			shift
			cmd_down "${1:-}"
			;;
		ps)
			shift
			cmd_ps "${1:-}"
			;;
		logs)
			shift
			cmd_logs "${1:-}"
			;;
		rm)
			shift
			cmd_rm "${1:-}"
			;;
		sql)
			shift
			cmd_sql "${1:-}" "${2:-}"
			;;
		*)
			usage
			;;
	esac
}

main "$@"
```

## 3) `justfile` block (append to new repo `justfile`)

```just
# Local MariaDB dev instances (isolated under tmp_db_instance/<instance> and db_instances_config/<instance>).
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
```

## 4) `.gitignore` entries (append in new repo)

```gitignore
tmp_db_instance/
db_instances_config/
```

