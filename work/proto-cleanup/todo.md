# Wire Protocol Review: Open Items Only

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

## Architecture decision (next major slice)

### Protocol state-machine foundation (**immediate next item**)

Why:
- We want explicit protocol lifecycle rules before adding more behavior.
- Remaining open work is transition/guard heavy and should not grow via ad-hoc branching.

Target outcomes:
- Deterministic session/statement transitions.
- Centralized invariants and recovery policy.
- Cleaner extension path for future protocol features.
- Lower regression risk.

Design direction:
- Make transition rules first-class internals (not scattered conditionals).
- Keep transport framing/parsing separate from transition logic.
- Keep public API stable while internals migrate.
- Drive rollout via regression-first transition tests.

Execution ordering:
1. Build state-machine foundation first.
2. Then close `#11`, `#19`, `#13`, `#14`, and `#20` on top of that foundation.

## Protocol/API gaps (to be completed after state-machine foundation)

### 11. Capability flags management

`WireConnectOptions` still accepts raw capability bitmasks without validation/normalization against features the client requires.

### 19. WireConnectOptions low-level exposure

`WireConnectOptions` still exposes low-level `client_capabilities` and `character_set` directly instead of a higher-level caller model.

## Robustness

### 13. Max payload size enforcement

**File:** `packages/mariadb-wire-proto/src/lib.drift` (`_read_packet_payload`)

No read-side payload cap yet; add configurable max packet guard.

### 14. Timeout semantics documentation/policy

**File:** `packages/mariadb-wire-proto/src/lib.drift` (`_duration_ms`)

`<=0` currently clamps to `1ms`; document explicitly or introduce clear sentinel semantics.

## Process

### 20. Hex fixture file policy

Fixture directories are still mostly `.gitkeep` while tests embed hex inline; decide canonical policy and align fixtures/tests.
