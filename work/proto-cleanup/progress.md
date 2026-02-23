# Proto Cleanup Progress

## Next steps (in order)

1. **State-machine foundation** — immediate next slice (see audit below).
2. **#11** Capability flags validation/normalization.
3. **#19** WireConnectOptions design-layer cleanup.
4. **#13** Max payload size cap on read.
5. **#14** `_duration_ms` clamp documentation/policy.
6. **#20** Hex fixture file policy.

Items 2–6 are to be completed on top of the state-machine foundation.

## Completed rounds

### Rounds 1–7
- [x] #4 `_query_expect_ok` swallows server error — surface ErrPacket details
- [x] #6 `auth-rejected` loses server error message — decode and surface ErrPacket
- [x] #3 `close()` should send COM_QUIT before socket close
- [x] #7 Add COM_QUIT command module
- [x] #2 Removed dead `next_sequence_id` field
- [x] #2b Response sequence-ID validation via `_session_read_packet`
- [x] #15 Add `state.reusable` check to `set_autocommit`/`commit`/`rollback`
- [x] #16 Add liveness probe (COM_PING) to `reset_for_pool_reuse`
- [x] #5 Remove dead field `emit_resultset_end_pending`
- [x] #10 Handle auth switch request (0xFE) during connect
- [x] #12 Parse and expose column definition packets
- [x] #8 COM_PING support
- [x] #22 Malformed column def must propagate error
- [x] #23 `_decode_column_def` must validate 0x0C marker byte
- [x] #1 Lenenc off-by-one bounds check fix
- [x] #17 Consolidate duplicated `_decode_text_row`

### Round 8/8b: COM_RESET_CONNECTION (#9, #9b) — closed
- [x] #9 COM_RESET_CONNECTION command module, `reset_connection()`, updated `reset_for_pool_reuse()` with primary/fallback
- [x] #9b.1 ERR packet decode + error code 1047 classification (unsupported vs real error)
- [x] #9b.2 `reset_for_pool_reuse()` only disables on unsupported; real errors propagated
- #9b.3 Capability-bit pre-gate — **won't-fix** (no standard capability bit exists; reactive probe is correct)
- #9b.4 Explicit unsupported-fallback test — **deferred** until replay/mock harness exists

## State-machine audit (pre-design report)

Audit of `lib.drift` to map implicit states, transitions, and guard patterns before designing the state-machine foundation.

### Session-level states (implicit, derived from WireSession fields)

| Implicit state | Fields that define it |
|---|---|
| **Connecting** | Before `connect()` returns — no `WireSession` exists yet |
| **Ready** | `is_closed=false`, `reusable=true`, `active_statement=false` |
| **Busy** (statement active) | `active_statement=true` |
| **Not-reusable** | `reusable=false`, `is_closed=false` |
| **Dead/Closed** | `is_closed=true` (implies `reusable=false`) |

### Statement-level states (already explicit via MODE_* constants)

- `MODE_RESULTSET` (3) — streaming rows
- `MODE_PENDING_OK` (1) — terminal OK waiting to be consumed
- `MODE_PENDING_ERR` (2) — terminal ERR waiting to be consumed
- `MODE_NEED_NEXT_FIRST` (4) — multi-result: awaiting next result set's first packet
- `MODE_DONE` (5) — fully consumed

### Guard pattern (repeated 6 times)

Same 3-check preamble in `query`, `set_autocommit`, `commit`, `rollback`, `ping`, `reset_connection`:

```
if session.is_closed { ... "session-closed" }
if not session.state.reusable { ... "session-not-reusable" }
if session.active_statement { ... "active-statement-present" }
```

`reset_for_pool_reuse` has a variant (no reusable check — intentionally, since it restores reusability). `close` only checks `is_closed`.

### Transition map

| From | Trigger | To | Location |
|---|---|---|---|
| (none) | `connect()` success | Ready | :564 |
| (none) | `connect()` failure | (none) — no session | :566 |
| Ready | `query()` | Busy | :641 |
| Ready | `ping/reset_connection/commit/rollback/set_autocommit` | Ready | various |
| Ready | `close()` | Dead | :570-583 |
| Ready | `reset_for_pool_reuse()` | Ready | :907-953 |
| Busy | `next_event → StatementEnd/StatementErr` | Ready | :414-416 |
| Busy | `Statement.destroy` (drop without consuming) | Ready or Dead | :484-497 |
| Any | `_mark_dead()` | Dead | :336-340 |
| Any | transport/decode error in command | Dead (via `_mark_dead`) | scattered |

### Failure semantics (two categories, not formalized)

1. **Transport/protocol failure** → `_mark_dead(session)` → session permanently dead.
2. **Server-level error** (ERR packet) → session stays alive, stream synchronized — e.g., `reset_connection` on unsupported, `_query_expect_ok` server errors.

The distinction is ad-hoc: some paths mark dead on unexpected responses, others don't. `reset_connection` explicitly doesn't mark dead on ERR (correct). `ping` marks dead on non-OK (correct — ping should not return ERR in normal protocol flow, but treating non-OK as terminal remains correct for pool health).

### Observations

1. **Guard pattern is the main centralization candidate.** Six copies of the same 3 checks. A single `_require_ready(session)` would eliminate repetition and make the precondition explicit.

