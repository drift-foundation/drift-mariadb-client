# Proto Cleanup Progress

## Next steps (in order)

1. ~~**#11** Capability flags validation/normalization.~~ Done.
2. ~~**#19** WireConnectOptions design-layer cleanup.~~ Done.
3. **#13** Max payload size cap on read.
4. **#14** `_duration_ms` clamp documentation/policy.
5. **#20** Hex fixture file policy.

State-machine foundation slice is complete; #11 and #19 build on that baseline.

## #11 Capability flags validation/normalization — plan

### Problem

`connect()` passes `opts.client_capabilities` directly to `HandshakeResponse41.capabilities` (lib.drift:450) with no validation. Two gaps:

1. **Missing required flags.** The decode/encode paths implicitly depend on specific capability flags being set. A caller passing the wrong bitmask gets protocol desync, not a clear error.
2. **No server intersection.** The server advertises its capabilities in the handshake hello (`hello.capabilities`). The client should not request flags the server doesn't support.
3. **Unsupported features advertised.** Caller-requested flags for features the client doesn't implement (e.g. `CLIENT_LOCAL_FILES`, `CLIENT_PS_MULTI_RESULTS`) are passed through to the server, which may then exercise code paths this client cannot handle.

### Implicit capability dependencies

Audited every decode/encode path to determine which flags the implementation actually requires:

| Flag | Bit | Value | Required by |
|---|---|---|---|
| `CLIENT_PROTOCOL_41` | 9 | 512 | ERR packet decoder expects `#` sql_state marker. OK packet decoder expects status_flags + warnings after lenenc fields. |
| `CLIENT_TRANSACTIONS` | 13 | 8192 | `_apply_status` reads `SERVER_STATUS_IN_TRANS` and `SERVER_STATUS_AUTOCOMMIT`. Without this flag, server omits status flags. |
| `CLIENT_SECURE_CONNECTION` | 15 | 32768 | `_sha1_native_password_token` + HandshakeResponse41 sends lenenc-prefixed auth_response (not old fixed-8-byte format). |
| `CLIENT_PLUGIN_AUTH` | 19 | 524288 | Auth switch handling expects plugin name in handshake. `encode_handshake_response41` writes `auth_plugin_name`. |
| `CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA` | 21 | 2097152 | Auth response encoded as lenenc string (not length-limited to 255 bytes). |

Conditional flags (included based on caller context, not hard-required):

| Flag | Bit | Value | Condition |
|---|---|---|---|
| `CLIENT_CONNECT_WITH_DB` | 3 | 8 | When `opts.database` is non-empty. |
| `CLIENT_MULTI_RESULTS` | 17 | 131072 | Default-on (see justification below). |

**`CLIENT_MULTI_RESULTS` justification:** This flag is default-on rather than hard-required. The wire proto's `next_event` handles `SERVER_STATUS_MORE_RESULTS_EXISTS` correctly, and the RPC layer's `CALL` depends on multi-result. Without it, single-result flows still work — the server simply never sends multi-result markers. However, because the RPC layer always uses `CALL` (which returns multiple result sets), and because every modern MariaDB/MySQL server supports it, we include it by default. If a caller explicitly clears it and the server strips it, single-result flows remain correct. The hard-required check does **not** validate MULTI_RESULTS — only the five core protocol flags above.

Flags actively stripped during normalization (client does not implement these features):

| Flag | Bit | Value | Reason stripped |
|---|---|---|---|
| `CLIENT_LOCAL_FILES` | 7 | 128 | No LOAD DATA LOCAL support. Advertising it could cause the server to send file-request packets this client cannot handle. |
| `CLIENT_PS_MULTI_RESULTS` | 18 | 262144 | No prepared statement support. |
| `CLIENT_SESSION_TRACK` | 23 | 8388608 | No session state change tracking support. |

Flags passed through if caller requests them (harmless or opt-in):

- `CLIENT_LONG_FLAG` (bit 2) — 4-byte column flags in column defs. Harmless.
- `CLIENT_MULTI_STATEMENTS` (bit 16) — enables multi-statement SQL. Opt-in by caller; code handles it correctly if present.

### Design

Three phases. No public API changes to `WireConnectOptions`.

#### Phase 1: Unit tests for normalization logic

