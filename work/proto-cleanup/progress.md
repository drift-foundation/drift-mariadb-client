# Proto Cleanup Progress

## Execution Order

### Round 1: Error detail propagation
- [x] #4 `_query_expect_ok` swallows server error — surface ErrPacket details
- [x] #6 `auth-rejected` loses server error message — decode and surface ErrPacket

### Round 2: COM_QUIT
- [x] #3 `close()` should send COM_QUIT before socket close
- [x] #7 Add COM_QUIT command module

### Round 3: Sequence ID cleanup
- [x] #2 Removed dead `next_sequence_id` field (cleanup only)
- [x] #2b Response sequence-ID validation via `_session_read_packet`

### Round 5: Reusability / liveness hardening
- [x] #15 Add `state.reusable` check to `set_autocommit`/`commit`/`rollback`
- [x] #16 Add liveness probe (COM_PING) to `reset_for_pool_reuse`

### Cleanup (low-effort, any time)
- [x] #5 Remove dead field `emit_resultset_end_pending`
- [x] #10 Handle auth switch request (0xFE) during connect
- [x] #21 Update stale checklist in progress.md

### Round 4: Column metadata surfacing
- [x] #12 Parse and expose column definition packets instead of discarding

### Deferred (protocol/feature gaps, not immediate)
- [x] #8 COM_PING support
- [ ] #9 COM_RESET_CONNECTION support
- [ ] #11 Capability flags validation/normalization
- [ ] #19 WireConnectOptions design-layer cleanup

### Round 6: Column def decode hardening
- [x] #22 Malformed column def must propagate error, not silently skip
- [x] #23 `_decode_column_def` must validate 0x0C marker byte

### Round 7: Lenenc bounds fix + code dedup
- [x] #1 Lenenc off-by-one bounds check fix
- [x] #17 Consolidate duplicated `_decode_text_row` (resultset.drift canonical, lib.drift calls through)

### Needs-nuance (clarity/hardening, not bugs)
- [ ] #13 Max payload size cap on read
- [ ] #14 `_duration_ms` clamp documentation
- [ ] #20 Hex fixture file policy

## Work Log

Detailed completed-session notes were moved to `history.md` under `## 2026-02-23`.

Active tracking in this file should only cover:
- open checklist items above
- in-flight work during the current proto-cleanup slice
