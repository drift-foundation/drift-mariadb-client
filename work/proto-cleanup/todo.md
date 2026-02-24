# Wire Protocol Review: Open Items Only

## Next step

### Deferred: 20. Hex fixture file policy

Deferred for current phase. Live DB integration tests against local dev instances are the active acceptance gate; fixture-policy standardization will be revisited later.

## Recently closed

### 9.x COM_RESET_CONNECTION follow-through

Status: **closed**.

Completed:
- `COM_RESET_CONNECTION` command module and `reset_connection(session)`.
- `reset_for_pool_reuse()` uses reset as primary path and falls back to rollback/autocommit/ping on explicit unsupported response.
- ERR decode/classification path in place (`error_code=1047` => unsupported tag).

Resolution notes:
- Proactive capability-bit gate: **won't-fix** (no reliable capability bit for COM_RESET_CONNECTION; probe-and-disable is intended behavior).
- Explicit unsupported-fallback regression in full reset flow: **deferred** until replay/mock harness exists (expected to align with state-machine slice).

### State-machine foundation slice

Status: **closed**.

Completed outcomes:
- transition-regression live e2e coverage added (`tests/e2e/live_session_state_test.drift`)
- guard and command preamble centralization in `lib.drift` (`_require_ready`, `_begin_command`, `_command_send_recv`)
- packet transport extraction to `src/transport.drift` with `lib.drift` calling through
- guard diagnostics normalized (`query` active statement tag aligned to `"active-statement-present"`)
- capability normalization/validation completed (#11)
- WireConnectOptions boundary cleanup completed (#19)
- read-side max payload guard completed (#13)
- timeout semantics policy + `_duration_ms` dedup completed (#14)

## Protocol/API gaps

None currently open in this section.

## Robustness
None currently open in this section.

## Process

### 20. Hex fixture file policy

Status: **deferred**.

Reason:
- Current coverage focus is live/protocol behavior against controlled local MariaDB instances.
- Packetized artifacts are available for future replay-harness work, but not yet the active test source.
