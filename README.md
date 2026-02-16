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