New test file: `tests/unit/capability_normalization_test.drift`.

`normalize_capabilities` is a pure deterministic function (three inputs → one output). Direct unit tests cover:

- **Required flags forced in:** caller passes `0`, result has all required flags set.
- **Database flag conditional:** with non-empty database, `CLIENT_CONNECT_WITH_DB` present; with empty database, absent.
- **MULTI_RESULTS default-on:** caller passes `0`, result includes `CLIENT_MULTI_RESULTS`.
- **Server intersection:** caller requests flag X, server doesn't advertise X, result omits X.
- **Server missing required cap:** server caps lack `CLIENT_PROTOCOL_41` → error with tag `"server-missing-required-capability"`.
- **Unsupported flags stripped:** caller requests `CLIENT_LOCAL_FILES | CLIENT_PS_MULTI_RESULTS | CLIENT_SESSION_TRACK`, **server also advertises all three**, result still omits them. Server must include unsupported flags so the test proves active stripping, not accidental intersection removal.
- **Pass-through flags preserved:** caller requests `CLIENT_MULTI_STATEMENTS`, server supports it, result includes it.

Add justfile recipe `wire-check-caps` and include in `just test`.

#### Phase 2: Capability constants and normalization function

**2a.** Add named capability constants to `protocol/constants.drift`. Decimal values (Drift has no hex literals).

```
pub const CLIENT_LONG_FLAG: Uint = 4;
pub const CLIENT_CONNECT_WITH_DB: Uint = 8;
pub const CLIENT_LOCAL_FILES: Uint = 128;
pub const CLIENT_PROTOCOL_41: Uint = 512;
pub const CLIENT_TRANSACTIONS: Uint = 8192;
pub const CLIENT_SECURE_CONNECTION: Uint = 32768;
pub const CLIENT_MULTI_STATEMENTS: Uint = 65536;
pub const CLIENT_MULTI_RESULTS: Uint = 131072;
pub const CLIENT_PS_MULTI_RESULTS: Uint = 262144;
pub const CLIENT_PLUGIN_AUTH: Uint = 524288;
pub const CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA: Uint = 2097152;
pub const CLIENT_SESSION_TRACK: Uint = 8388608;
```

**2b.** Add `normalize_capabilities` as a `pub` function in a new `src/capabilities.drift` module.

```
pub fn normalize_capabilities(requested: Uint, server: Uint, has_database: Bool) -> Result<Uint, PacketDecodeError>
```

`lib.drift` imports `capabilities` and calls through internally. The function is **not** re-exported from the top-level `lib.drift` export list — it's a module-public helper, accessible to unit tests via direct module import (`import mariadb.wire.proto.capabilities as caps`), but not part of the public wire-proto API surface. Same pattern as `transport.drift`.

Logic:
1. `REQUIRED` = sum of `CLIENT_PROTOCOL_41 + CLIENT_TRANSACTIONS + CLIENT_SECURE_CONNECTION + CLIENT_PLUGIN_AUTH + CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA` (single constant, value 2662912).
2. `UNSUPPORTED` = sum of `CLIENT_LOCAL_FILES + CLIENT_PS_MULTI_RESULTS + CLIENT_SESSION_TRACK` (single constant, value 8650880).
3. `effective = (requested | REQUIRED | CLIENT_MULTI_RESULTS) & ~UNSUPPORTED` — force required + default flags, strip unsupported.
4. If `has_database`: `effective = effective | CLIENT_CONNECT_WITH_DB`.
5. `final = effective & server` — intersect with server capabilities.
6. If `(final & REQUIRED) != REQUIRED`: return `Err("server-missing-required-capability", 0)`.
7. Return `Ok(final)`.

**2c.** In `connect()`, call `normalize_capabilities` between hello decode and HandshakeResponse41 construction. Use returned value instead of `opts.client_capabilities`.

### Forcing policy

**Explicit policy: required flags are silently forced.** Rationale: the five required flags are non-negotiable protocol structure dependencies — without them, every decode path fails. Silently ensuring them is analogous to a TCP library always setting SO_REUSEADDR; the caller shouldn't need to know. The `WireConnectOptions.client_capabilities` field is for *optional* feature bits (MULTI_STATEMENTS, etc.), not for opting out of protocol correctness. If a caller passes `0`, they get a working connection with minimal features. If a caller passes a curated bitmask, required flags are OR'd in transparently.

