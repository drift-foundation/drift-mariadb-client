# Reply to bookkeeper / singular team — `ManagedConnection` v1 is ready

## TL;DR

`mariadb.rpc.managed` v1 is in `mariadb-rpc` 0.4.0. It matches the shape you asked for: `acquire()` / RAII `LeasedConn` / `close()`, autonomous keepalive with DNS-remap recovery, observable events, no business-level retry. The `ConnectionSource` interface is in place, so swapping in a future `ConnectionPool` is a constructor-line change at your call sites.

## What landed

- `mariadb.rpc.managed` module (~330 LOC): `ManagedConnection`, `LeasedConn`, `ConnectionSource` interface, `ManagedConfig`, `ManagedEvent`, `ManagedError`.
- Doc section in `docs/effective-mariadb-rpc.md` under "Managed connection (`mariadb.rpc.managed`)" with full example, `ManagedConfig` defaults, event variants, and error tags.
- `live_managed_smoke_test` e2e — lifecycle + observed keepalive both pass against the test instance.

## Matches your spec — quick checklist

- **Acquire/release via RAII.** No `release()` method. `LeasedConn::destroy` is the only path back to the wrapper's slot. Can't leak.
- **Mutex protects the storage slot, not the lease.** The conn moves out of `Mutex<Optional<RpcConnection>>` on acquire and back on release. Lock held only at transitions. A 10-minute streaming statement holds no mutex.
- **Autonomous keepalive.** Background `conc.spawn`'d virtual thread pings on the configured interval. On failure, drops the dead conn and calls `rpc.connect(config)` again — re-resolves DNS, so DB host migration during maintenance is recovered transparently.
- **No business-level retry.** `RpcError` from a call goes straight to your code. Microflows owns that policy.
- **Events.** `ManagedConfig.event_sink: Optional<Callback1<ManagedEvent, Void>>` — install if you want logs. Fires for `KeepalivePingOk`, `KeepalivePingFailed`, `ReconnectAttempt`, `ReconnectSucceeded`, `ReconnectFailed`.
- **`ConnectionSource` interface from v1.** Fixed-signature `acquire` / `close`. Concrete `ManagedConnection` implements it now; `ConnectionPool` will implement it later. Depend on the interface at your call sites and the future swap is free.

## Toolchain requirement

Requires Drift `0.31.89+abi14` or later for `conc.sleep` correctness inside the keepalive VT. We've verified end-to-end against `0.31.89` staged (5/5 deterministic runs showing 4-7 ping ticks in 550ms with a 100ms interval, exactly the predicted range). Cert promotion will pull this version when next run.

## Sample integration shape (Singular bridge)

```drift
import mariadb.rpc as rpc;
import mariadb.rpc.managed as managed;

// At startup:
var mc_cfg = managed.default_managed_config();
mc_cfg.keepalive_interval_ms = 30000;
mc_cfg.event_sink = Optional::Some(<your log-routing Callback1>);
val mc = managed.open(rpc_config, move mc_cfg).or_throw();

// On each request:
match mc.acquire() {
    core.Result::Err(e) => { /* surface as FAILED_DB_UNAVAILABLE — let Microflows decide */ },
    core.Result::Ok(lv) => {
        var lease = move lv;
        match lease.conn() {
            core.Result::Ok(c) => {
                // Your CALL sp_singular_complete(...) here.
            },
            core.Result::Err(_) => { /* shouldn't happen */ }
        }
        // lease drops at end of scope → conn returns to slot
    }
}
```

The whole "retry-once-on-wire-error" wrapper you sketched at the start of this thread is no longer needed at your layer. If `rpc.call` returns a wire error during a lease, you surface it; by the time your next `acquire()` runs, the keepalive thread has likely already noticed and reconnected (or is in jittered-backoff retry). Either way, the next acquire gets a healthy conn or a `managed-acquire-busy` (try again briefly).

## What's NOT in v1 — for transparency

- **ConnectionPool**: implements the same `ConnectionSource`, single-conn-to-N is a constructor change at your call site. Not built; design is straightforward. Open if/when bookkeeper actually needs concurrency.
- **`arg_binary_fixed` semantics**: unrelated, already shipped in 0.3.2.
- **Caller-driven keepalive `tick()`**: not added; the autonomous keepalive thread covers the use case and works correctly now that the toolchain bug is fixed.

## Reviewing the diff

- `packages/mariadb-rpc/src/managed.drift` — the module.
- `docs/effective-mariadb-rpc.md` — see "Managed connection" section for the full reference.
- `packages/mariadb-rpc/tests/e2e/live_managed_smoke_test.drift` — both scenarios. `just rpc-live-managed` to run.
- `drift/manifest.json` — version bumped to 0.4.0.

Let me know if anything doesn't match what you had in mind.

—SL
