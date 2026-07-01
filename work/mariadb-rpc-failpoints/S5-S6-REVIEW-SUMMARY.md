# S5/S6 status — failpoint proxy control plane

Status: implementation complete, review-clean, **uncommitted**. Branch `failpoint-api-proxy`. For reviewers coming in cold; see `PLAN.md` for the full design and `PROXY-GATE-HARNESS.md` for the tracked follow-up.

## What this delivers

The `mariadb-failpoint-proxy` app (a `kind:app` Drift artifact, not part of production `mariadb-rpc`) can now actually inject the fault it was built for: deterministic, one-shot ambiguous-COMMIT-ack-loss, armed and observed over a raw-TCP JSON Lines control API. Before this work the proxy only transparently forwarded traffic and observed COMMITs; nothing could arm a failure yet (S4/slice-1).

Two new capabilities, both in one PR-sized change (S5 and S6 were originally separate plan steps; delivered together since they're one JSON dispatch table):

1. **Control plane** (`failpoint-proxy/src/control.drift`, new file, 583 lines) — `arm` / `status` / `list` / `assert_all_fired` / `clear` / `health` over newline-delimited JSON on a second TCP listener, backed by a `conc.Arc<Mutex<Array<FailpointEntry>>>` registry.
2. **Fault action** wired into the existing data path (`failpoint-proxy/src/main.drift`, `failpoint-proxy/src/framing.drift`) — on the nth matching COMMIT since arm, forward exactly through that COMMIT packet, swallow the server's response, close both sockets.

## Files touched

| File | Change |
|---|---|
| `failpoint-proxy/src/control.drift` | **new** — registry + control-plane JSON dispatch + control TCP listener |
| `failpoint-proxy/tests/unit/control_test.drift` | **new** — pure unit tests for `control.drift` (no sockets) |
| `failpoint-proxy/src/main.drift` | fault action wired into `_pump_client_to_server`/`_pump_passthrough`; `--help` fast-path; header comment updated |
| `failpoint-proxy/src/framing.drift` | new `feed_and_locate_commits` (returns per-COMMIT end offsets, not just a count) |
| `failpoint-proxy/tests/unit/framing_test.drift` | new coalesced-boundary regression scenario |
| `drift/manifest.json` | added `control.drift` to the app's `modules` list |
| `drift/mariadb-failpoint-proxy.author-claim` (+ `mariadb-rpc`/`mariadb-wire-proto`) | re-minted (source content changed) |
| `work/mariadb-rpc-failpoints/PLAN.md` | S5/S6 marked done with implementation notes; stale S4-era references cleaned up |

## Design points worth knowing for review

- **Race-free swallow, not timing-free luck.** A per-connection `conc.Arc<AtomicBool>` swallow flag is set by the client→server pump *before* the triggering COMMIT is forwarded to the backend. Since the backend cannot produce a response before it receives the request, and the flag is set strictly before that send, the server→client pump — which checks the flag right after every read — can never let a swallowed-response byte through, regardless of scheduling.
- **Protocol-exact boundary.** `framing.feed_and_locate_commits` returns each matched COMMIT's chunk-relative end offset. On fire, only bytes through that offset are forwarded; anything later in the same TCP read (pipelined traffic, kernel coalescing) is discarded with the connection rather than forwarded. This was a real gap caught in review (see below) — the synchronous `mariadb-rpc` client doesn't produce this shape today, but the proxy is meant to be protocol-exact regardless of caller.
- **`match.nth` counts from arm-time**, per PLAN.md §7 — the counter starts at the `arm` ack, not at connection-open, so `max_conns=1` reuse of a pre-existing connection doesn't throw off the count. Needed for H1 (Singular emits `start` then `complete`; nth=2 targets `complete`).
- **Two intentional approximations, both diagnostics-only** (not gated by any DoD item): `bytes_forwarded_to_server` is hardcoded `11` (exact size of a bare `COMMIT` packet — `wire.commit()` always emits exactly that text); `server_response_bytes_dropped` is a documented `-1` "not measured" sentinel, since actually reading the swallowed response to count its bytes would itself risk forwarding part of it.
- **`handle_line(reg, line) -> String` is pure** — no sockets, no logging — so the whole control-plane contract is unit-tested directly (`control_test.drift`), the same pattern this repo already uses for `classify_commit_wire_tag` and the packet framer.

## Review history (all resolved)

This went through several review rounds; listing them because the fixes are the parts most worth a second look:

1. **Manifest gap (blocking).** `control.drift` wasn't in `drift/manifest.json`'s `modules` list. Local/test builds masked it because the test harness source-walks the whole `failpoint-proxy/src/` directory, but `drift deploy`/`drift author` read the manifest list literally — a real "works locally, breaks at deploy" gap. Fixed; author-claim re-minted.
2. **`assert_all_fired` vacuous pass.** Returned `ok:true` on an empty registry (nothing ever armed, or everything wiped by `clear`) — contradicted the plan's own "a test that forgot to arm fails loudly" requirement. Fixed: `ok:false, error:"not-armed"` whenever `armed_count == 0`. (This is what the closed error-code set's `not-armed` tag was reserved for.)
3. **Protocol-exactness gap (the boundary issue above).** Originally the fault action forwarded the *entire* socket read before closing, not just through the triggering COMMIT. Fixed properly (framer offset support + truncated forward), not documented away, with a new regression test pinning a coalesced `[COMMIT][SELECT]` read.
4. **Logging thinner than the plan's own language.** Only a generic `control_op` event existed; added named `failpoint_arm`/`failpoint_clear` events matching PLAN.md §6.
5. **Bonus, unrelated discovery:** `drift deploy`'s baseline app smoke runs `<bin> --help` and requires *some* exit within 30s (any code — it only rules out a hang/crash). The proxy is a daemon with no such path, so `just deploy` (and therefore `just stress`/`just perf`, both `: deploy`) hung. Pre-existing since S4 (nobody had run real `deploy` against this artifact before; S4 used `just build-app` specifically to avoid it). Fixed with a minimal `--help`/`-h` handler.
6. **Doc/log-name consistency (final pass).** A couple of stale references survived the fixes above — the `PLAN.md` §7 example still showed `server_response_bytes_dropped: 7` instead of `-1`, and both `main.drift`'s header comment and a "TODO (slices 2-3)" block in `PLAN.md` still described the control plane/fault action as future work. Cleaned up.