2. **Statement mode is already a state machine.** The `MODE_*` constants + `next_event` dispatch loop is well-structured. Not much to change here.

3. **Session state is implicit but simple.** Only 4 meaningful states (Ready/Busy/NotReusable/Dead), determined by 3 booleans. Introducing a formal `variant` would make transitions explicit, but the current booleans work because:
   - `is_closed` is monotonic (once true, never reverts)
   - `reusable` is mostly monotonic, except explicit recovery paths (`reset_for_pool_reuse`)
   - `active_statement` toggles cleanly with statement lifecycle

4. **`reset_for_pool_reuse` is the most complex transition** — only function that traverses multiple states in one call (try reset → maybe fallback → rollback → autocommit → ping). Also the only place that intentionally skips the `reusable` guard.

5. **`_mark_dead` is the catch-all terminal transition.** Used correctly everywhere: transport errors kill the session, protocol-level ERR responses don't.

## State-machine foundation — implementation plan

Three phases, each independently verifiable via `just test`. No public API changes.

### Phase 1: Transition regression tests

Pin current guard and transition behavior before refactoring. New live e2e test file: `tests/e2e/live_session_state_test.drift`.

Scenarios to cover:
- **Guard: session-closed** — `close()` then `query()` → expect `"session-closed"` error.
- **Guard: active-statement-present** — `query()` without consuming, then `ping()` → expect `"active-statement-present"` error.
- **Guard: session-not-reusable** — trigger `_mark_dead` (e.g., query on bad SQL that causes protocol desync, or close + reopen), then `query()` → expect `"session-not-reusable"` or `"session-closed"` error.
- **Busy → Ready transition** — `query()`, consume to `StatementEnd`, then `ping()` succeeds (session is Ready again).
- **Drop auto-drain → Ready** — `query()`, drop statement without consuming, then `ping()` succeeds.
- **reset_for_pool_reuse restores reusability** — already covered in `live_proto_api_smoke_test:scenario_tx_roundtrip`, but add explicit `session_is_reusable()` assertion before and after.

Note: `query()` uses `"statement-already-active"` while the other 5 guarded functions use `"active-statement-present"`. Phase 2 will normalize this.

### Phase 2: Guard and command centralization

In `lib.drift`, extract three internal helpers:

1. **`_require_ready(session) -> Result<Void, PacketDecodeError>`**
   - The 3-check guard: `is_closed` → `"session-closed"`, `!reusable` → `"session-not-reusable"`, `active_statement` → `"active-statement-present"`.
   - Replaces 6 inline copies in `query`, `set_autocommit`, `commit`, `rollback`, `ping`, `reset_connection`.
   - Normalizes `query()`'s `"statement-already-active"` tag to `"active-statement-present"` (update Phase 1 test expectations accordingly).

2. **`_begin_command(session) -> Result<Void, PacketDecodeError>`**
   - `_require_ready(session)` + `_session_reset_seq(session)`.
   - Used by `ping`, `reset_connection`, `query`.
   - Not used by `set_autocommit`/`commit`/`rollback` (they go through `_query_expect_ok` → `query()`, which resets seq internally).

3. **`_command_send_recv(session, payload) -> Result<Array<Byte>, PacketDecodeError>`**
   - `_session_write_packet` + `_session_read_packet`, with `_mark_dead` on either failure.
   - Used by `ping` and `reset_connection` (identical write/read/mark-dead pattern today).
   - Not used by `query` (query sets `active_statement` between write and read, different flow).

`close` and `reset_for_pool_reuse` keep their specific guard variants (close only checks `is_closed`; reset_for_pool_reuse skips `reusable` check intentionally).

### Phase 3: Transport extraction

Move packet I/O to `src/transport.drift`:
- `_read_exact`, `_write_all`
- `_read_packet_raw`, `_read_packet_payload`, `_write_packet`
- `_session_read_packet`, `_session_write_packet`, `_session_reset_seq`

`lib.drift` imports `transport` and calls through. No behavioral change, pure file organization.

This separates transport framing from protocol transition logic, per the design direction. `lib.drift` retains:
- Public API functions (connect, close, query, etc.)
- Protocol transition helpers (_require_ready, _begin_command, _command_send_recv, _apply_status, _mark_dead)
- Decode helpers (_decode_column_count, _decode_column_def, _init_statement_from_first)
- Crypto/encoding helpers (_sha1_native_password_token, etc.)

### What this plan does NOT do (and why)

- **No formal session state variant.** The audit showed 4 states from 3 booleans. A variant would add indirection without solving the actual problems (guard duplication, boilerplate). The booleans work because `is_closed` is monotonic, `reusable` is mostly monotonic, and `active_statement` toggles cleanly. If state complexity grows later, upgrading to a variant is straightforward.
- **No statement mode changes.** The `MODE_*` constants + `next_event` dispatch loop is already a well-structured state machine.
- **No separate protocol transition module.** The transition helpers (_require_ready, _begin_command, _mark_dead, _apply_status) are small and tightly coupled to the API functions. Extracting them to a separate file would add import overhead without meaningful separation. Transport extraction (Phase 3) is the natural split.

## Work log

Detailed completed-session notes live in `history.md` under `## 2026-02-23`.
