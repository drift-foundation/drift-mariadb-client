# PLAN — `poll_many` keepalive + abi18 alignment

Status: DRAFT (in progress). Owner: sl. Started 2026-06-21.

## 1. Drivers

The toolchain is certifying `driftc 0.33.49 | abi 18`
(`/home/sl/opt/drift/staged/toolchain/drift-0.33.49+abi18`). We are out of
alignment and must close the gap in this release. Bundled into the same release,
per explicit direction:

1. **abi18 alignment** — build/cert against the new toolchain.
2. **`poll_many` adoption** — collapse the per-connection keepalive virtual
   threads onto a single `std.io.poll_many`-driven service fiber, per the
   toolchain's `poll_many` migration guidance (the `web-rest` adopter pattern).
3. **Fix `keepalive-vt-not-scheduled`** — the open concurrency bug (spike tests
   under `packages/mariadb-rpc/tests/spike/`). The single-fiber model is the
   candidate resolution: it removes the per-VT proliferation that the bug rides
   on, rather than masking it.

## 2. Findings (build verification against abi18)

Verified before writing code (do not re-derive):

- `drift build mariadb-wire-proto` against abi18 → **EXIT 0, clean.**
  wire-proto imports `std.io`, `std.net`, `std.concurrent` — the stdlib surface
  we use is intact under abi18.
- `drift build mariadb-rpc --package-root <abs>/build/deploy` → **fails** with
  `variant schema collision for 'std.concurrent:ProcessSignal'`. Root cause: the
  cached `mariadb-wire-proto` 0.4.0 artifact in `build/deploy` was built on the
  **old** ABI; its embedded `std.concurrent` schema collides with abi18's. This
  is a **stale-artifact ABI mismatch, NOT a source break.**
- **Conclusion: our source is abi18-clean.** Alignment is mechanical: rebuild
  the whole graph (wire-proto → rpc) under abi18, re-deploy, re-cert, reseal.
- `drift/lock.json` is already `schema_version: 4` (abi18-compatible). Deps are
  co-artifacts only (no external pkgs needing republish for abi18).

## 3. `poll_many` API (abi18 `std.io`)

```
poll_many(entries: &Array<PollEntry>, timeout: conc.Duration)
    nothrow -> core.Result<Array<PollReady>, IoError>

struct PollEntry  { fd: Int, token: Int, want_read: Bool, want_write: Bool }
struct PollReady  { fd: Int, token: Int, readable: Bool, writable: Bool,
                    hangup: Bool, err: Bool }
```

- fds from `TcpStream.raw_fd(&self) -> Int` (confirmed present in abi18).
- `timeout.millis > 0` bounds the wait; `<= 0` parks until ready.
- `Err(kind="timeout")` on elapse; `Err(kind="cancelled")` on VT cancel;
  `Err(kind="invalid-argument")` on **empty list**, zero-interest entry, or
  unregistrable fd; `Err(kind="requires_vthread")` off the VT runtime.
- Readiness is **EDGE-triggered**. Precedence: cancellation > readiness > timeout.
- `hangup`/`err` are **sticky terminal** — close the fd once seen.

## 4. Plumbing: raw_fd up the stack

Path is clean (all `pub`):
`net.TcpStream` → `wire.WireSession.stream` → `rpc.RpcConnection.wire_session`.

Add accessors (thin, no behavior):
- wire-proto `transport.drift`: `pub fn session_raw_fd(s: &WireSession) -> Int`
  returning `s.stream.raw_fd()`.
- rpc `lib.drift`: `pub fn raw_fd(conn: &RpcConnection) -> Int` (+ method form)
  returning `transport.session_raw_fd(&conn.wire_session)`.

This keeps the pool/managed layer from reaching through two struct boundaries
into `std.net`.

## 5. Single-fiber keepalive model

### 5.1 Today

Both `managed.drift` and `pool.drift` spawn **one keepalive VT per source**,
parked on `conc.sleep(interval)`. On each tick: try-acquire an idle conn, ping,
reap aged idles. The pool can hold N idle conns but watches none of their fds —
a server-initiated close on an idle conn is invisible until the next ping tick.

### 5.2 Target

**One service fiber per source**, built around `poll_many`:

