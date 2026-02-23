# Wire Protocol Review: Bugs, Issues, Improvements & Missing Features

## Bugs / Correctness Issues

### 1. Off-by-one in lenenc truncation checks

**File:** `packages/mariadb-wire-proto/src/packet/lenenc.drift:79,86,93`

The 2-byte, 3-byte, and 8-byte lenenc decode paths use `offset + N >= input.len` but should use `offset + 1 + N > input.len` (the data bytes start at `offset+1`). For the 2-byte case at line 79:

```
if offset + 2 >= input.len   // current: rejects valid input where last data byte is at input.len-1
```

Consider input `[0xFC, 0x01, 0x00]` (len=3). With `offset=0`: `0 + 2 >= 3` is false -- this happens to work here. But the check is semantically wrong: it checks whether the *last byte index* is in bounds, when it should check `offset + 3 > input.len`. For `offset=0` both give the same result, but for nonzero offsets the current form `offset + 2 >= input.len` rejects a valid packet where the last byte sits at exactly `input[input.len - 1]`. Specifically for the 3-byte case (`offset + 3 >= input.len`): input at offset 1 in a 5-byte buffer = `1 + 3 >= 5` = true = wrongly rejected.

The correct checks should be:
- 2-byte: `offset + 3 > input.len`
- 3-byte: `offset + 4 > input.len`
- 8-byte: `offset + 9 > input.len`

### 2. Sequence ID tracking is completely dead

**File:** `packages/mariadb-wire-proto/src/lib.drift:408`, `packages/mariadb-wire-proto/src/types.drift:99`

`next_sequence_id` is stored in `WireSession` but never incremented or validated. Every `_write_packet` call hardcodes `cast<Byte>(0)` or `cast<Byte>(1)`. The MariaDB protocol requires incrementing sequence IDs within a command/response exchange. While most servers are lenient about this, it's a protocol violation and could cause issues with strict proxy middleware or future server versions.

### 3. `close()` does not send `COM_QUIT`

**File:** `packages/mariadb-wire-proto/src/lib.drift:414-425`

The protocol specifies that clients should send a `COM_QUIT` (command byte `0x01`) before closing the socket. The current implementation just closes the TCP stream directly. This leaves the server holding the connection in a non-clean state and may cause server-side error logging.

### 4. `_query_expect_ok` swallows the server error

**File:** `packages/mariadb-wire-proto/src/lib.drift:329`

When a tx command (`COMMIT`/`ROLLBACK`/`SET autocommit`) returns `StatementErr`, the actual `ErrPacket` with error code and message is discarded and replaced with a generic `"tx-command-server-err"` tag. The caller has no way to know *why* the server rejected the command.

### 5. `emit_resultset_end_pending` field is dead

**File:** `packages/mariadb-wire-proto/src/types.drift:109`

This field is declared on `Statement`, initialized to `false`, but never read or written anywhere in the codebase. Dead struct field.

### 6. `auth-rejected` error loses the server error message

**File:** `packages/mariadb-wire-proto/src/lib.drift:402`

When the server returns an ERR packet during auth, the code returns a generic `"auth-rejected"` error. It should decode the ERR packet and surface the `error_code` / `message` (e.g., "Access denied for user 'foo'@'host'") so the caller can act on it.

## Protocol Gaps

### 7. No `COM_QUIT` command defined

There's no `com_quit.drift` or equivalent. `COM_QUIT` (byte `0x01`) is the proper way to terminate a connection.

### 8. No `COM_PING` support

`COM_PING` (byte `0x0E`) is essential for connection pool health checks. Without it, `reset_for_pool_reuse` can't verify the connection is still alive before handing it back.

### 9. No `COM_RESET_CONNECTION` support

`COM_RESET_CONNECTION` (byte `0x1F`) is the proper way to reset session state for pool reuse. The current `reset_for_pool_reuse` uses `ROLLBACK` + `SET autocommit=1`, which doesn't reset user variables, temp tables, or prepared statement state.

### 10. Auth plugin negotiation not handled

**File:** `packages/mariadb-wire-proto/src/lib.drift:355-411`

If the server responds to the handshake with an `AuthSwitchRequest` (0xFE header) instead of OK/ERR, the code will fall through to `"auth-invalid-response"`. MariaDB servers can request a plugin switch even for `mysql_native_password` in some configurations.

### 11. No `CLIENT_PROTOCOL_41` / capability flags management

The `client_capabilities` are passed through from `WireConnectOptions` raw, with no validation that the required flags for the protocol features used (like SQL state in ERR packets, or status flags in OK packets) are actually set. The caller must know to set these correctly.

### 12. Column definitions are discarded

**File:** `packages/mariadb-wire-proto/src/lib.drift:520-526`

During resultset streaming, column definition packets are read and immediately dropped. Column metadata (name, type, charset, length, flags) is never surfaced. This means:
- Callers can't know column names (blocks name-based row access in RPC layer)
- Callers can't know column types for type-safe conversion
- The RPC layer will need this for `row.get_int(name)` etc.

## Robustness Improvements

### 13. No max payload size enforcement on read

**File:** `packages/mariadb-wire-proto/src/lib.drift:170-183`

`_read_packet_payload` reads whatever `payload_len` the header claims, with no upper bound check. A malicious or buggy server could send a 16MB header length, causing an OOM allocation. Should cap at a configurable max (e.g., `max_packet_size` from connect options).

### 14. `_duration_ms` silently clamps <=0 to 1ms