**Known accepted low-priority gap:** the control connection's `MAX_LINE_BYTES` (1 MB) cap is checked only in the no-`\n`-yet branch of the read loop, so a line that crosses the cap in the same read that also contains its terminating `\n` would be accepted up to one read-chunk (4 KB) over. Not worth fixing unless strict byte-exact rejection is wanted; flagged and consciously deferred.

## Verification

- `just test` (full certification gate: correctness × plain/ASAN/memcheck): **215 ok, 0 failed** — includes `control_test` and `framing_test` in all three variants.
- `just build-app mariadb-failpoint-proxy` and `just deploy`: both clean (all 3 manifest artifacts publish).
- **Live end-to-end** against `mdb114-a`: armed a failpoint over raw TCP, drove the real `mariadb-rpc` client (`live_proxy_passthrough_smoke_test.drift`) through the proxy — `conn.commit()` returned `Err` (ambiguous write) exactly as designed; proxy logs showed `commit_observed` → `failpoint_fire` → `conn_close{reason:"read-error"}`; `assert_all_fired` flipped from `not-armed` to `ok:true`; re-running the same smoke with nothing armed passed cleanly, proving the one-shot doesn't wedge the proxy or consume an unrelated future COMMIT.

## What's next (not in this change)

- **S7** — e2e test using a real `pool.ConnectionPool` with `max_conns=1`: arm, trigger via `conn.commit()`, assert `RpcCommitErrorKind::AmbiguousWrite`, assert fired-exactly-once, assert next acquire reconnects cleanly.
- **S8** — automated proxy-*process* gate coverage (`just test` currently exercises proxy logic in-process via unit tests, not the actual running binary as a subprocess). Tracked in `PROXY-GATE-HARNESS.md`.
- **S9** — app-usage docs (DB domain routing, control API, no failpoint code in the app binary).
- **S10** — share with app team / K.

## For reviewers

Everything above is uncommitted in the working tree on `failpoint-api-proxy` — nothing has been pushed or shared beyond this repo yet. The diff is small enough to read directly: `control.drift` (new, 583 lines) is the main surface; the `main.drift`/`framing.drift` changes are the fault-action wiring (169 and 49 lines respectively). `PLAN.md` §5–§9 has the full protocol spec if anything in `control.drift` looks under-motivated.
