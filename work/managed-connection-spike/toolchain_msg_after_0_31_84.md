# Message to Drift toolchain team

**From:** mariadb-rpc maintainer
**Re:** 0.31.83+ verification — keepalive sleep fix confirmed in isolation; new regression observed; minor UX flaws; Issue 2 (captures-share + later move SSA) repro update
**Toolchain tested:** 0.31.84 (git 6188f7a7) certified

Thank you for the fast turnaround on the `std.concurrent.sleep` double-registration fix. Verification + new findings below.

---

## 1. Sleep parking fix — confirmed working in isolation

Standalone repro at `/tmp/repro_keepalive_vt.drift` (also vendored at
`packages/mariadb-rpc/tests/spike/repro_keepalive_with_rpc_import_test.drift`):

- Spawns a VT that loops `conc.sleep(Duration(millis = 100))` + `Mutex<Counter>` increment.
- Main calls `conc.sleep(Duration(millis = 550))`, then signals stop and joins.
- **5 of 5 runs:** `seen=5`. Exactly the expected 4-7 range. Spin-loop mode and no-schedule mode are gone in isolation.

Repro variants vendored under `packages/mariadb-rpc/tests/spike/`:
- `repro_keepalive_with_rpc_import_test.drift` — same pattern + `import mariadb.rpc as rpc`. Also `seen=5`. So the rpc import alone doesn't disturb the runtime.
- `repro_keepalive_arc_struct_test.drift` — same pattern but the slot+stop are both fields inside one `Arc<Inner>` struct (matches the production layout). Also `seen=5`.

In short: pure VT-sleep behavior matches the expected contract on 0.31.84. Thank you.

## 2. New issue: keepalive VT not scheduled when spawned from a helper inside the library

`packages/mariadb-rpc/src/managed.drift::open()` spawns a keepalive VT via a helper `_spawn_keepalive(inner_arc.clone())` and returns the VT handle inside a `ManagedConnection` struct to the caller. The caller (`live_managed_smoke_test.drift::scenario_observed_keepalive`) then calls `conc.sleep(550ms)` and observes events through a `Callback1` sink installed in the config.

**Observed on 0.31.84:**
- Test reports `observed pings: 0` (5 of 5 runs).
- Inserting `console.println` at the very top of `_keepalive_loop` confirms the loop body **never executes** — closure on the spawned VT is never invoked.
- All other code in `open()` runs to completion (`rpc.connect` succeeds, the local debug prints around `conc.spawn` fire, the test's own debug prints fire, etc.).

**Triangulation pieces (all in the same test binary, same source file):**

| Test variant | What it does | Result |
|---|---|---|
| Local `_local_spawn` of `_local_loop` from main, VT handle in local | Counter increment loop | `seen=5` ✓ |
| Local `_local_spawn` of `_local_loop`, VT handle moved into a struct (`SpikeHolder`) returned from a helper | Same loop, struct-wrapped handle | `seen=5` ✓ |
| `managed.open()` → `_spawn_keepalive` → VT moved into `ManagedConnection.keepalive_vt` | Real keepalive loop | **closure never runs** ✗ |
| Same as above, **but** I added a trivial no-capture `conc.spawn(...) ; trivial_vt.join()` inside `_spawn_keepalive` BEFORE the keepalive spawn | Real keepalive loop after primed executor | closure **does** run, then segfaults a few iterations in |

The "trivial VT spawn-and-join before the keepalive spawn unblocks the keepalive VT" datum is the strongest clue — it smells like the first `conc.spawn` from a given call site has different scheduling behavior than subsequent ones, or the executor is in a deferred-initialization state until something forces a flush.

**Source file in the repo for inspection:** `packages/mariadb-rpc/src/managed.drift::open` and `::_spawn_keepalive` (the second is two lines). The test triggering the failure is `packages/mariadb-rpc/tests/e2e/live_managed_smoke_test.drift::scenario_observed_keepalive` (currently disabled in the file — needs to be re-added to the `main()` chain to reproduce; the scenario function is preserved in git history of the working tree if you need the exact shape).

I can produce a standalone repro outside the package if helpful — say the word and I'll extract it.

## 3. Issue 2 (captures-share + later move SSA crash) — not reproduced from reduced cases

You asked for a standalone minimal repro. I tried — `/tmp/repro_closure_capture_move.drift` and variants — and on 0.31.84 the basic patterns compile cleanly. So Issue 2 either:

(a) was incidental to 0.31.81 and has been quietly fixed alongside the sleep work; or
(b) requires the full surrounding context (Arc<Struct-with-many-fields> + later move in same function + the conditional `if should_keepalive { ... }` block I had originally).

I'll leave the workaround in place (explicit `clone()` + `captures(move <clone>)`, plus `| |` with the inner-bar space rather than `||`) — that pattern compiles on every toolchain we've tried, so it's a safe forward path even if the underlying bug is fixed.

If you do want to dig further, the original failing shape was the closure construction inside `open()` followed by `return ManagedConnection(inner = move inner_arc, ...)`. The git history of `packages/mariadb-rpc/src/managed.drift` between `_spawn_keepalive` introduction and the final form has the failing intermediate states.

## 4. Issue 1 (arc.get().field on non-Copy fields) — workaround still in place

Confirmed: still need the `val r: &Inner = arc.get(); &r.field` two-step. Acceptable for now.

## 5. Minor doc / UX flaws worth fixing whenever a stdlib doc pass happens

- **`console.println` doesn't actually require `val _ = console.println(...)`.** Repro at `/tmp/repro_println_void.drift`: `console.println("hello");` as a plain statement compiles cleanly. `doc/stdlib/std_console.md` correctly documents `nothrow -> Void`. But our codebase (and the existing tests at `packages/mariadb-rpc/tests/e2e/live_rpc_smoke_test.drift`) is full of `val _ = console.println(...)` cruft — probably from a long-ago revision when it returned `Result<Void, _>`. Worth flagging in a doc example or migration note so consumers don't keep cargoing the unnecessary binding. Not a compiler bug.

- **`if cond { a } else { b }` rejected as a function-call argument.** Repro at `/tmp/repro_if_expr.drift`:
   ```drift
   val n = if v { 1 } else { 0 };          // OK — RHS of val binding
   format.format_int(if v { 1 } else { 0 })  // FAILS — same expression in arg position
   ```
   Parser inconsistency: `if` as an expression is allowed in some positions and not others. The semantic story is the same (it's a value-producing expression of type `Int` either way), so the parser should be lifted to accept it uniformly. Specific error: `error: Unexpected token Token('IF', 'if') [E-AUTO-5a5b6c85]`.

## What's working today, end-to-end

- `mariadb.rpc.managed` lifecycle path (open / acquire / RAII LeasedConn / re-acquire / close). Live test passes on 0.31.84 with `keepalive_interval_ms = 0`. See `packages/mariadb-rpc/tests/e2e/live_managed_smoke_test.drift`.
- `mariadb.rpc.managed` is structurally complete (300+ LOC); the keepalive code path compiles fine but is gated behind Issue §2 above to actually execute.

We'll re-test as soon as you can take a look at §2 — it's the only thing standing between this and shipping the v1 release to the downstream Singular/bookkeeper team.

Thanks,
SL
