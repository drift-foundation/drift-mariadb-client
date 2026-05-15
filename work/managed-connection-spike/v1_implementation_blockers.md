# mariadb.rpc.managed v1 — toolchain blockers (historical)

**Original date:** 2026-05-14 against staged 0.31.81.
**Latest re-test:** certified 0.31.84 (git 6188f7a7).
**Status:** Lifecycle path consistently passes on both toolchains. Issues 1 and 2 below were 0.31.81-specific (since fixed in 0.31.83+). **Issue 3 (the keepalive blocker) is now narrowed to a different root cause** — see `reply_to_toolchain_after_probes.md` for the current, sharper diagnosis on certified 0.31.84.

The framing in this file is preserved for historical context but should be read alongside `reply_to_toolchain_after_probes.md`, which supersedes the Issue 3 hypothesis below.

## Background

The bug fixed in 0.31.81 (`mem.replace` rejecting named `&mut T`) unblocked the design. The spike workaround was removed; the production-shape `Mutex<Optional<RpcConnection>>` compiles. Implementation of `packages/mariadb-rpc/src/managed.drift` proceeded and the lifecycle path works end-to-end against the live test DB.

Then the keepalive VT — a `conc.spawn`'d virtual thread that calls `conc.sleep(Duration(millis = interval))` in a loop — exposed three separate compiler / runtime issues.

## Issue 1: `arc.get().field` projection on non-Copy fields fails (CORE_BUG)

Compilation error chain when accessing `inner.get().slot` (where `inner: conc.Arc<ManagedInner>` and `slot: conc.Mutex<...>`):

```
error: cannot copy value of type 'std.concurrent.Mutex<...>' (use move <expr>) [E-AUTO-dfb0f287]
```

Same error for any non-Copy field through `arc.get().field` chained access — `AtomicBool`, `RpcConnectionConfig`, `Optional<Callback1<...>>` all trip it.

**Workaround in production code:** bind `val inner_ref: &ManagedInner = inner.get();` as an intermediate, then write `&inner_ref.slot`. Works but uglies up every access site, so we extracted projection helpers (`_slot_take`, `_slot_put`, `_inner_stop_flag`, etc.) into one block of code in `managed.drift`.

This contradicts the documented Arc behavior at `doc/stdlib/std_core_arc.md`:
> `Arc<T>` … Implements `core.Borrow<T>` and `core.BorrowMut<T>`, so it composes with code that takes `&T` / `&mut T` via UFCS resolution.

The compose-via-UFCS claim doesn't survive a chained `.field` access on a non-Copy field.

## Issue 2: `captures(share x)` + later move of `x` trips SSA pass (CORE_BUG)

```drift
var inner_arc: conc.Arc<ManagedInner> = ...;
var cb = core.callback0(| | captures(share inner_arc) => { ... });
var vt = conc.spawn(move cb);
return ManagedConnection(inner = move inner_arc, ...);  // move after share
```

Crashes with:
```
RuntimeError: SSA: load before store for local 'inner_arc' in multi-block rename
```

The pattern is straight from `doc/effective-drift.md:131` (Shared state + callbacks). On 0.31.81 it crashes the compiler's SSA pass.

**Workaround:** explicit clone + `captures(move <clone>)`, then move the original:
```drift
var inner_for_thread = inner_arc.clone();
var cb = core.callback0(| | captures(move inner_for_thread) => { return _keepalive_loop(move inner_for_thread); });
return ManagedConnection(inner = move inner_arc, ...);
```

Note also that closure syntax requires `| |` with a space between bars — `||` is rejected as a different token.

## Issue 3: `conc.sleep` in a spawned VT does not park (RUNTIME_BUG, blocking)

The keepalive loop:
```drift
while not _inner_stop_flag(&inner) {
    match conc.sleep(conc.Duration(millis = 100)) {
        core.Result::Err(_) => { return 0; },
        core.Result::Ok(_) => {}
    }
    // ... ping, emit, put back
}
```

Behavior across 5 runs of `tests/e2e/live_managed_smoke_test.drift` (main thread sleeps 550ms, expects ≥1 keepalive tick on a 100ms interval):

| Run | Observed pings |
|---|---|
| 1 | 2702+ |
| 2 | 2702+ |
| 3 | 0 |
| 4 | 0 |
| 5 | 0 |

Two failure modes, neither correct:
- **Spin-loop mode (runs 1, 2):** `conc.sleep(Duration(millis=100))` returns immediately. Each loop iteration is ~200µs (the cost of `rpc.ping` + slot mutex). 2700 iterations in 550ms ≈ ~5000 ticks/sec instead of the expected ~5/sec.
- **No-schedule mode (runs 3–5):** the keepalive VT doesn't run at all during main's 550ms `conc.sleep`. Zero events fire.

Both contradict the documented behavior at `doc/stdlib/std_concurrent.md:190`:
> `sleep(d: Duration) nothrow -> core.Result<Void, ConcurrencyError>`
> Suspends the calling virtual thread for `d`.
> Off the virtual-thread runtime (e.g. on the main thread before any VT exists), this yields control for `d.millis` real milliseconds without registering a reactor timer.

Inside a `conc.spawn`'d VT, sleep should register a reactor timer and the VT should be parked until it fires. Neither behavior matches.

Possible root causes (speculation — toolchain team would know):
1. The default executor's reactor isn't being polled when main is sleeping (main isn't a VT, so it doesn't drive the runtime).
2. `Duration(millis = N)` is being passed to sleep but isn't honored for VT-context calls.
3. The spawned VT is being scheduled on the same thread as main but main's sleep doesn't yield back to the executor reactor.

Repro: `packages/mariadb-rpc/tests/e2e/live_managed_smoke_test.drift` against the staged 0.31.81 toolchain. Run multiple times to see the nondeterminism.

## What we shipped vs what's blocked

**Shipped and working** (uncommitted, in working tree):
- `packages/mariadb-rpc/src/managed.drift` (~330 LOC) — full implementation matching the design memory.
- `packages/mariadb-rpc/tests/e2e/live_managed_smoke_test.drift` — lifecycle scenario passes consistently.
- `justfile` recipe `rpc-live-managed`.
- `drift/manifest.json` updated to include `managed.drift` in the `mariadb-rpc` artifact.

**Blocked on Issue 3 (sleep / VT scheduling):**
- Autonomous keepalive end-to-end behavior. The plumbing is there and emits events correctly; the timer that should bound CPU usage and decide tick cadence is broken.
- The keepalive scenario in the smoke test is therefore disabled until the runtime issue is resolved. Lifecycle scenario passes.

Once Issue 3 is resolved on a staged toolchain, no source changes should be needed — `conc.sleep` is the right primitive; we just need it to honor the documented contract.

## Suggested next steps

1. Send Issues 1, 2, 3 to the toolchain team along with this file. Issue 3 is the only true blocker for v1 cert.
2. While waiting, the manifest is ready, the lifecycle code is verified, and the doc/version bump can land in the same commit when Issue 3 clears.
3. The bookkeeper team reply can mention v1 is "structurally complete; keepalive cadence pending toolchain fix" so they aren't surprised.
