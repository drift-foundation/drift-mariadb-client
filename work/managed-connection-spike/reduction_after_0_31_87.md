# Sleep-after-rpc.connect — reduced repro for toolchain team

**Toolchain:** drift-0.31.87+abi14 (git de5fbaca) certified
**Date:** 2026-05-15
**Repro files in repo:**
- `packages/mariadb-rpc/tests/spike/reduce_sleep_after_connect_test.drift` — layered isolation L0 → L3.
- `packages/mariadb-rpc/tests/spike/reduce_sleep_l3_only_test.drift` — minimal isolated trigger, deterministic.

## Headline

After `rpc.connect()` returns successfully and **while the connection is still open**, `conc.sleep` returns in ~0ms regardless of the requested duration. As soon as `rpc.close()` is called, the next `conc.sleep` parks correctly. Deterministic across runs.

## Reproduction matrix

### Layered isolation (one binary, scenarios in order)

```
--- L0: baseline sleep                                ---  elapsed=550   OK
--- L1: net.connect only                              ---  elapsed=551   OK
--- L2: net.connect + 1 socket read                   ---  elapsed=550   OK
--- L2b: net.connect + 1 read + 1 write               ---  elapsed=550   OK
--- L2w: wire.connect (full handshake, no rpc layer)  ---  elapsed=551   OK
--- L2q: wire.connect + 1 wire.query (drain to end)   ---  elapsed=550   OK
--- L3: full rpc.connect                              ---  elapsed=550   OK (!)
```

When scenarios run in this order, L3 sleeps correctly. So the bug is not "rpc.connect breaks all subsequent sleeps in this process" — it's narrower.

### Isolated L3 (5 fresh process runs, no prior scenarios)

```
=== run 1 ===  connected  sleep(550ms) elapsed=0
=== run 2 ===  connected  sleep(550ms) elapsed=0
=== run 3 ===  connected  sleep(550ms) elapsed=0
=== run 4 ===  connected  sleep(550ms) elapsed=0
=== run 5 ===  connected  sleep(550ms) elapsed=0
```

5/5 deterministic 0ms. Pristine reproduction.

### One process, before/after rpc.connect

```
baseline sleep(50ms)            elapsed=50     OK
connected
[after connect] sleep(550ms)    elapsed=0      ✗ trigger
[after close]   sleep(550ms)    elapsed=550    OK (close repaired it)
[after close x2] sleep(550ms)   elapsed=551    OK
```

This is the cleanest signal. The poisoning is held by the **open** connection state. `rpc.close` (which sends COM_QUIT and calls `stream.close`) restores normal sleep behavior. The bug isn't a one-way reactor corruption — it's a "while this fd/registration is alive, sleep short-circuits" state.

## Where the trigger lives

Between L2q and L3:

- L2q does `wire.connect(opts)` (TCP + handshake + auth, native-password) + one `wire.query("SELECT 1")` + drain. **Sleep works.**
- L3 does `wire.connect(opts)` + `_exec_expect_ok("SET NAMES <charset> COLLATE <collation>")` + `wire.set_autocommit(false)`. **Sleep broken.**

The L3 path is `rpc.connect()` from `packages/mariadb-rpc/src/lib.drift:401`:

```
match wire.connect(&opts) {
    core.Result::Ok(v) => {
        var session = move v;
        var conn = RpcConnection(wire_session = move session, strict_reuse = config.strict_reuse);
        val set_names_sql = "SET NAMES " + config.charset + " COLLATE " + config.collation;
        match _exec_expect_ok(&mut conn, &set_names_sql) { ... }
        match wire.set_autocommit(&mut conn.wire_session, config.autocommit) { ... }
    }
}
```

So L3 adds, after `wire.connect`:
1. **Two extra COM_QUERY-and-drain round trips** (SET NAMES, SET autocommit). L2q does only one and is fine, so it's likely not "any query at all" — it's something about doing **multiple** consecutive queries on the same session, or a specific protocol path the second query exercises.
2. `wire.set_autocommit` may take a different code path than `wire.query` (separate function in wire-proto). Worth a glance.

## Hypothesis

The leading guess (mine, easy to be wrong): one of the reads inside the post-handshake query path leaves the socket fd registered in the reactor as "ready to read" (or with a pending epoll event). The next `conc.sleep` registers its timer, calls `epoll_wait`, and the reactor's loop returns immediately because the still-armed fd readiness event is the first thing in the ready set. The timer never gets a chance to actually fire.

`rpc.close` repairs this because closing the fd removes it from the epoll set, so the next `conc.sleep`'s `epoll_wait` has nothing in the ready set and parks until the timer.

This is consistent with the 0.31.86 fix description ("stale wake-token") — same shape, different trigger. The first fix removed one source; this is a second source on the multi-query-on-one-session path.

## Why earlier scenarios "fix" L3

Speculation: each L0–L2q scenario opens a socket, does work, then closes. Each close call presumably cleans up the reactor state. By the time L3 runs, the runtime has been through enough register/unregister cycles that whatever state primes the bug is in a non-broken arrangement. Running L3 alone exposes the bug because the L3 trigger is reached without those prior settle cycles.

## What I'd suggest for instrumentation

To bisect on your side:

1. Reproduce with `reduce_sleep_l3_only_test.drift` (deterministic 5/5).
2. Strip L3's `_exec_expect_ok` (remove `SET NAMES` and `SET autocommit`) and see if sleep works → confirms it's the post-handshake queries.
3. Strip just one of the two queries → narrows to a single query that triggers it.
4. If still triggers with one query, replace `wire.query` with raw `wire.session_write_packet` + `wire.session_read_packet` cycles to see if it's specifically the high-level query path or any consecutive read/write.

## Status of v1

- mariadb.rpc.managed code is structurally complete and the lifecycle path passes.
- The keepalive path is correct in structure — the VT IS spawning and IS calling `conc.sleep(100ms)` in a loop. But `conc.sleep` inside the VT presumably hits the same bug (its registration via the same reactor short-circuits because of the still-open conn's fd), so the loop's "sleep" returns instantly and the loop runs flat-out until the cancel fires.

Once this is fixed, the keepalive scenario should work without code changes on our side.

— SL
