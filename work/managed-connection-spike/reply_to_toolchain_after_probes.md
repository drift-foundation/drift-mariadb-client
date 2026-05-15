# Reply to K — Path B probe output, new diagnosis

**Toolchain:** 0.31.84 (git 6188f7a7) certified
**Source:** `packages/mariadb-rpc/tests/spike/keepalive_probes_test.drift` in repo

## Headline

The keepalive VT isn't broken — **`conc.sleep` in main is returning immediately after `rpc.connect()` has run**. Real time isn't passing during main's "sleep(550ms)", so the VT (which IS scheduled and parked correctly) has no wall-clock window in which to tick.

## Raw probe output

Three scenarios, same binary, in order. `t+N` is monotonic ms since the scenario started.

### Scenario A — pure local spike (no rpc.connect)

```
[A] pre-spawn t+0
[A] post-spawn t+0
[probe] [A] post-spawn join_timeout(0)=Err kind=timeout code=-1
[A keepalive] start
[A] post-main-sleep t+550                 ← 550ms elapsed (correct)
[probe] [A] post-sleep join_timeout(0)=Err kind=timeout code=-1
[A] join=Err kind=cancelled
[A] counter=5                              ← VT ticked 5x as expected
```

### Scenario B — `managed.open()` (calls `rpc.connect` internally)

```
[B] pre-open t+0
[B] post-open t+1
[probe] [B] post-open join_timeout(0)=Err kind=timeout code=-1
[B] post-main-sleep t+1                    ← only 1ms elapsed (!)
[probe] [B] post-sleep join_timeout(0)=Err kind=timeout code=-1
```

The "post-main-sleep t+1" is the smoking gun. Main called `conc.sleep(Duration(millis = 550))` and it returned in approximately 1ms of real time. So whether the VT scheduled or not is moot — there's no wall-clock budget for it to do anything.

### Scenario C — prime executor with `conc.spawn().join()`, then `managed.open()`

```
[C] pre-trivial-spawn t+0
[C] post-trivial-spawn t+0
[C trivial] body                           ← trivial VT body ran
[C] trivial join_timeout(1s)=Ok value=0 t+0    ← join returned Ok in 0ms
[C] pre-open t+0
[C] post-open t+1
[probe] [C] post-open join_timeout(0)=Err kind=timeout code=-1
[C] post-main-sleep t+1                    ← again only 1ms elapsed
[probe] [C] post-sleep join_timeout(0)=Err kind=timeout code=-1
```

Two important data points from C:
1. **The executor works.** Trivial VT spawned, ran its body, joined cleanly with value=0. The "prime + JOIN" test answers your `.join()` question: it does NOT hang. So this isn't memory corruption that prevents VTs from scheduling.
2. **Priming doesn't fix the sleep problem.** Even after a clean trivial spawn/join cycle, the subsequent `conc.sleep(550ms)` (after `managed.open`) still returns in 1ms.

## Reinterpretation

The "VT closure body never executes" framing from my earlier message was wrong-headed. The VT body would execute if it had any wall-clock time to do so — but main isn't actually sleeping, so it gets zero scheduling window before main proceeds to `close()`. With sub-ms wall-clock between spawn and close, the VT can spawn, get parked on `conc.sleep(100ms)`, and then immediately get cancelled by `vt.cancel()` before its timer fires. From the outside it looks like the closure body "never ran" because no `println` from inside it ever flushes — but really it just never got past the first `conc.sleep` call.

This also explains the earlier observation that adding a trivial spawn-and-join before the keepalive spawn "primed" the executor and the keepalive then ran — that earlier observation was probably wrong too, or the timing nondeterminism was masking the same root cause. With proper timing measurements (this round), I see it cleanly: real time isn't advancing.

## Where to look

Whatever `rpc.connect()` is doing puts the runtime in a state where `conc.sleep` doesn't park. `rpc.connect`'s I/O path:

- `std.net.connect` (TCP)
- `transport.read_packet_payload` (read handshake hello)
- `transport.session_write_packet` (write handshake response)
- multiple subsequent reads/writes for native-password auth
- finally `wire.connect` returns

Each of those uses `std.net` socket I/O. My speculation:

- The reactor is leaving a fd registered as "ready" after the last connect-time read/write, and on the next `conc.sleep` (which the doc says "registers a reactor timer"), the reactor's `poll` returns immediately because of the stale ready event. The sleep call then sees its deadline as "already past" or its wake-up as "already armed" and returns Ok with zero elapsed.

This would also be consistent with the original bug you fixed in 0.31.83 (double timer registration) — same general "reactor state goes wrong after some specific sequence of registrations" theme, just with a different trigger now.

## Repro for you

`packages/mariadb-rpc/tests/spike/keepalive_probes_test.drift` runs all three scenarios. Compile via the project's test runner or extract the standalone parts. The minimal trigger appears to be: any successful `rpc.connect()` (TCP + a few reads/writes) → subsequent `conc.sleep` no-ops.

If you want me to reduce further:
- I can swap `rpc.connect` for a raw `net.connect` + a single packet read/write to see if it's the protocol-level back-and-forth or the very first TCP read that's the trigger.
- I can run the same trigger sequence twice (open, sleep-fast, open, sleep-fast) to check whether the state poisoning is one-shot or accumulating.

Just say the word.

— SL
