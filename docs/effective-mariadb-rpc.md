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

`RpcConnection` itself does not emit keepalives. MariaDB's `wait_timeout` will reap idle conns; the next call surfaces as `rpc-wire-*`. For proactive keepalive on long-lived conns, either call `conn.ping()` on a timer yourself or use **`mariadb.rpc.managed`** (next section), which handles the lifecycle for you.

## Managed connection (`mariadb.rpc.managed`)

For long-lived single connections that need autonomous keepalive and DNS-remap recovery, the `mariadb.rpc.managed` module wraps `RpcConnection` and hands out leases.

```drift
import mariadb.rpc.managed as managed;

var mc_cfg = managed.default_managed_config();
mc_cfg.keepalive_interval_ms = 30000;  // ping every 30s; 0 disables

match managed.open(rpc_config, move mc_cfg) {
    core.Result::Err(e) => { /* handle open failure */ },
    core.Result::Ok(v) => {
        var mc = move v;
        // acquire bounds the WHOLE wait by the deadline. A busy slot (lease in
        // flight / keepalive ticking) parks up to the timeout, then returns the
        // `acquire-timeout` tag. The deadline is mandatory and finite.
        match mc.acquire(conc.Duration(millis = 5000)) {
            core.Result::Ok(lv) => {
                var lease = move lv;
                match lease.conn() {
                    core.Result::Ok(c) => {
                        // c: &mut RpcConnection — exclusive use during the lease.
                        // No lock held; the conn was MOVED out of the wrapper's slot.
                        rpc.call(c, &"sp_my_proc", &args)
                    },
                    core.Result::Err(_) => { /* internal invariant — unreachable in practice */ }
                }
                // lease drops here → Destructible::destroy fires and returns
                // the conn to the wrapper's slot. There is no release() method.
            },
            core.Result::Err(_) => { /* acquire-timeout (waited the deadline), managed-closed, or transport */ }
        }
        val _ = mc.close();
    }
}
```

### Model

- **Lease-based borrow.** `mc.acquire(timeout)` returns a `LeasedConn` that owns the underlying `RpcConnection` for the duration of the lease. Multiple sequential calls can run on the same lease — the single-active-statement rule still applies per call.
- **Mandatory finite deadline.** `acquire` takes a `conc.Duration` that bounds the whole call. If the slot is busy (lease in flight / keepalive / reconnect gap) it parks on a Condvar up to the deadline, waking on release/reconnect; on the deadline it returns the shared `acquire-timeout` tag. There is no "0 = forever" — `timeout <= 0` returns `acquire-timeout` immediately even if the slot is free. `acquire` takes `&Self`, so callers may share via `Arc<...>` and acquire concurrently.
- **RAII release only.** `LeasedConn` has no `release()` method. The only way to return the conn is to let the value go out of scope; `Destructible::destroy` puts it back in the wrapper's slot. You can't forget to release.
- **Mutex protects the storage slot, not the lease.** The internal `Mutex<ManagedSlot>` (the conn cell + a closed flag, paired with a Condvar) is held only at acquire/release transition points — milliseconds, not the lease duration — and never across socket I/O (teardown happens outside the lock). A streaming statement that runs for minutes does not monopolize any lock.
- **Autonomous keepalive thread.** When `keepalive_interval_ms > 0`, `managed.open()` spawns a virtual thread that periodically tries to take the conn from the slot, `ping`s it, and puts it back. On ping failure it drops the dead conn and calls `rpc.connect(config)` again — which **re-resolves DNS**, so a DB host migrated to a new IP during maintenance is recovered without app involvement. Keepalive ticks fire `ManagedEvent::KeepalivePingOk` / `KeepalivePingFailed` events through the optional `event_sink`.
- **Reconnect-on-failure with jittered exponential backoff.** Configured via `ManagedConfig`. Continues retrying until success or `close()`.

### `ConnectionSource` interface

`ManagedConnection` implements a fixed-signature `pub interface ConnectionSource`:

```drift
pub interface ConnectionSource {
    fn acquire(self: &Self, timeout: conc.Duration) nothrow -> core.Result<LeasedConn, ManagedError>;
    fn close(self: &mut Self) nothrow -> core.Result<Void, ManagedError>;
}
```

