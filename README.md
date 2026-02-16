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

## Dependencies

- `bash`
- `just`
- `docker` with Compose support (`docker compose` or `docker-compose`)

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
