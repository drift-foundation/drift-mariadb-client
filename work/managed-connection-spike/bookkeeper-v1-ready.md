# `mariadb.rpc.managed` + `mariadb.rpc.pool` — ready to integrate

## TL;DR

Two `ConnectionSource` implementations in `mariadb-rpc` 0.5.0:

- **`mariadb.rpc.managed.ManagedConnection`** — single long-lived conn. Use for bookkeeper-style daemons that do one thing serially.
- **`mariadb.rpc.pool.ConnectionPool`** — elastic pool, block-waits on exhaustion via Condvar. Use for app servers with concurrent requests.

Both implement `pub interface ConnectionSource { acquire / close }` with identical `LeasedConn` semantics, so call sites are interchangeable. Switching from one to the other is a one-line constructor change.

## What you get

- **`acquire()`** — exclusive lease on an `RpcConnection`.
- **`LeasedConn`** — RAII handle, returns the conn on drop. No public `release()` — can't leak.
- **`lease.conn() -> &mut RpcConnection`** — pass to `rpc.call(...)` etc.
- **Autonomous keepalive** — pings + reconnects on failure, re-resolves DNS so DB host migration during maintenance recovers without app code.
- **Block-wait under contention** (pool only) — waiters park on Condvar until a peer releases.
- **No business-level retry** — `RpcError` from a call surfaces to your code untouched. Microflows owns retry policy.
- **Event sink** — `ManagedEvent` / `PoolEvent` for observability (ping outcomes, reconnects, pool sizing). Install via config; defaults to no-op.

## Integration shape — app server (pool)

```drift
import mariadb.rpc as rpc;
import mariadb.rpc.pool as pool;

// At startup:
var pc = pool.default_pool_config();
pc.min_idle = 2;
pc.max_conns = 50;
pc.idle_timeout_ms = 60000;
pc.keepalive_interval_ms = 30000;
pc.event_sink = Optional::Some(<your log sink>);
val p = pool.open(rpc_config, move pc).or_throw();

// Per request handler:
match p.acquire() {
    core.Result::Err(e) => {
        // pool-closed (we're shutting down) → 503
        // pool-open-failed (couldn't open a new conn for you) → transient, retry-after
    },
    core.Result::Ok(lv) => {
        var lease = move lv;
        match lease.conn() {
            core.Result::Ok(c) => {
                // rpc.call(c, &"sp_singular_complete", &args)
            },
            core.Result::Err(_) => { /* invariant — won't happen */ }
        }
        // lease drops at end of scope → conn returns to pool, next waiter unblocked
    }
}
```

## Integration shape — single-conn (managed)

```drift
import mariadb.rpc.managed as managed;

var mc_cfg = managed.default_managed_config();
mc_cfg.keepalive_interval_ms = 30000;
val mc = managed.open(rpc_config, move mc_cfg).or_throw();

match mc.acquire() {
    core.Result::Err(_) => { /* slot busy: keepalive in flight, retry briefly */ },
    core.Result::Ok(lv) => {
        var lease = move lv;
        match lease.conn() { ... }
    }
}
```

## Both expose the same interface

```drift
pub interface ConnectionSource {
    fn acquire(self: &mut Self) nothrow -> core.Result<LeasedConn, ManagedError>;
    fn close(self: &mut Self) nothrow -> core.Result<Void, ManagedError>;
}
```

Your Singular bridge can hold either concretely or depend on the interface and decide at construction time. Starting concretely with `ConnectionPool` is fine — `ManagedConnection` is not on your critical path.

## Pool sizing guidance

- `max_conns`: match to MariaDB `max_connections` with headroom for other clients. 50–100 is typical for an app server.
- `min_idle`: 0 is correct for bursty workloads. Raise to amortize connect cost if your QPS rarely drops to zero.
- `idle_timeout_ms`: 60s default. Conns idle longer get reaped to free server resources. Keepalive doesn't count as use.
- `keepalive_interval_ms`: half of MariaDB `wait_timeout` is a safe floor. 30s default.

## Toolchain requirement

`mariadb-rpc` 0.5.0 requires Drift `0.31.90+abi14` or later, which is **now certified**. `drift trust` + `just prepare` on your side picks it up.

## What's NOT in v1 — for transparency

- **No `acquire_timeout(d)`** — block-wait is unbounded. If you want a deadline, wrap with your own timeout via a separate VT. Easy to add; just hasn't shipped.
- **No prepared statements / stmt cache** — same as `RpcConnection`; this is a `mariadb-rpc` text-protocol library.
- **No per-conn affinity** — `acquire()` returns the most-recently-used conn (LIFO) but doesn't try to bind a request to a specific conn across multiple acquires. Sticky sessions are an app-level concern.

## Where to look

- `packages/mariadb-rpc/src/pool.drift` — the pool implementation.
- `packages/mariadb-rpc/src/managed.drift` — single-conn implementation + `ConnectionSource` interface + `LeasedConn`.
- `docs/effective-mariadb-rpc.md` → "Connection pool" and "Managed connection" sections — full reference.
- `packages/mariadb-rpc/tests/e2e/live_pool_smoke_test.drift` — pool e2e. `just rpc-live-pool` to run.
- `packages/mariadb-rpc/tests/e2e/live_managed_smoke_test.drift` — single-conn e2e. `just rpc-live-managed`.

Ping me if any of the API doesn't match what you wanted.

—SL
