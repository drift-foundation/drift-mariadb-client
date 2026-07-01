> **UPDATE 2026-07-01 — RESOLVED in staged `drift-0.33.67+abi19`.** Both items below are
> fixed there: (1) dropping a `VirtualThread` handle now abandons only the result handle,
> not pending submitted work — detached spawn-per-connection works (verified: two overlapping
> proxy clients finish in ~1s, handlers on distinct vtids); (2) `TcpStream.peer_addr()` was
> added and now populates our `client_accept` log. No further action on these.
>
> **NEW item to report back (undocumented in the release notes):** staged 0.33.67 now requires
> the `--entry` target to be `pub` — both `pub fn main` (every test) and `pub fn service_main`
> (apps). This breaks all existing test/app entrypoints repo-wide until migrated. `pub fn main`
> also compiles on abi18, so it's forward-safe, but the change isn't in the release notes — is
> it intended (please document) or an accidental tightening? We migrated our 51 test mains +
> app entry + the `emit_test_plan` entry-detection regex.

# Toolchain question — `conc.spawn`: dropping the VirtualThread handle abandons the task

**From:** mariadb-client (failpoint-proxy work)
**To:** Drift toolchain / stdlib-concurrency team
**Toolchain:** certified `drift 0.33.64 | abi 18 | git c987c33f`
**Date:** 2026-06-30

## Summary

Dropping a `conc.spawn` `VirtualThread<T>` handle **before the task has run** appears to
**abandon the task** — it never executes. Fire-and-forget / detached spawn does not work.
We can't tell from the docs whether this is intended (structured-concurrency, cancel-on-drop)
or a runtime bug, and there is no `detach()` API, so we're asking before we build around it.

## What we're doing

Building a `kind:app` daemon (a MariaDB wire fault-injection proxy). The natural shape is an
accept loop that spawns one detached handler virtual thread per accepted connection:

```drift
while true {
    match listener.accept(...) {
        core.Result::Ok(cs) => {
            var cb = core.callback0(| | captures(move cs, ...) => { return _handle_client(...); });
            val _vt = conc.spawn(move cb);   // handle intentionally dropped: fire-and-forget
        },
        ...
    }
}
```

Symptom: the handler never runs. `_handle_client`'s first statement never executes; the
connection is accepted (kernel backlog) but never serviced.

## Isolated repro (no sockets, no saturation)

`packages/mariadb-rpc/tests/e2e/spawn_drop_probe_test.drift` — run with
`just check-one packages/mariadb-rpc/tests/e2e/spawn_drop_probe_test.drift`.

It spawns two trivial tasks that each set a shared `Arc<AtomicBool>`:
- **A**: handle dropped at end of an inner fn, then the caller `conc.sleep`s 500ms.
- **B** (control): handle `.join()`ed.

Exit code encodes the outcome. **Observed: exit 1** = *A did NOT run, B DID*. So with 500ms of
scheduling time and only two sequential spawns (no executor saturation), the dropped-handle task
is never executed. Joining (or otherwise retaining) the handle runs it.

## Why we think it may be a bug (or at least a spec/doc gap)

Per `doc/stdlib/std_concurrent.md`:
- `spawn<T>` returns a `VirtualThread<T>`; the only documented drop behavior is on the
  **result buffer**: "Drop, join, and cancel paths linearize through the mutex; only
  `ResultState`'s `Destructible` frees the buffer … when the last `Arc` clone dies." Nothing
  says dropping the handle **cancels or unschedules the task**.
- There is **no `detach()`** and no `spawn_detached`. `cancel()` is documented as *cooperative*
  cancellation (explicit), and `Scope` is described as "reserved … for future scoped-spawn APIs."

So either:
1. **Intended (structured concurrency / cancel-on-drop):** then (a) please document it on
   `spawn`/`VirtualThread` drop, and (b) tell us the supported way to run a detached
   fire-and-forget task — a `detach()`, a `spawn_detached`, or a blessed keep-alive registry
   pattern. A `Scope`-based API would also answer this.
2. **A runtime bug:** a pending (not-yet-scheduled) task is dropped from the run queue when the
   handle's `Arc` clone is released, instead of running to completion independently.

## Questions

1. Is drop-cancels-a-pending-task intended, or a bug?
2. If intended: what is the supported detached fire-and-forget primitive? (Retain-all-handles +
   `join_timeout(0)` reaping works but is clumsy for a long-lived accept loop.)
3. Does the behavior differ for a task that has *already started* vs. one not yet scheduled
   (i.e., does drop cancel a running task too)?
4. Is `submit_error` relevant here (we did not `.join()`, so a refused submission would be
   silent) — or is the task genuinely submitted-then-dropped?

## Impact / what we need

We want genuine per-connection concurrency (no one-connection-at-a-time limitation). The clean
model is detached spawn-per-connection. If drop-cancel is intended, we'll adopt the supported
detach primitive; if it's a bug, a fix would let the natural pattern work. Either way we'd like
the `std_concurrent` docs to state the drop/detach contract explicitly.
