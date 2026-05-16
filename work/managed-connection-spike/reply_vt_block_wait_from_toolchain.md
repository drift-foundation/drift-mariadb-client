# Reply: VT-safe block-and-wake primitive — what exists today

**Toolchain:** driftc 0.31.89 (current source), ABI 14.
**Replies inline to your four questions.**

## TL;DR

You have everything you need today; it's just not behind a `std.sync.*`
wrapper yet.

The primitive is `lang.thread.vt_park` / `vt_unpark`:

```drift
@intrinsic pub fn vt_park(reason: Int) nothrow -> Void;
@intrinsic pub fn vt_park_until(deadline_ms: Int) nothrow -> Void;
@intrinsic pub fn vt_unpark(vt: VtHandle) nothrow -> Void;
@intrinsic pub fn vt_current() nothrow -> VtHandle;
@intrinsic pub fn vt_cancel(vt: VtHandle) nothrow -> Int;
```

These are the same primitives `std.net._block_on_io` and
`std.concurrent.sleep` already use under the hood — they're stable
runtime contract, not experimental.

The `vt_park` / `vt_unpark` pair has a wake-token built in that
handles the canonical race: if `vt_unpark(h)` fires BEFORE `h` enters
`vt_park`, the token is recorded and the subsequent park returns
immediately. So you cannot lose a wake because of "release ran
before park."

**Note on stability**: 0.31.89 just shipped a fix for a stale-token
leak in the FAST-I/O direct-resume path that, pre-fix, made
`conc.sleep` instant-return after some I/O sequences. That fix was
on a different code site than `vt_park`/`vt_unpark` directly, but
the same VT scheduler — pin 0.31.89+ for any pool work.

## Q1 — Is there a supported primitive today?

**Yes:** `lang.thread.vt_park` + `vt_unpark` + `vt_current`. See above.
You didn't miss a `std.sync.Condvar` — there isn't one. The runtime
primitive is there; only the ergonomic `std.*` wrapper is missing.

`lang.thread` is documented as "internal stdlib infrastructure" with
the recommendation that app code goes through `std.*` wrappers, but
that's an ergonomic guideline.  The intrinsics are `pub`, stable,
and used by stdlib itself.  Until a `std.sync.Notify` or
`std.sync.Semaphore` lands (see Q3), the pool can call
`lang.thread.vt_*` directly with a thin local wrapper — see Q2.

## Q2 — Recommended pattern

Build a minimal `Notify` over `vt_park`+`vt_unpark`+`MpscQueue`:

```drift
import lang.thread as thread;
import std.sync as sync;

pub struct Notify {
    waiters: sync.MpscQueue<Int>,  // queue of VtHandle (Int alias)
}

pub fn notify_one(self: &mut Notify) nothrow -> Bool {
    match self.waiters.pop() {
        core.Option::Some(vt) => { thread.vt_unpark(vt); return true; },
        default => { return false; }
    }
}

pub fn wait(self: &mut Notify) nothrow -> Void {
    val me = thread.vt_current();
    self.waiters.push(me);
    thread.vt_park(0);   // park indefinitely; vt_unpark or vt_cancel wakes us
}

pub fn wait_timeout(self: &mut Notify, deadline_ms: Int) nothrow -> Bool {
    val me = thread.vt_current();
    self.waiters.push(me);
    thread.vt_park_until(deadline_ms);
    // On wake: either the deadline fired or someone called vt_unpark.
    // The wake-token protocol means a missed-wake is impossible: if
    // notify_one ran between our push and our park, the token is
    // recorded and park_until returns immediately.  Caller checks
    // the application-level condition (e.g. did `available` get a
    // conn?) and re-parks if necessary.
    return true;  // shape; tighten the contract for your callers
}
```

For your `acquire` path:

