# To: bookkeeper / singular team — acquire deadlines have landed (mariadb-rpc 0.6.0)

**From:** mariadb-rpc team
**Re:** your request — mandatory deadlines on `acquire` (and a structured timeout)

Your request shipped and is certified on the latest toolchain. You can unblock
the bookkeeper work (pool event-sink + wiring the deadline at call sites) — and
delete any interim app-side timeout race you were about to write; the library
owns it now.

- `mariadb-rpc` **0.5.2 → 0.6.0** (breaking)
- `mariadb-wire-proto` **0.3.3 → 0.4.0** (breaking; co-artifact, pulled in via the `@0.4` range)

Both are deploy-certified: full `just test` (incl. ASAN + memcheck, 0 errors / 0
leaks), `just stress`, and `just perf` (wire bytes/packets byte-for-byte
unchanged — pure control-flow).

---

## The contract you now have

`acquire(timeout)` is a **logical operation with an end-to-end deadline**: "give
me a usable connection within ~timeout, or a structured timeout error." The
deadline bounds the **whole** call — pool wait + DNS/TCP connect + handshake +
auth + `SET NAMES` + autocommit — not a per-socket timer. Per-op
`read_timeout_ms` stays as a low-level guardrail, capped by the remaining
deadline.

## What changed (breaking — both must be updated)

**1. `acquire` now takes an `AcquireWait`, with a configurable default + no-arg form:**

```drift
pub variant AcquireWait { UseDefault, Forever, For(timeout: conc.Duration) }

// Configure the default once (PoolConfig / ManagedConfig); default is None = block forever:
pc.acquire_timeout = Optional::Some(conc.Duration(millis = 5000));   // or leave None to block

// Then the common path is the no-arg overload (uses the configured default):
match src.acquire() {
    core.Result::Ok(lv)  => { var lease = move lv; /* use lease.conn() */ },
    core.Result::Err(e)  => { /* branch on e.tag — see below */ }
}

// Override per call (always wins over the default):
src.acquire(AcquireWait::For(timeout = conc.Duration(millis = 500)))   // strict cap
src.acquire(AcquireWait::Forever())                                    // block indefinitely
```

- On the `ConnectionSource` interface, so **`ManagedConnection` and
  `ConnectionPool` adopt it together** — drop-in interchangeable. (The no-arg
  `acquire()` is a concrete-type overload — interface method names can't be
  overloaded — so via an interface-typed value use `acquire(AcquireWait::UseDefault())`.)
- Both **block-forever and strict-cap are first-class**: set the default for
  your app's posture, override per request when needed. A finite `For(d)` with
  `d <= 0` returns `acquire-timeout` immediately even if a conn is free.

**2. `acquire` is now `&Self` (was `&mut Self`):**

- It only reads internal state and is internally synchronized, so you can share
  one source across worker VTs via **`Arc<ConnectionPool>`** and acquire
  **concurrently** — no outer `Mutex` serializing acquires. If you were planning
  `Arc<Mutex<Source>>`, drop the `Mutex`. (`close` stays `&mut`.)

## Error model — branch on `e.tag`

| Tag | Meaning | Suggested action |
|---|---|---|
| `acquire-timeout` | deadline elapsed (interface-level; **both** pool and managed emit it) | retriable / shed load |
| `pool-closed` / `managed-closed` | source is shutting down | stop; don't retry |
| `pool-open-failed` | on-demand connect failed for a transport reason, within the deadline | transport-level handling |

> **Note on the tag name:** you asked for `pool-acquire-timeout`. We made it a
> single **interface-level `acquire-timeout`** instead, so code that branches on
> "did this time out?" works without knowing whether it holds a `ConnectionPool`
> or a single `ManagedConnection` — that substitutability is the whole point of
> the shared interface. `pool-closed` / `pool-open-failed` stay impl-specific
> because they're source-specific failure modes.

## Which calls can block, and what bounds them (the audit you asked for)

| Call | Bounded by |
|---|---|
| `acquire(timeout)` — pool wait, on-demand open, managed busy-slot wait | **the `timeout` you pass** (end-to-end) |
| `close()` | keepalive cancel + join (prompt; no network wait) |
| keepalive ping/reconnect (internal) | config timeouts; never blocks your acquire |
| **post-lease ops** — `call` / `next_event` / `commit` / `rollback` / `ping` / `close` on a leased conn | **only per-op `read_timeout_ms` today** — see follow-up |

**Follow-up (heads-up, not in this release):** post-lease operations on a
`LeasedConn` are still bounded only by the per-op `read_timeout_ms`, not a
logical-operation deadline. Giving `call`/`commit`/etc. their own finite
caller deadlines is the next API pass. So `acquire`'s deadline covers getting a
usable conn; it does **not** cover a slow query you run on the lease afterward —
keep your own statement-level timeout/cancellation for now if you need it.

## Migration checklist

1. Bump deps: `mariadb-rpc@0.6.0` (and `mariadb-wire-proto@0.4.0` resolves with it).
2. Pick your posture once via `PoolConfig`/`ManagedConfig.acquire_timeout` — `None` (block, the default) for "OK to wait", `Some(d)` for a default cap.
3. Use the no-arg `acquire()` on the common path; override per request with `acquire(AcquireWait::For(...))` or `acquire(AcquireWait::Forever())`.
4. Branch on the error tags below (esp. `acquire-timeout` → retriable, `*-closed` → shutting down). (Note: with the default `None`/`Forever`, you won't get `acquire-timeout` — that only fires for a finite `For`/configured cap.)
5. If sharing a source for concurrent acquire, use `Arc<ConnectionPool>` (no `Mutex`).
6. Delete any interim app-side `acquire` timeout race.
7. Wire your pool event-sink (`PoolConfig.event_sink`) — `AcquireWaiting` / open / reap / keepalive events are available.

Docs: `docs/effective-mariadb-rpc.md` (shipped as a package asset) has the full
model, `ConnectionSource` interface, and error tables. Ping us with questions or
if the post-lease-deadline follow-up should be prioritized.
