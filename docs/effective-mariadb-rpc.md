# Effective MariaDB RPC Usage

Audience: application developers using `mariadb-rpc`.

Status: living guide. Update as API stabilizes.

## Goals

- Provide practical guidance for calling stored procedures safely and efficiently.
- Keep wire-level mechanics out of app code.

## Architecture overview

```
mariadb-rpc/src/
  lib.drift — Currently implemented in one file. All types, config builder,
              connection, statement, row getters, arg encoding, and
              free-function wrappers.
```

The RPC layer is a thin facade over `mariadb-wire-proto`. It adds:
- Builder-pattern configuration with validation.
- Stored procedure CALL generation with argument encoding and proc name validation.
- Typed row getters (get_int, get_string, etc.) on top of raw ResultSetCell.
- Error tag mapping from wire errors to `rpc-*` namespace.

## Core principles

- Prefer RPC API over wire API for normal application development.
- Treat stored procedure calls as streamed responses, not pre-buffered blobs.
- Keep transaction scope small and explicit.
- Return connections to pool only after reset/sanitization.
- Use `call(...)` as the statement entrypoint; consume via statement events.

## Configuration

### Builder pattern

```
var b = rpc.new_connection_config_builder();
b.with_host("10.0.0.1");
b.with_port(3306);
b.with_user("appuser");
b.with_password("secret");
b.with_database("mydb");
b.with_connect_timeout_ms(3000);
b.with_read_timeout_ms(3000);
b.with_write_timeout_ms(3000);
b.with_autocommit(false);
b.with_strict_reuse(true);
match rpc.build_connection_config(move b) {
    core.Result::Ok(cfg) => { ... },
    core.Result::Err(e) => { ... }
}
```

### Defaults

| Field | Default |
|---|---|
| host | `127.0.0.1` |
| port | `3306` |
| connect/read/write timeout | `3000ms` |
| charset | `utf8mb4` |
| collation | `utf8mb4_unicode_ci` |
| autocommit | `false` |
| strict_reuse | `true` |

### Timeout mapping

`read_timeout_ms` maps to the wire layer's `io_timeout_ms` for all socket I/O. `write_timeout_ms` is validated but not consumed — reserved for a future wire-layer timeout split.

## Statement model (streaming-first)

- `conn.call(proc_name)` or `conn.call(proc_name, &args)` starts one statement and returns `RpcStatement`.
- Consume results incrementally with:
  - `stmt.next_event()` — returns the next `RpcEvent`.
  - `stmt.skip_result()` — drains current resultset, stops at boundary.
  - `stmt.skip_remaining()` — drains everything to terminal.
- There is no `query_all`/buffer-all API in MVP.

### Event sequence

```
[Row, Row, ...] → ResultSetEnd → [Row, Row, ...] → ResultSetEnd → StatementEnd
                                                                    or ServerErr
```

For single-resultset procedures, the pattern is simpler:
```
[Row, Row, ...] → ResultSetEnd → StatementEnd
```

For procedures with no resultset (INSERT/UPDATE only):
```
StatementEnd
```

## Single active statement rule

- One connection supports one active statement at a time.
- Practical implication: finish a statement (consume/skip) before starting the next call.
- This is pool-friendly and avoids overlapping response streams on one socket.

## Event types

| Event | Meaning |
|---|---|
| `RpcEvent::Row(row)` | One row of data. Access columns via `row.get_int(idx)`, etc. |
| `RpcEvent::ResultSetEnd` | Current resultset exhausted. More may follow for multi-result procs. |
| `RpcEvent::StatementEnd(summary)` | Terminal. Statement complete. `summary` has `affected_rows`, `last_insert_id`, `status_flags`, `warnings`. |
| `RpcEvent::ServerErr(err)` | Terminal. Server SQL error. `err` has `error_code`, `sql_state`, `message`. Connection is still alive. |

## Row getters

| Method | Returns | Error |
|---|---|---|
| `row.is_null(idx)` | `Bool` | `rpc-row-index-out-of-bounds` |
| `row.get_string(idx)` | `String` | `rpc-row-index-out-of-bounds`, `rpc-row-null` |
| `row.get_int(idx)` | `Int` | above + `rpc-row-parse-int-failed` |
| `row.get_uint(idx)` | `Uint` | above + `rpc-row-parse-uint-failed` |
| `row.get_float(idx)` | `Float` | above + `rpc-row-parse-float-failed` |

All getters work on the text protocol — server sends values as strings, getters parse them.

## Argument encoding

Arguments are encoded into the CALL SQL string with type-appropriate escaping:

| `RpcArg` variant | SQL encoding |
|---|---|
| `Null` | `NULL` |
| `Bool(true/false)` | `1` / `0` |
| `Int(v)` | decimal string |
| `Float(v)` | decimal string |
| `String(v)` | single-quoted with `'` doubled (`'it''s'`) |
| `Bytes(v)` | hex literal (`0xABCD...`) |

### Fixed-width binary arguments — `arg_binary_fixed`

For `BINARY(N)` or `VARBINARY(N)` parameters where a length mismatch would corrupt application semantics, prefer the fail-loud helper over raw `arg_bytes`:

```drift
match rpc.arg_binary_fixed(uuid_bytes, 16) {
    core.Result::Ok(a) => { args.push(move a); },
    core.Result::Err(e) => { /* surface as FAILED_INVALID_PAYLOAD */ }
}
```

