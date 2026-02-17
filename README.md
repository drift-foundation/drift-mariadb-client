# mariadb-client

Drift user-land MariaDB client project.

This repository is intentionally not part of Drift stdlib. It is a curated user-land library intended for package-index publication and normal third-party consumption.

## Packages

1. `packages/mariadb-wire-proto`
- Low-level MariaDB wire protocol implementation.
- Packet codec, handshake/auth, command/response state machine.

2. `packages/mariadb-rpc`
- Stored-procedure-oriented client API on top of `mariadb-wire-proto`.
- Drift-friendly call and result mapping for app code.

## Scope (MVP)

- MariaDB server versions controlled by project.
- Basic auth mode(s) only.
- TLS disabled for MVP.
- Stored procedure workflow first (via `COM_QUERY` path).

## Protocol References

- MariaDB Client/Server Protocol (overview): https://mariadb.com/docs/server/reference/clientserver-protocol
- Packet format: https://mariadb.com/docs/server/reference/clientserver-protocol/0-packet
- Connection/handshake phase: https://mariadb.com/docs/server/reference/clientserver-protocol/1-connecting/connection
- MariaDB vs MySQL protocol differences: https://mariadb.com/docs/server/reference/clientserver-protocol/mariadb-protocol-differences-with-mysql
- Accessed: 2026-02-17

## Dependencies

- `bash`
- `just`
- `docker` with Compose support (`docker compose` or `docker-compose`)
- `driftc` (set `DRIFTC` to the compiler path, for example `/home/sl/src/drift-lang/bin/driftc`)

### Compiler env

- `DRIFTC` should point to the compiler launcher.

### Build Support Flags

- `DRIFT_ALLOC_TRACK=1`
  - Enables allocator tracking instrumentation in runtime and enforces per-case leak checks when expected config requires it.
- `DRIFT_MEMCHECK=1`
  - For execute-time checks (`just wire-check`, `just wire-check-unit ...`), runs binaries under `valgrind --tool=memcheck`.
- `DRIFT_MASSIF=1`
  - For execute-time checks (`just wire-check`, `just wire-check-unit ...`), runs binaries under `valgrind --tool=massif`.
- `DRIFT_ASAN=1`
  - For execute-time checks, sets default `ASAN_OPTIONS=detect_leaks=0:halt_on_error=1` unless explicitly provided.
  - Incompatible with `DRIFT_MEMCHECK` and `DRIFT_MASSIF` in the same run.

### Wire Recipes

- `just wire-check`
  - Compile and execute all unit test entrypoints under `packages/mariadb-wire-proto/tests/unit`.
- `just wire-check-unit packages/mariadb-wire-proto/tests/unit/packet_header_test.drift`
  - Compile and execute one unit test entrypoint.
- `just wire-compile-check`
  - Compile-only check for library sources.
- `just wire-compile-check-unit <test-file>`
  - Compile-only check for a specific unit test entrypoint.

## Local MariaDB Dev Instances

### Layout (generated, not checked in)

- `tmp_db_instances/<instance>/runtime/data`
- `tmp_db_instances/<instance>/runtime/log`
- `tmp_db_instances/<instance>/runtime/tmp`
- `tmp_db_instances/<instance>/runtime/run.env`
- `tmp_db_instances/<instance>/config/compose.yaml`
- `tmp_db_instances/<instance>/config/conf.d/my.cnf`
- `tmp_db_instances/<instance>/config/init/`

### Naming and auto-port scheme

- Use instance names like `mdb114-a`, `mdb114-b`, `mdb114-c`.
- Port formula: `34000 + version + (slot_index - 1) * 5`.
- Examples:
- `mdb114-a` -> `34114`
- `mdb114-b` -> `34119`
- `mdb114-c` -> `34124`

### Commands (`just db-*`)

- `just db-create mdb114-a`
- `just db-up mdb114-a`
- `just db-ps mdb114-a`
- `just db-logs mdb114-a`
- `just db-sql mdb114-a "SELECT 1;"`
- `just db-down mdb114-a`
- `just db-rm mdb114-a`
- Override host port and image:
- `just db-create mdb114-b 34080 mariadb:11.4`

### Notes

- Data and config both live under `tmp_db_instances/<instance>/` (`runtime/` and `config/`).
- You can run multiple instances concurrently by using distinct instance names.
- `tmp_db_instances/` must stay git-ignored.

### Safety

- Instance names are strictly validated (`^mdb[0-9]+-[a-z]$`).
- `run.env` is never `source`d; expected keys are parsed explicitly.
- Docker compose is invoked with argv arrays (no fragile command-string splitting).

## Repository layout

```text
packages/
  mariadb-wire-proto/
  mariadb-rpc/
examples/
tests/
docs/
work-progress.md
AGENTS.md
```

## Development policy

- Track Drift toolchain `main` (see `AGENTS.md`).
- Regression-first for core defects.
- No workaround-only masking for protocol/concurrency/lifetime bugs.
