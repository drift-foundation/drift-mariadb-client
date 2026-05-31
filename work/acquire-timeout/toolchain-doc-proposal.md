# Proposed addition to `effective-drift.md`

**To:** toolchain team
**Why:** while building `mariadb-rpc`'s `ConnectionSource.acquire` we hit the
"interfaces can't overload" rule and worked out the idiomatic way around it. The
existing **"Method overload resolution by parameter type"** section (which is
great) only covers concrete types and doesn't mention that interface
declarations *can't* overload, nor the canonical-method-+-concrete-sugar pattern
that follows from it. We probed the behavior to confirm it; suggest adding the
subsection below right after that section. Drop-in markdown follows.

Confirmed on certified `drift 0.33.9 / abi 14`:
- `implement` blocks on a concrete type may overload a method by arity/param type (already documented).
- An **interface** declaring the same method name twice → `error: duplicate method '<name>' in interface '<Iface>'`.
- A no-arg overload in `implement Type { ... }` coexists with the interface method from `implement Iface for Type { ... }`, and `self.method(args)` resolves across both.

---

### Interfaces can't overload — canonical method on the interface, sugar on the type

Overloading (above) works for methods declared in `implement` blocks. It does
**not** work for `interface` declarations — two methods with the same name in
one interface is a hard error:

```drift
pub interface Source {
    fn acquire(self: &Self) nothrow -> Int;
    fn acquire(self: &Self, wait: Wait) nothrow -> Int;   // error: duplicate method 'acquire' in interface 'Source'
}
```

When you want both a low-typing default form and a parameterized form behind one
name, put the **single canonical method on the interface** and add the
convenience **overload on each concrete type** (concrete `implement` blocks
*can* overload, and may delegate to the interface method):

```drift
pub variant Wait { UseDefault, Forever, For(ms: Int) }

pub interface Source {
    fn acquire(self: &Self, wait: Wait) nothrow -> Int;   // the one canonical form
}

implement Source for Pool {
    pub fn acquire(self: &Pool, wait: Wait) nothrow -> Int { /* ... */ }
}

// Concrete-only convenience overload — not on the interface:
implement Pool {
    pub fn acquire(self: &Pool) nothrow -> Int { return self.acquire(Wait::UseDefault()); }
}

val p = Pool(...);
p.acquire();                 // concrete no-arg sugar
p.acquire(Wait::For(ms = 5)) // canonical form (also reachable via a `Source`-typed value)
```

Trade-off to call out: the no-arg sugar lives on the concrete type, so a value
typed as the **interface** must use the canonical form
(`src.acquire(Wait::UseDefault())`). That is usually fine — callers typically
hold the concrete type they constructed.

Design note: prefer a **variant parameter** over a pile of overloads when the
forms are semantically distinct modes (here: use-default / forever / finite).
The variant is exhaustively matchable, names each mode (no sentinel values like
`0 = forever`), and expresses cases arity-overloading can't. Overloading then
only buys the no-arg spelling — keep it as thin concrete sugar, not the contract.

> If default parameter values or default (provided) interface-method bodies are
> ever added to the language, this idiom collapses into a one-liner — but the
> variant-parameter design stays the right call regardless.