Unsupported flags are silently stripped for the same reason — advertising unimplemented features is a protocol-level hazard, not a caller choice.

### What this does NOT change

- **`WireConnectOptions` struct unchanged.** `client_capabilities` field stays. Callers set optional flags; required flags and stripping are internal normalization.
- **RPC layer unchanged.** `DEFAULT_CLIENT_CAPS` already includes all required flags. Normalization makes this safe for direct wire-proto callers.

## #19 WireConnectOptions design-layer cleanup — closure

**Decision:** Keep `WireConnectOptions` low-level. The struct stays as-is (no churn). The wire-proto layer is the low-level protocol layer; the RPC layer provides the high-level caller model. This is documented boundary, not a gap.

**Changes made:**

1. **Named constant for charset:** Added `UTF8MB4_CHARSET_ID: Byte = 45` to `protocol/constants.drift`. Added `DEFAULT_CHARSET_ID: Byte = 45` export from `lib.drift` (literal due to Drift v1 const cross-reference limitation; comment references canonical constant).

2. **`client_capabilities = 0` as default:** High-level callers (`live_proto_api_smoke_test`, `live_session_state_test`, `live_rpc_smoke_test`, RPC `connect()`) now pass `cast<Uint>(0)` for `client_capabilities`. The #11 normalization layer forces required flags, so zero is a valid "give me defaults" value.

3. **Low-level e2e tests use named constants:** `live_tcp_smoke_test`, `live_tcp_tx_test`, `live_tcp_load_test` bypass `connect()` and build `HandshakeResponse41` directly. These now construct capabilities from named `protocol.*` constants instead of magic integer `11510412`.

4. **RPC layer cleanup:** Removed `DEFAULT_CLIENT_CAPS: Uint = 11510412`. `DEFAULT_WIRE_CHARSET` kept as literal `45` (Drift v1 const limitation). `connect()` passes `client_capabilities = cast<Uint>(0)`.

5. **Magic literal `45` eliminated** from all callers via `protocol.UTF8MB4_CHARSET_ID` or `proto.DEFAULT_CHARSET_ID`.

All tests pass (`just test` green).

## #13 Max payload size enforcement — plan

### Problem

`read_packet_payload` and `session_read_packet` in `transport.drift` check `payload_len < 0` but not `payload_len > MAX_PAYLOAD_LEN`. The 3-byte wire header physically cannot encode more than 16777215, so this is a defensive assertion, not a live vulnerability. However, the explicit check makes the invariant visible and guards against any future header decode changes.

### Scope

The todo suggests "configurable max packet guard." Analysis: the 3-byte header inherently caps at ~16MB. Adding a configurable field to `WireConnectOptions` and threading it through `WireSession` is disproportionate struct churn. The right fix is to validate against `MAX_PAYLOAD_LEN` in both transport read paths. If a per-connection cap is needed later, it can be layered on.

### Changes

1. **`transport.drift`**: Import `packet_header.MAX_PAYLOAD_LEN`. In both `read_packet_payload` and `session_read_packet`, after the `payload_len < 0` check, add `if hdr.payload_len > packet_header.MAX_PAYLOAD_LEN { return Err("wire-payload-too-large", hdr.payload_len); }`.

2. **No struct changes.** No new fields in `WireConnectOptions` or `WireSession`.

3. **No new tests.** This guard cannot be triggered by the protocol's own 3-byte encoding — it's a defensive assertion. Unit-testing it would require mocking the header decoder to return an out-of-range value, which the current test harness doesn't support. The existing live tests exercise the read paths and confirm they work.

## Completed rounds

Summary only; detailed notes in `history.md` under `## 2026-02-23`.

- Rounds 1–7: #1–#8, #10, #12, #15–#17, #22, #23 (error propagation, COM_QUIT, sequence validation, column metadata, pool guards, lenenc fix, decode dedup)
- Round 8/8b: #9, #9b (COM_RESET_CONNECTION, ERR classification, pool reset primary/fallback)
- Round 9: state-machine foundation (transition regression tests, guard/command centralization, transport extraction, diagnostics normalization)

## Work log

Detailed completed-session notes live in `history.md` under `## 2026-02-23`.