Why this exists: `BINARY(N)` is fixed-width on the server. If your `Array<Byte>` has fewer than `N` bytes, MariaDB silently right-pads with `\0` and stores it under the padded value. For idempotency keys, lease owners, or any column you read back to compare, this produces a "valid-looking but wrong" key — two requests with subtly different input lengths get different stored keys, and the next request can't find the prior write. `arg_binary_fixed` rejects with `rpc-binary-length-mismatch` (message format `expected=N,got=M`) before the bytes ever reach the wire.

Use `arg_bytes` directly only for variable-length binary parameters (`BLOB`, `VARBINARY` with no fixed app-side length contract).

## Proc name validation

Proc names are validated to contain only `[A-Za-z0-9_]` characters. This prevents SQL injection through proc name manipulation. Names with spaces, semicolons, quotes, or other special characters are rejected with `rpc-invalid-proc-name`.

## Drain semantics and pool safety

- Preferred explicit drain when you do not need all results:
  - `stmt.skip_result()` to jump to the next resultset.
  - `stmt.skip_remaining()` to finish the statement.
- If a statement is dropped before terminal event, wire-layer destruction drains remaining packets.
- Before returning a connection to pool, call:
  - `conn.reset_for_pool_reuse()`
- `reset_for_pool_reuse()` normalizes session state for next borrower:
  - tries COM_RESET_CONNECTION (single round-trip)
  - falls back to ROLLBACK + SET autocommit=1 + PING
  - verifies reusable state.

## Transactions

- For explicit transaction flow:
  - `conn.set_autocommit(false)`
  - one or more `conn.call(...)`
  - `conn.commit()` or `conn.rollback()`
- Keep transactions short when calls can produce large resultsets; stream/skip aggressively.

## Liveness and transport-error classification

Long-lived connections need three primitives to write a clean reconnect-on-disconnect path:

| Helper | Cost | Use |
|---|---|---|
| `conn.ping() -> Result<Void, RpcError>` | one round-trip (COM_PING / OK) | Active probe — recommended keepalive on long-lived idle conns. Failure marks the conn dead. Returns `rpc-wire-ping-failed` on error. |
| `conn.is_alive() -> Bool` | flag check, no I/O | Returns `true` iff session is not marked dead, not closed, and has no active statement. Cheap; use to short-circuit before issuing a call. **Caveat:** does not detect silent TCP half-close (NAT timeout, cable yank) until the next syscall — for true network liveness use `ping()`. |
| `rpc.is_transport_error(&RpcError) -> Bool` | pure | Returns `true` for any `rpc-wire-*` tag — the class for which the connection may be dead and a reconnect is the right response. Server SQL errors arrive via `RpcEvent::ServerErr`, not `RpcError`, so they never pass through here. Use as the gate in a retry-once-on-transport-error wrapper. |

### Reconnect recipe (single-connection, long-lived)

```drift
match rpc.call(&mut conn, &proc_name, &args) {
    core.Result::Ok(stmt) => { /* stream events */ },
    core.Result::Err(e) => {
        if rpc.is_transport_error(&e) {
            // Drop the dead conn and rebuild from the same config.
            val _ = rpc.close(&mut conn);
            match rpc.connect(config) {
                core.Result::Ok(new_conn) => { conn = move new_conn; /* retry once */ },
                core.Result::Err(_) => { /* connect-failed: jittered exponential backoff */ }
            }
        } else {
            /* config/proc-name/etc. — not retriable */
        }
    }
}
```

Differentiate `rpc-wire-connect-failed` from mid-flight `rpc-wire-*` tags for backoff: the former usually indicates "we never had a conn this attempt" (DNS / firewall / server-down — longer jittered backoff); the latter means "we had a conn and lost it" (fast retry-once is reasonable).

### Idle timeout

The library does not emit keepalives. MariaDB's `wait_timeout` will reap idle conns; the next call surfaces as `rpc-wire-*`. For proactive keepalive on long-lived conns, call `conn.ping()` on a timer; safe cadence is `wait_timeout / 2`.

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
| `rpc-wire-ping-failed` | `conn.ping()` | Wire layer ping failed (conn marked dead) |

Statement operations:

| Tag | Source | When |
|---|---|---|
| `rpc-invalid-proc-name` | `conn.call()` | Proc name empty or has non-identifier characters |
| `rpc-binary-length-mismatch` | `arg_binary_fixed()` | `v.len != expected_len` (message: `expected=N,got=M`) |
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

## Connect sequence detail

`rpc.connect()` performs three steps:

1. **Wire connect** — TCP connect + MariaDB handshake + auth → `WireSession`.
2. **SET NAMES** — `SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci` (or configured charset/collation).
3. **Autocommit** — `SET autocommit=0/1` per config.

If any step fails, the connection is closed before error is returned.

## Recommended usage pattern

1. Build config and connect.
2. Call procedure.
3. Stream events, processing rows you need.
4. Skip unneeded remainder (`skip_result`/`skip_remaining`).
5. Commit or rollback.
6. `reset_for_pool_reuse()`.
7. Return connection to pool (or close).

## Anti-patterns to avoid

- Implicitly relying on connection close for cleanup in pooled environments.
- Issuing large result-producing calls in latency-sensitive transactions.
- Assuming statement errors (ServerErr) close the session — they don't.
- Skipping `reset_for_pool_reuse()` before returning to pool — session state may leak.
