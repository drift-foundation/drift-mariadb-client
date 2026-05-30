# Acquire Timeout — Concrete Plan

Mandatory, end-to-end deadlines on `ConnectionSource.acquire()` for both
`ConnectionPool` and `ManagedConnection`, surfaced as a distinct timeout error.

**Origin:** request from the bookkeeper / singular (app) team —
`~/src/pushcoin/work/mariadb-rpc-acquire-timeout-request.md`. They are holding
their pool event-sink + call-site wiring until this signature lands.

**Why this is ours, not the app's:** a timeout race that abandons a parked
Condvar wait is easy to get subtly wrong (lost wakeup, deadline reset on
spurious wake), and only the source can apportion *one* user-visible deadline
across its internal phases (wait → open → refill → close races) and emit an
accurate timeout-vs-closed-vs-open-failed distinction. Wrapping `acquire()` in
an app-side `conc` race can't see inside the Condvar loop and would be
reimplemented (and drift) per consumer.

---

## Core decisions (resolved)

1. **Deadline is end-to-end, not just the parked-wait.** `acquire(deadline)`
   returns a usable lease only if the source can provide one before the
   deadline; if the deadline elapses at *any* point during `acquire`, return
   the timeout tag. The caller buys "a lease within N, or an error" and does
   not know/care about internal phases. (App follow-up #1, agreed.)

2. **Mandatory finite `Duration`, no "0 = forever" sentinel.** Per-call
   argument on the interface; no `PoolConfig`/`ManagedConfig` default. This
   mirrors the convention `std.concurrent` already enforces one layer down:
   `Condvar.wait_until` documents "there is no overload via a '0 means forever'
   sentinel." We are not importing a preference — we are matching the primitive.

3. **Clock: `wait_timeout(remaining)`, NOT `wait_until`.** `wait_until` expects
   a monotonic *absolute* timestamp comparable to `thread.now_ms()`, which is
   **not** comparable to the pool's `_now_ms()` (elapsed-since-`PoolInner.boot`,
   `pool.drift:496`). Rather than chase the absolute clock backing `wait_until`,
   compute the deadline from a *local* `time.now_monotonic()` Instant taken at
   `acquire` entry and wait on the recomputed remaining budget each iteration:

   ```
   val start = time.now_monotonic();        // local to this acquire() call
   // ... per wait iteration:
   val remaining = budget_ms - time.elapsed_ms(&start);
   match remaining <= 0 {
       true  => { /* return acquire-timeout */ },
       false => { cv.wait_timeout(&mut guard, conc.Duration(millis = remaining)); }
   }
   ```

   This defeats the **reset-on-spurious-wake bug** (naive `wait_timeout(d)` in a
   re-check loop resets the full budget on every spurious/stolen wakeup) without
   depending on `wait_until`'s clock, and does **not** reuse `boot` (which stays
   dedicated to idle-reap timestamps). `conc.Duration` rejects negative millis,
   so the `remaining <= 0` guard must run before constructing it.