**File:** `packages/mariadb-wire-proto/src/lib.drift:71-76`

If the user sets `io_timeout_ms = 0` (which could mean "no timeout" or "error"), it silently becomes 1ms. This should either be documented or use a different sentinel for "no timeout".

### 15. No reusability check on `set_autocommit`/`commit`/`rollback`

**File:** `packages/mariadb-wire-proto/src/lib.drift:609-640`

These functions check `is_closed` and `active_statement` but do NOT check `state.reusable`. If a prior I/O error marked the session non-reusable, these will still attempt I/O on a potentially corrupted stream. Compare with `query()` at line 453 which does check reusability.

### 16. `reset_for_pool_reuse` doesn't verify connection liveness

After rollback/autocommit reset, there's no ping or lightweight validation that the server is still responsive. A half-closed TCP connection would pass all checks.

## Code Quality

### 17. Duplicated `_decode_text_row` logic

`_decode_text_row` exists in both `lib.drift:241-268` and `decode/resultset.drift:60-87` with identical logic. The streaming path in `lib.drift` uses its own copy. The `resultset.drift` copy is only used by `decode_text_resultset_packets` (the batch/packet-array API). Should consolidate.

### 18. Duplicated `_append_bytes` helper

Same helper exists in `lib.drift:86-92` and `auth.drift:39-45`.

### 19. `WireConnectOptions` exposes raw `client_capabilities` and `character_set`

These are low-level protocol details that shouldn't be exposed to the RPC caller. The work-progress doc already designs `RpcConnectionConfigBuilder` with high-level names like `charset: String`, but the wire layer requires the caller to know the numeric capability bitmask and character set byte.

## Missing from Phase 1 Checklist

### 20. Hex fixture files still empty

All fixture directories (`tests/fixtures/handshake/`, `tests/fixtures/packet/`, `tests/fixtures/resultset/`) contain only `.gitkeep`. Tests embed hex inline instead. The checklist calls for `.hex` fixture files.

### 21. Phase 1 checklist items 2-3 still marked unchecked

The implementation exists and tests pass, but the checklist in `progress.md` shows `[ ]` on packet header, lenenc, and handshake items. Either the checklist is stale or there are missing sub-items.

## Summary Priority

| Priority | Item | Category |
|----------|------|----------|
| High | #1 lenenc off-by-one | Bug |
| High | #4 tx error swallowed | Bug |
| High | #6 auth error swallowed | Bug |
| High | #12 column defs discarded | Missing feature |
| Medium | #3 no COM_QUIT | Protocol gap |
| Medium | #2 sequence ID dead | Protocol gap |
| Medium | #10 no auth switch | Protocol gap |
| Medium | #13 no max payload cap | Robustness |
| Medium | #15 no reusability check on tx ops | Bug |
| Low | #5 dead field | Cleanup |
| Low | #7-9 COM_PING/RESET | Protocol gap |
| Low | #17-18 duplication | Code quality |

## Next Immediate Steps

1. Finish #2 sequence-id work as a real protocol item (not cleanup only):
- Add response sequence-id tracking/validation in `_read_packet_payload`.
- Add minimal failing regression first (bad sequence on second packet), then implement fix.
- Keep existing behavior for client-side send sequence (`seq=0` commands, `seq=1` handshake response) unchanged unless regression proves otherwise.

2. Implement #10 auth switch handling:
- Add `AuthSwitchRequest (0xFE)` branch during connect/auth response handling.
- Support at least `mysql_native_password` switch flow in MVP.
- Add deterministic fixture-based test for auth-switch happy path and unsupported-plugin failure.

3. Start #12 column metadata surfacing:
- Parse and store column definition packets in streaming statement state.
- Expose metadata to callers (minimum: name + type + flags) without breaking existing row streaming API.
- Add one replay test and one live e2e assertion that validates metadata count/name availability.

4. Address #16 via #8 dependency planning:
- Define minimal `COM_PING` command support.
- Use it in `reset_for_pool_reuse` liveness verification after rollback/autocommit normalization.
- Add a test that simulates dead connection path and verifies deterministic non-reusable outcome.

5. Keep hygiene in sync while touching these areas:
- Update `work/proto-cleanup/progress.md` checklist and verification block per round.
- Update stale Phase 1/1.5 checkboxes in `progress.md` where implementation is already landed.

## Review Addendum (latest pass)

### 22. Wrong offset reported for bad column-def fixed-length marker

**File:** `packages/mariadb-wire-proto/src/lib.drift` (`_decode_column_def`)

Status: **resolved**.

The `"coldef-bad-fixed-length-marker"` error now reports the payload index (`fixed_start`) rather than marker byte value.

### 23. Column-def decode does not reject trailing bytes

**File:** `packages/mariadb-wire-proto/src/lib.drift` (`_decode_column_def`)

Column-def parsing validates minimum fixed block length, but accepts extra trailing bytes without error. For strict packet validation, require exact payload consumption and return a deterministic trailing-bytes decode error when `fixed_start + 12 != payload.len`.

Status: **open** (policy decision required).

Decision/options:
- strict reject: safest correctness posture, fail statement/session on trailing bytes.
- tolerate: better forward-compatibility with server/proxy extensions.

### 24. Progress log text is stale vs current behavior

**File:** `work/proto-cleanup/progress.md:111`

Status: **resolved**.

Progress notes now reflect that column-def decode errors propagate and mark session dead.