```
loop while not stop_flag:
    # snapshot idle conns under the slot lock (no I/O under lock)
    entries, ids = build PollEntry[] for each idle conn:
        fd = rpc.raw_fd(conn), token = conn.id, want_read = true, want_write = false
    drop lock

    if entries empty:
        conc.sleep(min(interval, IDLE_WATCH_CAP))   # nothing to watch
        -> on wake: run keepalive_tick (ping due + idle reap)
        continue

    timeout = min(remaining keepalive interval, IDLE_WATCH_CAP)
    match poll_many(&entries, conc.Duration(millis = timeout)):
        Err(IO_ERROR_KIND_CANCELLED)        -> exit     # close() cancelled us
        Err(IO_ERROR_KIND_TIMEOUT)          -> (no ready; tick below if due)
        Err(IO_ERROR_KIND_INVALID_ARGUMENT) -> resnapshot immediately (continue);
            # an fd was raced out / closed under us — the next snapshot drops it
            # naturally. Count consecutive occurrences; only if they exceed
            # SPIN_GUARD in a row insert a tiny sleep to avoid a hot loop.
        Err(other)                          -> count + brief backoff (unexpected)
        Ok(ready):
            recycle_ready_idles(tokens where readable|hangup|err)   # see 5.3
    if elapsed(last_tick) >= interval: keepalive_tick (ping due + idle reap); last_tick = now
```

`keepalive_tick` runs on the **interval deadline**, decoupled from poll wakeups,
so a busy recycle stream can't starve pings and a sub-interval `IDLE_WATCH_CAP`
poll cycle doesn't over-ping.

An idle pooled conn that becomes **readable / hangup / err is misbehaving**
(server spoke first, or closed): we do not drain — we **recycle** it (close +
decrement + signal a waiter so a replacement can open). This avoids importing
the full edge-drain read loop into the keepalive path; drain semantics only
matter for streams we intend to keep reading, which idle pooled conns are not.

### 5.3 THE key design problem: watch/lease race

The slot lock is dropped across `poll_many`. While we wait, a user `acquire()`
can pop a watched conn out of `available` and start real request/response I/O on
its fd. We must never become a **second reader** on a leased fd.

Resolution (chosen): **generation-token exclusive recycle.**
- `ConnEntry` carries a `watch_token: Int` assigned **fresh from a monotonic
  `PoolSlot.next_watch_token` counter every time the entry ENTERS `available`**
  (open-seed and release both mint a new token under the lock). A token
  therefore identifies *this idle incarnation*, not the physical conn.
  `PollEntry.token = watch_token`.
- On readiness for `token = T`, re-lock and **recycle the entry whose
  `watch_token == T` iff it is still in `available`** (drain-and-rebuild the
  deque, pulling matches out — `Deque` has no middle-removal):
  - Found → still idle, now **exclusively ours** → recycle (close outside the
    lock, `total_count--`, `cv.signal_one()`).
  - Not found → it was leased (then maybe returned with a NEW token) or reaped
    in the race window → **do nothing**.
