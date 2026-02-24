# RPC API Finalize — Concrete Plan

Finalize mariadb-rpc public API contract + error-tag contract.

**Why this next:**

- Proto-cleanup is effectively closed (with #20 deferred).
- RPC implementation is already in motion, so freezing the contract now prevents churn.

## Decisions (resolved)

1. **`RpcResultSetSummary`** — Option A: remove `done` field, make `ResultSetEnd` a unit variant. `done=true` carries no signal. `has_more` is useful but belongs in a separate wire/API change, not this contract freeze.

2. **`RpcArgError`** — Option A: remove from export and both definition sites. Currently dead surface (exported but never returned by any function).

3. **`write_timeout_ms`** — Option B: keep, but document explicitly. It's part of the pinned MVP config shape; removing it is API churn during a freeze step. Add explicit note: wire currently uses one `io_timeout_ms`; RPC maps `read_timeout_ms` to wire I/O timeout; `write_timeout_ms` is reserved for future wire timeout split.

4. **Proc name validation test** — Option B: add live scenario to `live_rpc_smoke_test.drift`. Minimal and high-signal; validates SQL-injection guard through real API path.

## 0. File structure: resolve types.drift duplication

### Problem

`packages/mariadb-rpc/src/types.drift` defines the exact same types as `lib.drift` (structs, variants, Diagnostic impls). **Nothing imports `types.drift`** — all consumers import `mariadb.rpc` (which resolves to `lib.drift`). The compiler includes `types.drift` in the build via `--src-root` scanning, but no module references it.

This is a maintenance hazard: both files must be kept in sync manually, and divergence is silent.

### Resolution

**Delete `types.drift`.** `lib.drift` is the authoritative source for all type definitions. This eliminates the divergence risk. If separation is needed later (e.g., when the file grows large enough to warrant splitting), add it back with a proper `import mariadb.rpc.types as types` in `lib.drift`.

**Note:** Verify via compile check that removing `types.drift` doesn't break anything. Since nothing imports it, it should be clean.

## 1. API signature audit — current state and changes

### 1a. Signatures that are final (no changes)

Configuration builder pattern:
- `new_connection_config_builder() -> RpcConnectionConfigBuilder`
- `build_connection_config(builder) -> Result<RpcConnectionConfig, RpcConfigError>`
- All 12 `with_*` builder methods (including `with_write_timeout_ms` — kept per decision #3)

Connection lifecycle:
- `connect(config) -> Result<RpcConnection, RpcError>`
- `conn.close() / close(conn) -> Result<Void, RpcError>`

Statement operations:
- `conn.call(proc_name) -> Result<RpcStatement, RpcError>`
- `conn.call(proc_name, args) -> Result<RpcStatement, RpcError>`
- `stmt.next_event() / next_event(stmt) -> Result<RpcEvent, RpcError>`
- `stmt.skip_result() / skip_result(stmt) -> Result<Void, RpcError>`
- `stmt.skip_remaining() / skip_remaining(stmt) -> Result<Void, RpcError>`

Transaction control:
- `conn.set_autocommit(enabled) / set_autocommit(conn, enabled) -> Result<Void, RpcError>`
- `conn.commit() / commit(conn) -> Result<Void, RpcError>`
- `conn.rollback() / rollback(conn) -> Result<Void, RpcError>`

Pool reuse:
- `conn.reset_for_pool_reuse() / reset_for_pool_reuse(conn) -> Result<Void, RpcError>`

Argument helpers:
- `new_args() -> Array<RpcArg>`
- `arg_null/arg_bool/arg_int/arg_float/arg_string/arg_bytes`

Row getters:
- `row.is_null(idx) -> Result<Bool, RpcError>`
- `row.get_string(idx) -> Result<String, RpcError>`
- `row.get_int(idx) -> Result<Int, RpcError>`
- `row.get_uint(idx) -> Result<Uint, RpcError>`
- `row.get_float(idx) -> Result<Float, RpcError>`

Standalone function wrappers (dual style — method + free function):
- `next_event`, `skip_result`, `skip_remaining`, `set_autocommit`, `commit`, `rollback`, `reset_for_pool_reuse`, `close`

### 1b. Event model change: `ResultSetEnd` becomes unit variant

Remove `RpcResultSetSummary` type entirely. Change `RpcEvent::ResultSetEnd(value: RpcResultSetSummary)` to `RpcEvent::ResultSetEnd`.

Callers currently match `RpcEvent::ResultSetEnd(_)` — they'll need to match `RpcEvent::ResultSetEnd` (no payload). Existing tests in this repo are the only callers.

### 1c. `RpcArgError` removal

Remove from:
- Export list in `lib.drift`
- Type definition in `lib.drift`
- Diagnostic impl in `lib.drift`
- (Already deleted `types.drift` in phase 0)

### 1d. `write_timeout_ms` documentation

Add comment on `RpcConnectionConfig.write_timeout_ms`:
```
// Reserved. Wire layer currently uses one io_timeout_ms for both reads and
// writes; RPC maps read_timeout_ms to wire io_timeout_ms. write_timeout_ms
// is validated but not consumed until wire supports separate write timeout.
```

## 2. Error tag contract — canonical catalog

### 2a. Config-time errors (returned as `RpcConfigError`)

| Tag | Field | When |
|---|---|---|
| `rpc-config-missing-required` | `user` or `password` | Required field not set via builder |
| `rpc-config-invalid-port` | `port` | Port not in 1..65535 |
| `rpc-config-invalid-timeout` | `connect_timeout_ms`, `read_timeout_ms`, `write_timeout_ms` | Timeout <= 0 |

### 2b. Runtime errors (returned as `RpcError`)

**Connection lifecycle:**

| Tag | Source | When |
|---|---|---|
| `rpc-wire-connect-failed` | `connect()` | Wire layer TCP connect or handshake failed |
| `rpc-wire-query-failed` | `connect()`, `conn.call()` | Wire layer query execution failed (transport/protocol error) |
| `rpc-server-error` | `connect()` (SET NAMES) | Server returned ERR during post-connect setup |
| `rpc-wire-set-autocommit-failed` | `connect()`, `conn.set_autocommit()` | Wire layer set_autocommit failed |
| `rpc-wire-commit-failed` | `conn.commit()` | Wire layer commit failed |
| `rpc-wire-rollback-failed` | `conn.rollback()` | Wire layer rollback failed |
| `rpc-wire-reset-failed` | `conn.reset_for_pool_reuse()` | Wire layer reset failed |
| `rpc-wire-close-failed` | `conn.close()` | Wire layer close failed |

**Statement operations:**

| Tag | Source | When |
|---|---|---|
| `rpc-invalid-proc-name` | `conn.call()` | Proc name empty or has non-identifier characters |
| `rpc-wire-next-event-failed` | `stmt.next_event()` | Wire layer next_event failed (transport/protocol error) |
| `rpc-wire-skip-result-failed` | `stmt.skip_result()` | Wire layer skip_result failed |
| `rpc-wire-skip-remaining-failed` | `stmt.skip_remaining()` | Wire layer skip_remaining failed |

**Row getters:**

| Tag | Source | When |
|---|---|---|
| `rpc-row-index-out-of-bounds` | `row.is_null/get_*()` | Index < 0 or >= column count |
| `rpc-row-null` | `row.get_string()` | Cell is NULL (use `is_null` first) |
| `rpc-row-parse-int-failed` | `row.get_int()` | String-to-Int parse failed |
| `rpc-row-parse-uint-failed` | `row.get_uint()` | String-to-Uint parse failed |
| `rpc-row-parse-float-failed` | `row.get_float()` | String-to-Float parse failed |

### 2c. Server errors surfaced as events (not RpcError)

Server SQL errors are **not** returned as `Result::Err`. They are surfaced as `RpcEvent::ServerErr(RpcServerError)` through the event stream. This is intentional — a server error doesn't mean the connection is broken; the statement is terminal but the connection remains usable.

Key error model distinction: **`RpcError` = transport/protocol failure (connection may be dead). `RpcEvent::ServerErr` = server-level SQL error (connection alive, statement finished).**

### 2d. Error tag naming convention

All tags follow the pattern: `rpc-{category}-{detail}`.
- `rpc-config-*` — configuration validation errors
- `rpc-wire-*` — wire layer passthrough errors (transport/protocol)
- `rpc-server-error` — server ERR during internal operations (connect setup)
- `rpc-invalid-*` — input validation errors
- `rpc-row-*` — row accessor errors

## 3. Contract tests

### 3a. Existing coverage (no changes needed)

- Row getters: `row_getters_test.drift` — all error tags asserted, bounds/null/parse paths covered
- Happy-path RPC flow: `live_rpc_smoke_test.drift` — call/args/multi-resultset/skip/reset
- Connect state: multiple connect_state_handoff tests
- Event progression: implicitly covered by existing smoke test scenarios

### 3b. New test: `rpc_config_validation_test.drift`

Unit test (no network). File: `packages/mariadb-rpc/tests/unit/rpc_config_validation_test.drift`.

Scenarios:

1. **Missing user** — builder without `with_user` → expect `Err`, tag `rpc-config-missing-required`, field `user`.
2. **Missing password** — builder without `with_password` → expect `Err`, tag `rpc-config-missing-required`, field `password`.
3. **Invalid port (0)** → expect `Err`, tag `rpc-config-invalid-port`.
4. **Invalid port (70000)** → expect `Err`, tag `rpc-config-invalid-port`.
5. **Invalid connect timeout (0)** → expect `Err`, tag `rpc-config-invalid-timeout`, field `connect_timeout_ms`.
6. **Invalid read timeout (negative)** → expect `Err`, tag `rpc-config-invalid-timeout`, field `read_timeout_ms`.
7. **Invalid write timeout (0)** → expect `Err`, tag `rpc-config-invalid-timeout`, field `write_timeout_ms`.
8. **Valid config** → expect `Ok`, verify key fields match builder inputs.

Add justfile recipe `rpc-check-config` and include in `just test` flow (after `rpc-check`).

### 3c. New scenario: `scenario_invalid_proc_name` in `live_rpc_smoke_test.drift`

Add to existing live smoke test. Call with invalid proc name (e.g., `"bad;name"`), expect `Err` with tag `rpc-invalid-proc-name`. Single scenario, minimal addition.

## 4. Execution plan (in order)

### Phase 0: File cleanup
1. Delete `packages/mariadb-rpc/src/types.drift`.
2. Compile check to confirm no breakage.

### Phase 1: API cleanup (code changes in `lib.drift`)
3. Remove `RpcArgError` — type definition, Diagnostic impl, export entry.
4. Remove `RpcResultSetSummary` — type definition, Copy impl, export entry. Change `RpcEvent::ResultSetEnd(value: RpcResultSetSummary)` to `RpcEvent::ResultSetEnd`.
5. Update `next_event` in RpcStatement to emit `RpcEvent::ResultSetEnd()` instead of `RpcEvent::ResultSetEnd(RpcResultSetSummary(done = true))`.
6. Update all test files that match `RpcEvent::ResultSetEnd(_)` to match `RpcEvent::ResultSetEnd`.
7. Add `write_timeout_ms` documentation comment on `RpcConnectionConfig`.
8. Compile check.

### Phase 2: Config validation test
9. Create `packages/mariadb-rpc/tests/unit/rpc_config_validation_test.drift` with 8 scenarios.
10. Add justfile recipe `rpc-check-config`.
11. Run `rpc-check-config`.

### Phase 3: Proc name validation test
12. Add `scenario_invalid_proc_name` to `live_rpc_smoke_test.drift`.
13. Run `rpc-live`.

### Phase 4: Documentation
14. Update `docs/effective-mariadb-rpc.md` error model section with canonical tag tables from section 2.
