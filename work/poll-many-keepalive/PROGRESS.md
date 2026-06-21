# PROGRESS — `poll_many` keepalive + abi18 alignment

See [PLAN.md](PLAN.md) for design. This file = running status only.

Toolchain target: `drift 0.33.49 | abi 18`
(`/home/sl/opt/drift/staged/toolchain/drift-0.33.49+abi18`).

## Status board

| Step | What | State |
|------|------|-------|
| S0 | Repoint dev loop to abi18; rebuild graph | ✅ both artifacts deploy clean on abi18; smoke passes |
| S1 | Re-run keepalive spike probes on abi18 (gate) | ✅ GREEN — VT-scheduling bug does NOT reproduce on abi18 |
| S2 | Plumb `raw_fd` (wire-proto + rpc) | ✅ added `session_raw_fd` (wire) + `raw_fd` (rpc), builds clean |
| S3 | pool: watch_token + snapshot + recycle_ready_idles | ✅ compiles on abi18 |
| S4 | pool: poll_many service loop replaces `_keepalive_loop` | ✅ compiles on abi18 |
| S5 | managed: single-fiber loop + token-checked recycle | ✅ compiles on abi18 |
| S6 | Tests (unit + live + idle-close regression) | ✅ added pool_idle_close_recycle + managed_idle_close_recycle (both PASS, poll-path proven); wired into LIVE_TESTS |
| S7 | Cert gates on abi18 (test/stress/perf) | ✅ ALL GREEN at 0.5/0.7: test 200 ok, stress 2 ok, perf 3 ok (no regression; hot path untouched) |
| S8 | reseal + version bumps + deploy | ✅ versions bumped (wire 0.5.0, rpc 0.7.0), reseal done (claims+lock+trust-check); commit pending (user) |

## Log

### 2026-06-21
- Read toolchain `poll_many` migration guidance; cross-checked against code.
- **Build verification (abi18):**
  - `drift build mariadb-wire-proto` → EXIT 0, clean.
  - `drift build mariadb-rpc --package-root <abs>/build/deploy` → fails
    `variant schema collision for 'std.concurrent:ProcessSignal'` = stale
    wire-proto 0.4.0 artifact (old ABI) vs abi18 stdlib. **Not a source break.**
  - Conclusion: source is abi18-clean; alignment = rebuild graph + re-deploy.
- Confirmed `poll_many` API + `TcpStream.raw_fd` present in abi18 stdlib docs.
- Confirmed raw_fd plumbing path: `RpcConnection.wire_session.stream` (all pub).
- Wrote PLAN.md (design incl. the watch/lease race resolution).
- S2: added `session_raw_fd` (wire-proto lib.drift, exported) + `RpcConnection.raw_fd`
  / free `rpc.raw_fd` (rpc lib.drift, exported). wire-proto builds clean on abi18.
- S0: re-minted author-claims (source changed) + `just deploy` on abi18 →
  **both artifacts published clean, baseline smoke passed.** rpc source is
  abi18-clean end-to-end. NOTE: the parked `deploy_acquire_blocker` (0.33.13)
  did NOT resurface on abi18/0.33.49 — appears resolved by the new toolchain.

- **S1 GATE GREEN.** Ran `keepalive_probes_test` on abi18 (built directly, stdout
  captured):
  - Scenario A (local spike): keepalive VT runs, `counter=5`. ✅ scheduled.
  - Scenario C (trivial spawn+JOIN): `trivial join_timeout(1s)=Ok value=0`. ✅
    executor schedules+completes VTs.
  - Scenario B (`managed.open()`): `keepalive_vt` is `Some` (no "is None"/202),
    join=timeout (parked, correct), program exit 0.
  - Conclusion: the catastrophic `keepalive-vt-not-scheduled` failure does NOT
    reproduce on abi18 → **the bug is resolved by the new toolchain.** The
    poll_many rewrite proceeds on its pool-fd-watch merit (PLAN OQ1 closed).

