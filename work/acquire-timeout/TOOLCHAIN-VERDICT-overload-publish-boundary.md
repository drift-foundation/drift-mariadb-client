# Toolchain verdict: FIXED in 0.33.14 — keep the no-arg `acquire()` idiom

**Re:** "concrete-type method overload does not survive the published-package
boundary" (was blocking `drift deploy` → stress + perf on 0.33.13)

**Status: FIXED in toolchain 0.33.14.** mariadb-rpc was using the documented,
supported idiom correctly. **Keep the no-arg `acquire()` overload — do not
rename it.** If you applied a temporary rename workaround (`acquire_default()`
/ `acquire_now()`), revert it. Rebuild `drift deploy` once on 0.33.14 and the
baseline-smoke recompile resolves.

## What was wrong (and why `just test` passed but `drift deploy` didn't)

Your idiom — canonical method on the interface, no-arg sugar on the concrete
type:

```drift
pub interface Source { fn acquire(self: &Self, wait: Wait) nothrow -> Int; }
implement Source for Pool { pub fn acquire(self: &Pool, wait: Wait) ... }  // canonical (1-arg)
implement Pool          { pub fn acquire(self: &Pool) { return self.acquire(Wait::UseDefault()); } }  // sugar
```

In a **whole-source** compile (`just test`), the resolver saw both `acquire`
overloads and the no-arg body's `self.acquire(wait)` resolved fine. In a
**consumer compiled against the published `.dmp`** (`drift deploy` baseline
smoke), the interface-impl method's identity is tagged by the trait-impl index,
so the method resolver routed it into a separate "trait candidate" bucket — and
the inherent-method-wins selection then *discarded* it. That left only the no-arg
overload visible, so the 1-arg call inside it failed:
`no matching method 'acquire' for receiver Pool` / `...Ref<Pool>`. The published
package always carried both methods; the defect was purely consume-side overload
resolution.

## The fix (toolchain side)

`call_resolver.resolve_method_call`: interface-impl methods are now treated as
peers of inherent methods for overload resolution — they go into a dedicated
candidate list that is **unioned** with the inherent candidates, so inherent +
interface-impl methods of the same name on a concrete type form one overload set
(matching whole-source). `pub trait` method scoping is unchanged. Pure
consume-side resolver logic — **no ABI change** (ABI stays 15; DRIFTC
0.33.13 → 0.33.14).

## Validation

- New regression `test_pkg_iface_inherent_overload_merge.py`: your idiom AND a
  plain same-block overload control each emit-package → consume-against-published
  → link → **run to exit 23** (proves the *correct* overload resolves, not just
  that compilation stopped erroring). 2 passed.
- type_checker suite 147 passed; interface/trait dispatch no-regression batch 27
  passed, 0 leaks (cross-pkg interface metadata, borrowed-interface dispatch,
  trait-impl target type, cross-package method param, require-interface-impl,
  arc-interface get-dispatch) — confirms dynamic dispatch and `pub trait`
  scoping are intact.

## Action for mariadb-rpc

1. Pick up toolchain 0.33.14.
2. Revert any `acquire()` rename workaround — keep the no-arg convenience overload.
3. Re-run `drift deploy`; stress + perf unblock.

The acquire-deadline feature itself was never implicated — this was solely the
no-arg convenience overload at the publish boundary. The `effective-drift.md`
"Interfaces can't overload — canonical method plus concrete-type sugar" idiom is
now publish-safe.

— toolchain team (0.33.14; fix + regression + no-regression validation as above)
