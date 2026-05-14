# ManagedConnection spike — findings (resolved)

**Date:** 2026-05-14
**Toolchain:** drift-0.31.76+abi14 (git e4f5d42d)
**Status:** Spike scenarios pass end-to-end on live MariaDB (mdb114-a). Lease + slot + RAII model proven. One compiler bug isolated with a viable workaround.

## What the spike proved

`packages/mariadb-rpc/tests/spike/managed_connection_spike.drift` runs against the live test DB and exercises:

- **Open / close lifecycle** (`scenario_open_close`).
- **Acquire → use → RAII release → re-acquire** (`scenario_acquire_use_release`). The second acquire's `ping` succeeds, which is the structural proof that `LeasedConn::destroy` returned the conn to the slot.

Final scenario output:
```
=== spike: ManagedConnection (minimal) ===
[s1] opened
[s1] closed
[s2] ping ok via lease #1
[s2] ping ok via lease #2 (slot restored)
=== spike: minimal scenarios passed ===
```

That confirms:
- `Arc<Mutex<SlotCell>>` shape compiles and runs.
- Brief lock holds at acquire/release (mutex protects the storage slot, not the lease duration).
- `LeasedConn` owns the conn during the lease; `Destructible::destroy` returns it.
- No explicit `release()` API — only `destroy` via RAII.

## Compiler bug discovered: `mem.replace` rejects named `&mut T` bindings

Minimal repro files in this directory:

| File | Pattern | Result |
|---|---|---|
| `lang_bug_repro_optional_replace.drift` | Combined A+B in one file | B fails |
| `/tmp/repro_case_a.drift` | `&mut box.x` inline at call site | **compiles & runs (exit 42)** |
| `/tmp/repro_case_b.drift` | `mem.replace<...>(guard.get_mut(), ...)` directly | fails |
| `/tmp/repro_case_c.drift` | Helper with `slot_mut: &mut Optional<T>` parameter | fails |
| `/tmp/repro_case_d.drift` | Same as C but no explicit type args on `mem.replace` | fails |
| `/tmp/repro_case_e.drift` | Local `val slot_mut: &mut Optional<T> = &mut b.x` | fails |
| `/tmp/repro_case_f.drift` | `Mutex<SlotCell { value: Optional<T> }>` + `&mut cell.value` inline | **compiles & runs (exit 88)** |

**Pattern:** `mem.replace` accepts the first argument only when it's a **fresh `&mut <field>` projection at the call site**. Any named binding — parameter `slot: &mut Optional<T>` or local `val slot_mut: &mut Optional<T>` — fails with `error: replace expects &mut T as the first argument [E-AUTO-9370445a]`.

Error appears whether or not `mem.replace<type Optional<T> >(...)` carries explicit type arguments, so it's not an inference-only issue — the validator literally rejects the named `&mut T`.

## Workaround in use: SlotCell wrapper

To make `&mut <field>` projection available at the `mem.replace` call site, wrap `Optional<T>` in a thin struct:

```drift
pub struct SlotCell {
    pub value: Optional<rpc.RpcConnection>
}

pub type Slot = conc.Arc<conc.Mutex<SlotCell> >;
```

Then at the call site (e.g. inside `acquire`):

```drift
val cell: &mut SlotCell = guard.get_mut();
val taken = mem.replace(&mut cell.value, _none_conn());  // inline projection — works
```

Zero runtime overhead (SlotCell is a one-field struct, layout-identical to `Optional<T>` modulo any padding). The workaround stays in place until the compiler bug is fixed.

## Reproducible bug report (ready to send)

> **Repro:** `/tmp/repro_case_b.drift` (failing) and `/tmp/repro_case_a.drift` (working) on toolchain `drift-0.31.76+abi14` (git `e4f5d42d`).
>
> **Pattern:** `mem.replace` rejects `&mut T` from a named binding (parameter or local) but accepts the same `&mut T` from an inline `&mut <field>` projection. Specifically:
> ```drift
> // Compiles:
> fn ok(box: &mut Box) -> Optional<R> {
>     return mem.replace(&mut box.x, _none());           // &mut box.x is the first arg
> }
> // Fails E-AUTO-9370445a:
> fn bad(slot: &mut Optional<R>) -> Optional<R> {
>     return mem.replace(slot, _none());                 // slot is the first arg
> }
> ```
> Same error appears when the `&mut T` comes from `MutexGuard::get_mut()`. Explicit type arguments (`mem.replace<type Optional<R> >(...)`) don't change the outcome.
>
> Likely diagnostic: the binder / inference for generic intrinsic `mem.replace` treats a named `&mut T` differently from an inline projection, even though both produce the same `&mut T` at the type level. Effect is that `mem.replace` is unusable on any `&mut T` returned from a `&mut self`-method (notably `MutexGuard::get_mut`).
>
> Both repro files compile standalone with: `driftc --stdlib-root <stdlib> --entry <module>::main -o <out> --target-word-bits 64 <file>.drift`.

## What's NOT in the spike yet — and why that's OK

- **Keepalive thread** (background `conc.spawn`'d loop that pings the slot's conn periodically and reconnects on failure). Not in the spike because (a) the structural pattern — Arc<Mutex<SlotCell>> shared with a captures-style closure — is well-trodden in `effective-drift.md` and not the risky part; (b) deferring to the real implementation avoids burning more spike time on a piece that's clearly buildable. The v1 implementation needs to include it; the spike is here to derisk the bits that aren't obviously expressible.
- **Reconnect on ping failure** (drop dead conn, `rpc.connect(config)` to re-resolve DNS). Same reasoning — `rpc.connect` already works; calling it from a keepalive loop is straightforward once the loop exists.
- **Event sink for observability** (`ManagedEvent::ReconnectAttempted`, etc.). Pure additive surface; doesn't depend on any unknown.

## Next step

Send the bug report above to the toolchain team and proceed to draft the v1 `mariadb.rpc.managed` module. The spike has proven the load-bearing parts of the design; the rest is straightforward composition with `effective-drift` patterns.
