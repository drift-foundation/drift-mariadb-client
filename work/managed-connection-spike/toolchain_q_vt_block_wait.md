# Question for toolchain team — VT-safe block-and-wake primitive

## Context

Building `mariadb.rpc.pool.ConnectionPool` (elastic pool of `RpcConnection`s) on top of the `ConnectionSource` interface that shipped in `mariadb-rpc` 0.4.0 with `ManagedConnection`. Target consumer is an app server expecting hundreds of concurrent acquirers.

## What we need

A VT-safe **block-and-wake** primitive for the acquire path:

```drift
pool.acquire() :=
    if available.pop() exists:
        return it as a LeasedConn
    elif active_count < max_conns:
        open a fresh conn, return it as a LeasedConn
    else:
        ** PARK THE CURRENT VT UNTIL release() signals **
```

And on release (`LeasedConn::destroy`):
- push the conn back to `available`
- **wake one waiter** if any are parked

The block-wait path needs to be:
- **VT-correct**: the parked VT yields its OS thread so other VTs (incl. the keepalive thread, in-flight statements on other leases, the lease-release path that's going to wake us) can run. A spin/`conc.sleep` loop is wrong here — we'd burn CPU at high contention and starve the very thread that's going to unblock us if the executor is single-worker.
- **Multi-waiter**: dozens or hundreds of waiters may park simultaneously. FIFO-ish ordering is nice-to-have but not required; any "one wake per release" semantics works.
- **Composable with timeout**: callers may want `acquire_timeout(d)` so they can give up after a deadline.
- **Cancellation-aware**: if `close()` is called while waiters are parked, they should unpark with a sensible error (`pool-closing` or similar) — not hang forever.

## What I see in std.concurrent / std.sync today

- `conc.Mutex<T>` — spin lock; "hold for short critical sections only" per the doc. Not a parking primitive.
- `conc.AtomicBool` / `conc.AtomicInt` — atomic cells, no built-in wait/notify.
- `conc.sleep(Duration)` — parks the VT on a timer. Doesn't compose with "wake on event."
- `conc.spawn(...) -> VirtualThread<T>` + `vt.join()` — `join` parks the calling VT until the spawned VT completes. Could be abused as a one-shot signal channel (spawn a VT that does nothing; releaser completes it via... well, the VT runs and exits on its own — no external "complete now" knob).
- `sync.MpscQueue<Handle<T>>` — non-blocking pop (`Option<Handle<T>>`). Not blocking.
- `conc.scope` — currently a "thin shape" per the doc; reserved as the entry point for future scoped-spawn APIs.

I don't see a `Condvar`, `Semaphore`, `Notify`, or blocking-channel `recv` in the documented surface. Did I miss one?

## What we'd like to know

1. **Is there a supported primitive today** that gives us "park this VT until someone signals; signaler can wake one or many"? If yes, pointer to it and a tiny usage shape.

2. **If not**, what's the recommended pattern to build it from what's there? Some candidates we've considered:

   - **Sleep-poll loop**: `acquire()` spins on `available.pop()` with `conc.sleep(small_d)` between attempts. Simple but exactly the busy-wait you'd expect us to avoid; backpressure has to be bounded by some per-attempt latency floor.
   - **MpscQueue as wakeup channel**: each waiter `spawn`s a tiny ack-VT, registers its `VirtualThread<Void>` handle somewhere, then `join`s its own ack-VT. Releaser... has no way to externally complete someone else's VT. Doesn't work.
   - **Atomic counter + sleep**: waiters increment a "waiter count" atomic; releaser increments a "release count" atomic; waiter loops checking `released > my_waiter_index` with sleep. Still polling, just smarter accounting.
   - **Custom condvar built on a Mutex-protected wait list of `AtomicHandle<VT>`** — but we don't have a way to "wake a sleeping VT externally" even given its handle, as far as I can tell.

3. **Is there a planned slice for a parking primitive** in the 0.31.x or 0.32 line? If something is on the roadmap close enough to land, we can shape `ConnectionPool` to use it from day one rather than shipping a stopgap and rewriting.

4. **Is `VirtualThread<T>.cancel()` the right shape for "unblock a parked VT"?** If we park a VT via some hack and then the pool's `close()` is called, can we use cancel to surface that as a `ConcurrencyError(kind=CANCELLED)` cleanly?

## Why this matters

Our app-server consumer expects ~100s of concurrent acquirers. Without a real block-wait primitive we either:
- Ship `ConnectionPool` with `fail-fast + caller retries` semantics (works but pushes backpressure to every consumer's call site).
- Ship with a sleep-poll loop in `acquire()` (works but burns CPU + latency under contention).
- Wait until a primitive lands.

The "fail-fast" stopgap is probably acceptable for the v1 cut of the pool if a real primitive is coming in a known window. We can implement the right shape later without breaking the `ConnectionSource` interface that callers see.

Thanks,
SL