4. **Timeout error tag — OPEN, app-team's call.** Two candidates; present both
   in the reply as a deliberate API decision, do not pick unilaterally:
   - **Shared `acquire-timeout`** — one tag from both impls, so a caller holding
     the `ConnectionSource` interface can branch on timeout without knowing the
     concrete type. Best for substitutability (the reason it's on the interface).
   - **Per-impl `pool-acquire-timeout` / `managed-acquire-timeout`** — what the
     app team literally wrote; matches the existing prefixed convention
     (`pool-closed`, `managed-acquire-busy`) but couples callers to the impl.

   Either way: distinct from `pool-closed` (shutting down → don't retry) and
   `pool-open-failed` (transport → distinct cause). **`pool-closed` wins** when
   the source is closed before/during the wait; timeout wins only when the
   deadline elapses first.

5. **`managed-acquire-busy` fate.** Once `acquire` mandates a `Duration`, the
   "slot busy" case becomes "waited to deadline" → timeout tag. A near-zero
   deadline returns the timeout tag (almost) immediately, replacing today's
   instant `managed-acquire-busy`. Decision: **retire `managed-acquire-busy`
   from the public contract** (it may survive as an internal try-acquire
   fast-path label). This is a behavior change for existing managed consumers —
   call it out in the reply.

---

## Sequencing

**Step 0 lands first and independently** — it is a correctness fix valuable on
its own, and "no I/O under lock" is a precondition for reasoning about acquire
latency at all. The timeout work (Steps 1–3) touches the same release/close
paths, so fixing lock discipline first avoids re-churning them.

---

## Step 0 — FIX: no socket I/O while holding the `PoolSlot` mutex

**Classification:** mariadb-rpc correctness bug (our pool code). *Not* a
toolchain `CORE_BUG` — there is no language/runtime defect to pin; the
defect-policy `CORE_BUG` tag does not apply here.

**The bug:** `_release` calls `rpc.close(&mut c)` while holding the `PoolSlot`
mutex when the conn is unhealthy or the pool is closed (`pool.drift:389`). Wire
`close()` performs real write/close work bounded by `io_timeout_ms`
(`packages/mariadb-wire-proto/src/lib.drift:594`). So a teardown blocks every
concurrent `acquire` / `release` / close-drain behind socket I/O.

**Same defect, other sites:** `close()`'s drain loop (`pool.drift:149-159`) and
`_drain_close` (`pool.drift:538-556`) both call `_close_entry_conn` →
`rpc.close` under the lock. Fix all three under one discipline so the invariant
is actually true.

**Repro shape:** `max_conns = 1`; lease a conn; make it unhealthy (or
`pool.close()` while the lease is still out); drop the lease → `_release` takes
the mutex, enters `not healthy or s.closed`, calls `rpc.close` under the lock;
any concurrent `acquire`/`release`/drain is blocked behind network teardown.

**Root fix:**
- Under lock: update counters, decide requeue-vs-close, `mem.replace` the doomed
  conn out into a local "close-after-unlock" holder (`Optional<RpcConnection>`).
- Drop the guard.
- `rpc.close()` outside the mutex.
- Signal waiters after the counter transition is visible (so a woken waiter sees
  the freed capacity / correct `total_count`). Note: when we close-after-unlock,
  capacity for *acquire purposes* is already reflected by the counter update
  under the lock, so signalling after unlock is fine.

**Regression test (decide seam first):**
- Preferred: `tests/unit/pool_release_no_io_under_lock_test.drift` IF a stub
  conn whose `close()` blocks/sleeps is feasible — a second VT then proves it is
  not blocked during teardown. **No fake-conn seam exists in `tests/unit/`
  today** (existing no-network tests are all state-handoff shape tests); needs a
  seam or is infeasible as a unit test.
- Fallback: e2e under `tests/e2e/` driving real conns + concurrency, asserting a
  concurrent acquire makes progress while one conn is being torn down.
- **Action:** investigate the stub seam before committing to unit-vs-e2e.

**Audit-language outcome:** the "I/O under lock" row changes from a caveat to
"intended invariant; regression pinned + fixed."

---

## Step 1 — Interface + pool end-to-end deadline

**Interface (`managed.drift:153`):**
```drift
pub interface ConnectionSource {
    fn acquire(self: &mut Self, deadline: conc.Duration) nothrow -> core.Result<LeasedConn, ManagedError>;
    fn close(self: &mut Self) nothrow -> core.Result<Void, ManagedError>;
}
```
Both impls adopt the same signature → stay drop-in interchangeable.

**Pool acquire budget split (`pool.drift` `_acquire` / `_acquire_decide`):**
Thread a `budget_ms` + local `start: Instant` through the decide phase:

- **Idle conn available** → return immediately, unless budget already ≤ 0 →
  timeout.
- **Exhausted / parked** → replace `cv.wait(&mut guard)` (`pool.drift:298`) with
  the `wait_timeout(remaining)` loop (Decision 3). On `Err(TIMEOUT)`: decrement
  `s.waiters`, return the timeout tag. On `Err(CLOSED)`: existing `pool-closed`
  path. Keep the existing `waiters` accounting.
- **On-demand open** (`_do_open`) → clamp the cloned cfg's `connect_timeout_ms`
  to `min(remaining_budget, connect_timeout_ms)` before `rpc.connect`.
  - **Residual nuance (document, don't overclaim):** the handshake's *read*
    phase is still separately bounded by `read_timeout_ms` (wire `io_timeout_ms`),
    so the open phase is not perfectly clamped to the remaining budget — it is
    bounded by `min(remaining, connect_timeout_ms)` for the TCP connect plus up
    to `read_timeout_ms` for handshake reads. Typically `read_timeout_ms` ≪
    acquire budget.
  - **Disambiguation after a failed `_do_open`:** if `remaining ≤ 0` →
    timeout tag; else → `pool-open-failed`. (App follow-up #1: "timeout wins
    only when the deadline elapses first.")

- **Keepalive path unchanged:** `is_keepalive = true` is try-acquire (never
  waits) — pass a zero/expired budget or keep the existing no-wait branch so
  keepalive never parks.

**Default `Duration` for keepalive's internal `_acquire`:** keepalive calls
`_acquire(inner, true)`; give the internal signature a budget param and pass an
already-expired/zero budget for the keepalive try-acquire so it hits the
no-wait branch.

---

## Step 2 — ManagedConnection: closed-state + Condvar + close() wake

This is the larger lift (the cost the first plan understated). Today
`ManagedConnection.acquire` fails instantly with `managed-acquire-busy` when the
slot is taken (lease out, keepalive in flight, or reconnect gap); the slot is a
bare `Mutex<Optional<RpcConnection>>` with no Condvar and no closed flag.

**Changes to `ManagedInner` (`managed.drift:86`):**
- Promote the slot to a small struct, e.g.
  `ManagedSlot { conn: Optional<rpc.RpcConnection>, closed: Bool }`, guarded by
  the existing mutex. (`stop_flag` AtomicBool stays for the keepalive loop; the
  mutex-guarded `closed` is what the acquire wait loop checks.)
- Add `cv: conc.Condvar` to `ManagedInner`.

**`acquire` (deadline-bounded):** local `start` Instant; loop: lock, check
`closed` → managed-closed; try-take conn → Ok; else `remaining ≤ 0` → timeout,
else `cv.wait_timeout(remaining)`. Same loop shape as the pool.

**Signal points:** `_slot_put` (lease release, `managed.drift:180`) and the
keepalive reconnect refill must `cv.signal_one()` after putting the conn back so
a parked acquirer wakes.

**`close()` (`managed.drift:188`) must wake waiters:** add `cv.close()` (or set
`closed = true` under the mutex + `signal_all`) so parked acquirers wake with
CLOSED → managed-closed, instead of only discovering shutdown by timing out.
Without this, managed conflates shutdown with saturation — the exact failure the
app team is eliminating.

---

## Step 3 — Tests, docs, perf

**Tests:**
- Step 0 regression (above).
- Pool: acquire returns timeout under saturation before deadline; returns lease
  when a release arrives within budget; `pool-closed` beats timeout when closed
  during wait; spurious-wake does not extend the deadline (budget honored across
  multiple wakeups).
- Pool open-phase: `_do_open` clamps connect timeout to remaining; failed open
  near deadline returns timeout, failed open with budget left returns
  `pool-open-failed`.
- Managed: deadline-wait clears when in-flight lease/keepalive releases;
  `close()` wakes a parked acquirer with managed-closed (not timeout).
- Live smoke updates: `tests/e2e/live_pool_smoke_test.drift`,
  `live_managed_smoke_test.drift` adopt the new `acquire(deadline)` signature.

**Docs:**
- `docs/effective-mariadb-rpc.md`: update §"Block-wait on exhaustion"; add the
  **"which calls can block, and what bounds them"** table the app team asked for
  (corrected version below).
- `history.md` entry; bump `TODO.md` (this closes a Phase 3 hardening gap).

**Perf:** `just perf` must show wire byte/packet metrics **unchanged** — this is
a control-flow/timeout change, not a wire change. Confirm zero delta against the
machine-keyed baseline.

**Versioning:** API-breaking interface change (`acquire` gains a required arg) →
**minor** bump of `mariadb-rpc`. `mariadb-wire-proto` only bumps (patch) if Step
0 touches its `close()` path; the bug is in the rpc pool, so likely wire is
untouched. Re-mint author-claims on any manifest version bump (per 0.33.8 deploy
notes).

---

## Blocking-entry-point audit (corrected — for the reply + docs)

| Entry point | Blocks? | Bounded by |
|---|---|---|
| `ConnectionPool.acquire` (exhausted/parked) | **Yes — unbounded today** | **FIX: end-to-end acquire deadline → timeout tag** |
| `ConnectionPool.acquire` on-demand open (`_do_open`→`rpc.connect`) | Yes | `min(remaining budget, connect_timeout_ms)` + handshake reads ≤ `read_timeout_ms` |
| `pool.open()` seeding `min_idle` | Yes — **serial** | `connect_timeout_ms` per conn (worst case `min_idle × connect_timeout_ms`) |
| `managed.open()` initial connect | Yes | `connect_timeout_ms` |
| `ManagedConnection.acquire` (slot busy / keepalive / reconnect gap) | Today: instant `managed-acquire-busy` → **gains end-to-end deadline wait** | new Condvar + closed-state + `close()` wake |
| keepalive ping / statement I/O | Yes | `read_timeout_ms` (mapped to wire `io_timeout_ms`); `write_timeout_ms` validated but **not consumed** today |
| keepalive acquiring a conn | No | try-acquire (`is_keepalive=true`, never parks) |
| `pool.close()` → `vt.cancel()` + `vt.join()` | Yes | `cancel()` precedes `join`; bounded by keepalive cancel-responsiveness (consider `join_timeout`) |
| `conc.lock(slot)` | Briefly | O(1) transitions, **zero I/O under lock** — *violated today (Step 0), true after fix* |

---

## Open questions for the app team (for the reply)

1. **Tag shape** (Decision 4): shared `acquire-timeout` vs prefixed
   `pool-acquire-timeout` / `managed-acquire-timeout`. Recommend shared for
   substitutability; their call.
2. **Constructor deadlines?** `open()` paths block on initial connect, bounded
   only by `connect_timeout_ms`. In scope, or is acquire-only sufficient?
3. **`managed-acquire-busy` retirement** (Decision 5): confirm they're fine with
   the instant-busy error being replaced by the timeout tag.