`acquire` takes `&Self` (not `&mut`): it only reads internal state and is internally synchronized, so callers can share a source via `Arc<ConnectionSource impl>` and acquire **concurrently** without an outer mutex serializing them. `close` takes `&mut Self`. The `timeout` is mandatory and bounds the whole acquire end-to-end; on the deadline both implementations return the shared `acquire-timeout` tag, so a caller branching on timeout need not know whether it holds a `ManagedConnection` or a `ConnectionPool`.

Callers can depend on `ConnectionSource` rather than the concrete `ManagedConnection` type. `ConnectionPool` (`mariadb.rpc.pool`) implements the same interface — switching from one connection to N is a constructor-line change at the call site, not a refactor.

### What the wrapper does NOT do

- **Does not interpret call errors.** If a `rpc.call(...)` inside a lease returns `RpcError`, it goes straight to the caller. The wrapper does not auto-retry, does not classify "retriable vs not", does not silence anything. Business-level retry policy is the caller's problem (or their orchestration layer's).
- **Does not validate result semantics.** The wrapper holds a conn live; what the caller does with it is between them and MariaDB.
- **Does not provide a connection pool.** v1 is single-conn. A pool is planned as a separate `ConnectionSource` implementation; until then, run multiple `ManagedConnection`s if you need parallelism.

### `ManagedConfig` defaults

```drift
pub struct ManagedConfig {
    pub keepalive_interval_ms: Int,        // default 30000 (30s; 0 disables)
    pub reconnect_backoff_initial_ms: Int, // default 1000
    pub reconnect_backoff_max_ms: Int,     // default 16000
    pub reconnect_backoff_jitter_pct: Int, // default 25
    pub event_sink: Optional<core.Callback1<ManagedEvent, Void> >  // default None
}
```

Pick `keepalive_interval_ms` based on the server's `wait_timeout` — half of it is a safe floor. Set to `0` for short-lived connections where the wrapper is only buying you the RAII lease shape.

### Events

```drift
pub variant ManagedEvent {
    KeepalivePingOk,
    KeepalivePingFailed(ping_failed_tag: String),
    ReconnectAttempt(attempt_attempt: Int),
    ReconnectSucceeded(succeeded_attempt: Int),
    ReconnectFailed(failed_attempt: Int, failed_tag: String)
}
```

Install a `core.Callback1<ManagedEvent, Void>` via `ManagedConfig.event_sink` to observe these. The callback fires on the keepalive virtual thread; if it shares state with the main thread, protect it via `conc.Arc<conc.Mutex<...>>`.

### ManagedError tags

| Tag | When |
|---|---|
| `managed-open-connect-failed` | Initial `rpc.connect` inside `open()` failed |
| `acquire-timeout` | The slot stayed busy until the `acquire(timeout)` deadline elapsed (interface-level tag, shared with `ConnectionPool`) |
| `managed-closed` | `acquire()` after `close()`, or `close()` woke a parked acquirer |
| `managed-wait-failed` | Condvar wait returned a non-CLOSED/TIMEOUT error (rare; runtime issue) |
| `managed-lease-empty` | Internal invariant violation in `LeasedConn::conn()` — should not happen in normal use |

## Connection pool (`mariadb.rpc.pool`)

For app servers expecting concurrent acquirers, the `mariadb.rpc.pool` module hands out leases from an elastic pool of `RpcConnection`s. It implements the same `ConnectionSource` interface as `ManagedConnection`, so call sites are drop-in compatible — switch the constructor and the rest of your code keeps working.

```drift
import mariadb.rpc.pool as pool;

var pc = pool.default_pool_config();
pc.min_idle = 2;       // pre-seed 2 conns at open()
pc.max_conns = 20;     // open up to 20 on demand
pc.idle_timeout_ms = 60000;   // reap idle conns after 60s
pc.keepalive_interval_ms = 30000;

val p = pool.open(rpc_config, move pc).or_throw();
```

### Model