```drift
pub fn acquire(self: &mut ConnectionPool) -> core.Result<LeasedConn, PoolError> {
    loop {
        match self.available.pop() {
            core.Option::Some(conn) => { return core.Result::Ok(LeasedConn(conn)); },
            default => {}
        }
        if self.active_count < self.max_conns {
            // open fresh conn ... return as LeasedConn
        }
        // No conn, at cap: wait for a release.
        self.notify.wait();
        // Loop: re-check `available` and `active_count`.  We don't
        // trust the wake — the conn may have been grabbed by a faster
        // waiter (the queue is single-wake, not "this specific
        // waiter gets it").
    }
}

pub fn release(self: &mut ConnectionPool, conn: RpcConnection) nothrow -> Void {
    self.available.push(conn);
    val _ = self.notify.notify_one();
}
```

**Critical detail**: the loop-after-wake is load-bearing.  `notify_one`
wakes one waiter, but between wake and the woken waiter actually
running, ANOTHER acquirer may steal the conn (e.g. via a fresh
`pop()` that races).  The woken waiter must re-check the underlying
predicate (`available.pop()` returns Some, or `active_count <
max_conns`) and re-park if it lost the race.  This is the standard
condvar-style contract.

## Q3 — Roadmap

No committed `std.sync.Notify` / `Semaphore` / `Condvar` slice yet.
The runtime primitives are stable; the ergonomic wrapper is
unblocked but not scheduled.  Given the maria-rpc 0.4 pool is real
user demand, this is a reasonable candidate for the 0.32 line — but
**no commitment** at this point.

Recommendation: ship the in-package `Notify` (or `PoolWaiters`)
wrapper now.  When `std.sync.Notify` lands, the migration is
mechanical (same primitive underneath, just a different import).
Don't block the v1 pool cut waiting for a stdlib wrapper.

## Q4 — `vt_cancel` for unblocking on `close()`

**Yes, exactly the right shape.**  `vt_cancel(handle)` does:

1. Atomically sets the VT's `cancelled` flag.
2. Bumps `park_token`, which causes the VT's next `park_until`
   (already running) to return immediately.

For the pool's `close()`:

```drift
pub fn close(self: &mut ConnectionPool) -> core.Result<Void, PoolError> {
    self.closed.store(true);
    // Drain all parked waiters by cancelling each.
    loop {
        match self.notify.waiters.pop() {
            core.Option::Some(vt) => { val _ = thread.vt_cancel(vt); },
            default => { break; }
        }
    }
    // ... close in-flight conns ...
}
```

The woken waiter's loop should then check `self.closed.load()` and
return `Result::Err(PoolError(kind=CLOSED))`:

```drift
pub fn acquire(self: &mut ConnectionPool) -> core.Result<LeasedConn, PoolError> {
    loop {
        if self.closed.load() { return core.Result::Err(...CLOSED...); }
        // ... pop / open-fresh checks ...
        self.notify.wait();
        // Wake — could be a real release OR a cancel from close().
        // The `closed` check at the top of the next iteration handles
        // both uniformly.
    }
}
```

`vt_cancel` is a one-way edge — once cancelled, the VT's `cancelled`
atomic stays set.  If the waiter VT had other work to do after the
wake, it would observe `cancelled` and short-circuit; for the pool
case that just means "next iteration sees `self.closed.load()` and
returns".

## Suggested shape for the v1 pool cut

Ship the pool with a real `Notify`-style wait inside the package
(not as `std.sync`), under a path like
`packages/mariadb-rpc/src/pool/notify.drift`.  Keep the public
`ConnectionPool` / `ConnectionSource` surface stable.  When
`std.sync.Notify` lands later, swap the internal import with no
public-API change.

Don't ship fail-fast.  The primitive is there and the wrapper is
~40 lines.  Sleep-poll is the wrong call — exactly the "burn CPU at
contention" failure mode you flagged, plus the same VT-scheduler
fairness problem (sleeping VT might block its own waker on a
single-worker executor).

— K