## Decisions taken
- Idle-readable/hangup/err on a pooled conn ⇒ **recycle, don't drain** (§5.2).
- Watch/lease race ⇒ **generation-token exclusive recycle**: `ConnEntry.watch_token`
  minted fresh on every enter-available; recycle only on exact-token match still
  in `available` (§5.3). Closes the churn window (plan review #1).
- `poll_many` `Err` policy: `invalid-argument` ⇒ immediate resnapshot; backoff
  only for repeated unexpected errors, counted (plan review #2).
- Use named `io.IO_ERROR_KIND_*` constants, not string literals.
- Apply the single-fiber model to **both** pool and managed (§5.5).

### 2026-06-21 (cont.) — implementation
- pool.drift: `import std.io`; consts `IDLE_WATCH_CAP_MS=1000`, `POLL_SPIN_GUARD=8`,
  `POLL_ERR_BACKOFF_MS=50`; `ConnEntry.watch_token` + `PoolSlot.next_watch_token`
  + `_mint_token` (token minted under lock at every enter-available: seed +
  release); `_keepalive_loop` rewritten as the poll_many service loop; new
  `_snapshot_idle_entries`, `_ready_tokens`, `_contains_token`,
  `_recycle_ready_idles` (drain-and-rebuild, exact-token match).
- managed.drift: same shape over the single slot — `ManagedSlot.watch_token`
  (bumped in `_slot_put_decide`), `_keepalive_loop` poll loop, `_managed_ping_tick`
  (periodic active ping retained for silent half-open death), `_snapshot_managed_entry`,
  `_ready_has_any`, `_take_idle_if_token`.
- Two abi18 compile fixes worth noting: `Deque.len` is a METHOD (`.len()`), not a
  field (Array's `.len` IS a field — asymmetry); `io.PollReady` is NOT Copy, so
  read its fields through the index place (`ready[i].readable`), don't bind `val r`.
- **Validation (live DB mdb114-a, abi18):** `live_pool_smoke`, `live_managed_smoke`,
  `pool_release_discard_wakeup_regression`, `pool_acquire_timeout`,
  `managed_acquire_timeout`, `managed_release_wakeup` — all PASS. Full `just test`
  gate launched in background.
- New compiler UX adopted in touched code: named `io.IO_ERROR_KIND_*` constants.
  match-on-Int verified available on 0.33.49 (literals/int-consts + mandatory
  `default` arm; `_` rejected) but the new keepalive logic is range/Bool
  conditionals with no natural literal-match sites. House-cleaning sweep of
  existing `if/else`-on-Int-literal → `match` is a deferred follow-up (don't
  balloon this diff).

## Plan review (user, 2026-06-21) — incorporated
- #1 generation token → adopted (§5.3, OQ3 closed). #2 Err policy → adopted
  (§5.2). #3 staleness → S0/S1/S2 marked done. Validated-good items: recycle-not-
  drain, pull-under-lock, empty-set handling, S1-escalation gate.

## Code review (user, 2026-06-21) — incorporated
- **#1 HIGH (invalid-argument wedge):** the old branch only counted/slept on a
  non-timeout/cancel error and assumed the bad fd "drops out next pass" — false if
  the bad conn stays in `available`. ONE unregistrable fd also fails the whole
  aggregate poll, blinding readiness for every other fd. FIX: pool probes each fd
  individually (`_probe_bad_tokens`, tiny per-fd poll) to find the offender(s) +
  any masked readiness, then recycles them via the token path (`_recycle_and_report`);
  managed (single fd) recycles entries[0] directly (`_managed_recycle_idle`). Loop
  is now genuinely self-healing; spin-guard backoff only if a probe pass finds
  nothing.
- **#2 MEDIUM (no race regressions):** added `pool_idle_close_recycle_test`
  (phase1: current-token server-close recycles via poll, interval=60s so ping
  tick excluded; phase2: healthy churn → 0 spurious recycles) and
  `managed_idle_close_recycle_test` (server-close → recycle+reconnect via poll,
  replacement healthy). Needed: new `PoolEvent::IdleConnRecycled` (recycle was
  silent — observability gap), fixture proc `sp_set_wait_timeout`. Both PASS
  (~4.5s / ~2.1s run → poll path, not the 60s tick). Wired into LIVE_TESTS.
- **#3 MEDIUM/LOW (min_idle):** documented in `_recycle_ready_idles` + PLAN OQ5 —
  min_idle is a SEED-TIME floor (matches prior ping-failure discard), not
  maintained. **DECIDED (user, 2026-06-21): keep seed-floor for this release**; a
  maintained live floor is a deferred follow-up (separate behavior change with
  reconnect/backoff/load implications), only if teams need warm idle capacity.

## Blockers / awaiting
- S1 is a hard gate: if the VT-scheduling bug still repros on abi18 for *any*
  single VT, escalate to toolchain before proceeding (abort-on-spec/impl-gap).

## Next action (PAUSED for user review of pool.drift/managed.drift diff)
Decided but NOT yet done (awaiting review greenlight):
- Idle-close regression test (server KILL of an idle pooled conn → recycle).
- `just stress` + `just perf` gates.
- Version bumps: minor both — wire-proto 0.4.0→0.5.0, rpc 0.6.0→0.7.0 — then
  reseal (author-claim + prepare + trust-check). User authors the commit.