- **Elastic sizing.** Starts with `min_idle` pre-opened conns. `acquire()` opens new conns on demand up to `max_conns`. Idle conns are reaped after `idle_timeout_ms` of no user lease (keepalive activity does NOT count as use).
- **Deadline-bounded wait on exhaustion.** When the pool is at `max_conns` and all conns are leased, `acquire(timeout)` parks the calling VT on an internal Condvar for up to the remaining budget. The next `LeasedConn::destroy` (release) signals one waiter; on the deadline the waiter returns `acquire-timeout`. The budget bounds the whole call — including an on-demand `rpc.connect` (TCP + handshake + setup), not just the park. `pool.close()` wakes all waiters with `pool-closed`, which wins over timeout/open-failed once close begins.
- **Concurrent acquire.** `acquire` takes `&Self`, so share the pool via `Arc<ConnectionPool>` and acquire from many VTs concurrently — no outer mutex, so the internal Condvar/waiter model is actually exercised.
- **Lease shape identical to `ManagedConnection`.** `acquire(timeout)` returns the same `LeasedConn` type. RAII release via destructor; no `release()` method.
- **Sequential calls per lease.** Single-active-statement rule applies per lease. To run two statements at once, hold two leases.
- **One keepalive thread.** Periodically pings idle conns and reaps those beyond `idle_timeout_ms`. Same DNS-re-resolve-on-reconnect behavior as `ManagedConnection`.
- **Lock discipline.** Never holds the internal slot mutex across network I/O. `rpc.connect`, `rpc.ping`, and `rpc.close` all run with the lock released. Idle reap, push-back, and counter updates are the only critical sections.

### `PoolConfig` defaults

```drift
PoolConfig(
    min_idle = 0,
    max_conns = 10,
    idle_timeout_ms = 60000,
    keepalive_interval_ms = 30000,
    reconnect_backoff_initial_ms = 1000,
    reconnect_backoff_max_ms = 16000,
    reconnect_backoff_jitter_pct = 25,
    event_sink = None
)
```

Pick `max_conns` matched to your MariaDB `max_connections` (with headroom for other clients). `min_idle = 0` is correct for bursty workloads; raise it to amortize the connect cost if your QPS rarely drops to zero.

### `PoolEvent` variants

```drift
pub variant PoolEvent {
    ConnOpened(opened_total: Int),
    ConnClosedByReap(closed_total: Int),
    KeepalivePingOk,
    KeepalivePingFailed(ping_failed_tag: String),
    ReconnectAttempt(attempt_attempt: Int),
    ReconnectSucceeded(succeeded_attempt: Int),
    ReconnectFailed(failed_attempt: Int, failed_tag: String),
    AcquireWaiting(waiting_now: Int),
    AcquireUnblocked
}
```

Install via `PoolConfig.event_sink`. The callback fires on the keepalive virtual thread, on the releaser's VT, or on the acquirer's VT — protect any shared state with `conc.Arc<conc.Mutex<...>>`.

### `ManagedError` tags from the pool

| Tag | When |
|---|---|
| `pool-config-invalid-max-conns` | `max_conns < 1` |
| `pool-config-invalid-min-idle` | `min_idle < 0 or min_idle > max_conns` |
| `pool-open-seed-failed` | Failed to seed `min_idle` conns at `open()` |
| `acquire-timeout` | The `acquire(timeout)` deadline elapsed — while parked on exhaustion, or during an on-demand connect (interface-level tag, shared with `ManagedConnection`) |
| `pool-open-failed` | On-demand `rpc.connect` failed for a transport reason inside `acquire()` (within the deadline) |
| `pool-closed` | `acquire()` after `close()`, or `close()` racing a parked waiter / in-flight open — closed wins once close begins |
| `pool-busy-keepalive` | Internal: keepalive tried to take a conn but pool was busy. Caller never sees this. |
| `pool-wait-failed` | Condvar wait returned a non-CLOSED/TIMEOUT error (rare; runtime issue) |

### When to use `ManagedConnection` vs `ConnectionPool`

| Use case | Use |
|---|---|
| Single long-lived process with serial calls (CLI tool, bookkeeper-style daemon) | `ManagedConnection` |
| App server expecting concurrent requests | `ConnectionPool` |
| Migrating from one to the other later | Depend on `ConnectionSource` interface; the constructor is the only call-site change |

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