- Why a per-incarnation token, not a stable id: a conn leased → used cleanly →
  returned **still idle** re-enters `available` with a *new* token. A stale
  readable edge from its previous incarnation carries the *old* token, which no
  longer matches anything in `available` → **no spurious recycle.** This closes
  the churn window entirely (per plan review #1), which matters because MariaDB
  reconnects are expensive and the churn would be load-dependent.
- Because we only ever read/close a conn pulled out under the lock, the
  double-reader hazard on a leased fd is structurally impossible.

### 5.4 Empty idle-set & newly-idle staleness

- `poll_many` errors on an empty list, so when no conns are idle we fall back to
  `conc.sleep(min(interval, IDLE_WATCH_CAP))` and then run a normal tick.
- A conn that becomes idle while we're parked in `poll_many` is not watched
  until the next snapshot. We cap the poll timeout at `IDLE_WATCH_CAP`
  (proposed 1000 ms) so staleness is bounded without a self-pipe/eventfd wake.
  An eventfd-style self-wake is a later optimization (Open Questions).

### 5.5 managed vs pool

- **pool.drift** is the real beneficiary (N idle fds) — primary target.
- **managed.drift** is single-slot; `poll_many` over one fd is degenerate but we
  apply the **same single-fiber mechanism** for (a) consistency and (b) the
  `keepalive-vt-not-scheduled` fix, which the spike tests exercise via
  `managed.open()`.
- Factor the fiber loop into a small shared shape if it falls out cleanly;
  otherwise duplicate the ~30-line loop rather than over-abstracting across two
  different slot models (Mutex<ManagedSlot> vs Mutex<PoolSlot>).

## 6. `keepalive-vt-not-scheduled` — verify FIRST

Risk: the bug may be "a spawned VT never schedules." If even **one** VT fails to
schedule under abi18, `poll_many`-on-a-VT does **not** help (it also runs on a
VT). Before committing the rewrite as the bug fix, **re-run the spike probes
against abi18**:

- `packages/mariadb-rpc/tests/spike/keepalive_probes_test.drift` (scenarios A/B/C).
- If abi18 **already fixes** scheduling → the rewrite's bug-fix rationale is moot
  but consolidation still stands on its own (pool fd-watch). Record and proceed.
- If it **still reproduces** → determine whether it's per-VT-count or
  any-VT. If any-VT, STOP and escalate to toolchain per the abort-on-spec/impl-gap
  policy; do not ship a rewrite that can't actually run.

## 7. Step plan

- [ ] S0. Repoint toolchain to staged abi18 for the dev loop; rebuild graph
      (wire-proto then rpc) under abi18 to reproduce a clean build.
- [ ] S1. Re-run keepalive spike probes against abi18 (§6). Gate decision.
- [ ] S2. Plumb `raw_fd` (wire-proto `session_raw_fd`, rpc `raw_fd`). Build.
- [ ] S3. pool.drift: add `ConnEntry.watch_token` + `PoolSlot.next_watch_token`
      (fresh token on every enter-available); snapshot helper → `Array<PollEntry>`;
      `recycle_ready_idles(tokens)` drain-and-rebuild by exact token.
- [ ] S4. pool.drift: replace `_keepalive_loop` body with the poll_many service
      loop (§5.2); keep `_keepalive_tick` (ping + reap) as the timeout action.
- [ ] S5. managed.drift: same single-fiber loop over its one slot.
- [ ] S6. Tests: unit (no DB) for snapshot/recycle/empty-set; live tests for
      idle-close detection + keepalive + reap. Add a regression that an idle
      conn closed server-side is recycled within `IDLE_WATCH_CAP + interval`.
- [ ] S7. Full cert gates against abi18: `just test` (plain+ASAN+memcheck),
      `just stress`, `just perf`. DB `mdb114-a` up.
- [ ] S8. reseal (author-claim + prepare + trust-check); bump versions; deploy.

## 8. Open questions / risks

- **OQ1 (gate):** CLOSED — VT-scheduling bug does NOT repro on abi18 (S1 green).
- **OQ2:** `IDLE_WATCH_CAP` value — 1000 ms proposed. Trades idle-close detection
  latency vs poll wakeups. Confirm against perf gate.
- **OQ3:** CLOSED — adopt generation-token (`watch_token`) per plan review #1;
  the benign-race compromise is dropped (§5.3).
- **OQ4:** self-wake (eventfd/self-pipe) for newly-idle conns instead of the cap
  — defer unless idle-close latency matters to a consumer.
- **OQ5 (plan review #3): DECIDED 2026-06-21 — keep `min_idle` as a SEED-TIME
  floor for this release.** It matches the pre-existing ping-failure discard
  behavior and is documented in code (`_recycle_ready_idles`). A maintained LIVE
  floor (background refill in recycle + `_release_decide`) is a separate behavior
  change with reconnect/backoff/load implications — deferred as a FOLLOW-UP, to be
  done only if teams actually need warm idle-capacity guarantees. No code change
  for this release.
- **OQ5:** version bumps — wire-proto (raw_fd accessor is additive → minor) and
  rpc (keepalive internals + raw_fd → minor). Confirm at reseal.
- **R1:** cert gates need the orchestrator for evidence-bearing cert-claims;
  local `just stress`/`perf` use the dev no-evidence sentinel. Release
  certification stays an orchestrator-side flow.

## 9. Definition of done

- Clean abi18 build of both artifacts; cert gates green.
- One service fiber per source; zero per-conn keepalive VTs.
- Idle conn closed server-side is detected & recycled (regression test).
- `keepalive-vt-not-scheduled` spike: resolved or formally escalated (S1).
- reseal + version bumps committed by the user (git stays read-only here).
