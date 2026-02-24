# Effective MariaDB RPC Usage

Audience: application developers using `mariadb-rpc`.

Status: living guide. Update as API stabilizes.

## Goals

- Provide practical guidance for calling stored procedures safely and efficiently.
- Keep wire-level mechanics out of app code.

## Core principles

- Prefer RPC API over wire API for normal application development.
- Treat stored procedure calls as streamed responses, not pre-buffered blobs.
- Keep transaction scope small and explicit.
- Return connections to pool only after reset/sanitization.
- Use `call(...)` as the statement entrypoint; consume via statement events.

## Statement model (streaming-first)

- `conn.call(...)` starts one statement and returns `RpcStatement`.
- Consume results incrementally with:
  - `stmt.next_event()`
  - `stmt.skip_result()`
  - `stmt.skip_remaining()`
- There is no `query_all`/buffer-all API in MVP.

## Single active statement rule

- One connection supports one active statement at a time.
- Practical implication:
  - finish a statement (consume/skip) before starting the next call.
- This is pool-friendly and avoids overlapping response streams on one socket.

## Drain semantics and pool safety

- Preferred explicit drain when you do not need all results:
  - `stmt.skip_result()` to jump to the next resultset.
  - `stmt.skip_remaining()` to finish the statement.
- If a statement is dropped before terminal event, wire-layer destruction drains remaining packets.
- Before returning a connection to pool, call:
  - `conn.reset_for_pool_reuse()`
- `reset_for_pool_reuse()` normalizes session state for next borrower:
  - rolls back open transaction if needed
  - restores `autocommit=1` if needed
  - verifies reusable state.

## Transactions

- For explicit transaction flow:
  - `conn.set_autocommit(false)`
  - one or more `conn.call(...)`
  - `conn.commit()` or `conn.rollback()`
- Keep transactions short when calls can produce large resultsets; stream/skip aggressively.

## Error model

Two distinct error channels:

- **`RpcError`** (returned as `Result::Err`) — transport or protocol failure. The connection may be dead after this.
- **`RpcEvent::ServerErr`** (surfaced through the event stream) — server-level SQL error. The connection remains usable; only the statement is terminal.

Handle both layers explicitly.

### Config-time errors (`RpcConfigError`)

| Tag | Field | When |
|---|---|---|
| `rpc-config-missing-required` | `user` or `password` | Required field not set via builder |
| `rpc-config-invalid-port` | `port` | Port not in 1..65535 |
| `rpc-config-invalid-timeout` | `connect_timeout_ms`, `read_timeout_ms`, `write_timeout_ms` | Timeout <= 0 |

### Runtime errors (`RpcError`)

Connection lifecycle:

| Tag | Source | When |
|---|---|---|
| `rpc-wire-connect-failed` | `connect()` | Wire layer TCP connect or handshake failed |
| `rpc-wire-query-failed` | `connect()`, `conn.call()` | Wire layer query execution failed |
| `rpc-server-error` | `connect()` (SET NAMES) | Server returned ERR during post-connect setup |
| `rpc-wire-set-autocommit-failed` | `connect()`, `conn.set_autocommit()` | Wire layer set_autocommit failed |
| `rpc-wire-commit-failed` | `conn.commit()` | Wire layer commit failed |
| `rpc-wire-rollback-failed` | `conn.rollback()` | Wire layer rollback failed |
| `rpc-wire-reset-failed` | `conn.reset_for_pool_reuse()` | Wire layer reset failed |
| `rpc-wire-close-failed` | `conn.close()` | Wire layer close failed |

Statement operations:

| Tag | Source | When |
|---|---|---|
| `rpc-invalid-proc-name` | `conn.call()` | Proc name empty or has non-identifier characters |
| `rpc-wire-next-event-failed` | `stmt.next_event()` | Wire layer next_event failed |
| `rpc-wire-skip-result-failed` | `stmt.skip_result()` | Wire layer skip_result failed |
| `rpc-wire-skip-remaining-failed` | `stmt.skip_remaining()` | Wire layer skip_remaining failed |

Row getters:

| Tag | Source | When |
|---|---|---|
| `rpc-row-index-out-of-bounds` | `row.is_null/get_*()` | Index < 0 or >= column count |
| `rpc-row-null` | `row.get_string()` | Cell is NULL (use `is_null` first) |
| `rpc-row-parse-int-failed` | `row.get_int()` | String-to-Int parse failed |
| `rpc-row-parse-uint-failed` | `row.get_uint()` | String-to-Uint parse failed |
| `rpc-row-parse-float-failed` | `row.get_float()` | String-to-Float parse failed |

### Error tag naming convention

All tags follow `rpc-{category}-{detail}`:
- `rpc-config-*` — configuration validation
- `rpc-wire-*` — wire layer passthrough (transport/protocol)
- `rpc-server-error` — server ERR during internal operations
- `rpc-invalid-*` — input validation
- `rpc-row-*` — row accessor errors

## Recommended usage pattern

1. Borrow connection.
2. Call procedure.
3. Stream events, processing rows you need.
4. Skip unneeded remainder (`skip_result`/`skip_remaining`).
5. Commit or rollback.
6. `reset_for_pool_reuse()`.
7. Return connection.

## Operational guidance

- Timeout strategy defaults.
- Retry guidance (what is safe to retry and when).
- Pool sizing and backpressure recommendations.
- Observability/metrics suggestions.

## Anti-patterns to avoid

- Implicitly relying on connection close for cleanup in pooled environments.
- Issuing large result-producing calls in latency-sensitive transactions.
- Assuming statement errors close the session.

## TODO: examples to add

- `call` with args and streamed row handling.
- Multi-resultset selective consume (`skip_result` then read next resultset).
- Partial consume then `reset_for_pool_reuse` path.
- Streaming-to-file example at RPC layer.
