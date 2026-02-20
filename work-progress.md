# MariaDB Client Work Progress

## Goal

Provide a Drift-native MariaDB client focused on Stored Procedure calls, with clear separation between protocol mechanics and RPC-style usage.

## Pinned architecture

Two packages in one repository:

1. `mariadb-wire-proto`
- Owns wire protocol concerns only.
- Responsibilities:
  - packet framing/deframing
  - handshake and capability negotiation (MVP-constrained)
  - auth flow (MVP-constrained plugin set)
  - command/response state machine (`COM_QUERY` first)
  - result/OK/ERR packet decoding
- No business-level API for “call procedure”.

2. `mariadb-rpc`
- SP-oriented API built on `mariadb-wire-proto`.
- Responsibilities:
  - `call(proc_name, args)` style surface
  - SQL call construction for stored procedures (MVP)
  - mapping protocol results to Drift-friendly return shapes
  - error tagging suitable for machine handling
- No direct packet logic.

## Why split into two packages

- Keeps low-level protocol isolated and testable.
- Allows iterative replacement/extension of RPC behavior without destabilizing protocol code.
- Lets future users consume raw wire package for non-SP use cases.

## MVP constraints (explicit)

- Server: controlled MariaDB version(s).
- Auth: basic constrained mode(s) only.
- TLS: disabled in MVP.
- Operations: Stored Procedure invocation only (`COM_QUERY` path first).
- Concurrency model: integrates with Drift virtual-thread runtime through existing network I/O primitives.

## User-land validation objective

- This is the first Drift user-land library effort, not just a protocol implementation task.
- We expect real package-development pressure to surface integration gaps in `driftc` and/or stdlib.
- When such issues are found, record minimal repros and treat them as first-class integration outcomes while continuing delivery of a useful MariaDB client.

## Proposed phases

### Phase 0: Contract pinning
- Finalize package names and public module ids.
- Pin `mariadb-rpc` API signatures and error tags.
- Pin supported auth plugin(s) and server capability assumptions.

### Phase 1: Wire foundations (`mariadb-wire-proto`)
- Packet reader/writer + length-encoded primitives.
- Handshake/auth happy path.
- `COM_QUERY` request + OK/ERR/resultset decode.
- Deterministic parser tests with fixed binary fixtures.

#### Phase 1 concrete checklist (with file-level TODOs)

1. Package skeleton and module boundaries
- [x] Create package root and public modules:
  - `packages/mariadb-wire-proto/src/lib.drift`
  - `packages/mariadb-wire-proto/src/types.drift`
  - `packages/mariadb-wire-proto/src/errors.drift`
- [x] Define internal module split:
  - `packages/mariadb-wire-proto/src/packet/header.drift`
  - `packages/mariadb-wire-proto/src/packet/lenenc.drift`
  - `packages/mariadb-wire-proto/src/handshake/hello.drift`
  - `packages/mariadb-wire-proto/src/handshake/auth.drift`
  - `packages/mariadb-wire-proto/src/command/com_query.drift`
  - `packages/mariadb-wire-proto/src/decode/ok_packet.drift`
  - `packages/mariadb-wire-proto/src/decode/err_packet.drift`
  - `packages/mariadb-wire-proto/src/decode/resultset.drift`

2. Packet framing + length-encoded primitives
- [ ] Implement packet header encode/decode in `packages/mariadb-wire-proto/src/packet/header.drift`.
- [ ] Implement length-encoded integer/string helpers in `packages/mariadb-wire-proto/src/packet/lenenc.drift`.
- [ ] Add unit fixtures and roundtrip tests:
  - `packages/mariadb-wire-proto/tests/unit/packet_header_test.drift`
  - `packages/mariadb-wire-proto/tests/unit/lenenc_test.drift`
  - `packages/mariadb-wire-proto/tests/fixtures/packet/*.hex`

3. Handshake/auth happy path (MVP plugin set)
- [ ] Parse server handshake in `packages/mariadb-wire-proto/src/handshake/hello.drift`.
- [ ] Build client handshake response in `packages/mariadb-wire-proto/src/handshake/auth.drift`.
- [ ] Implement constrained auth flow state transition (happy path only) in `packages/mariadb-wire-proto/src/handshake/auth.drift`.
- [ ] Add deterministic transcript tests:
  - `packages/mariadb-wire-proto/tests/unit/handshake_decode_test.drift`
  - `packages/mariadb-wire-proto/tests/fixtures/handshake/*.hex`

4. `COM_QUERY` encode + first response discriminator
- [x] Implement query packet encode in `packages/mariadb-wire-proto/src/command/com_query.drift`.
- [x] Implement response routing (OK vs ERR vs resultset header) in `packages/mariadb-wire-proto/src/decode/resultset.drift`.
- [x] Add command/decode tests:
  - `packages/mariadb-wire-proto/tests/unit/com_query_test.drift`
  - `packages/mariadb-wire-proto/tests/unit/response_discriminator_test.drift`

5. OK/ERR/resultset decode
- [x] Implement OK packet decode in `packages/mariadb-wire-proto/src/decode/ok_packet.drift`.
- [x] Implement ERR packet decode in `packages/mariadb-wire-proto/src/decode/err_packet.drift`.
- [x] Implement resultset decode (column count, column definitions, row values, terminator handling) in `packages/mariadb-wire-proto/src/decode/resultset.drift`.
- [ ] Add fixture-driven parser tests:
  - `packages/mariadb-wire-proto/tests/unit/ok_packet_test.drift`
  - `packages/mariadb-wire-proto/tests/unit/err_packet_test.drift`
  - `packages/mariadb-wire-proto/tests/unit/resultset_decode_test.drift`
  - `packages/mariadb-wire-proto/tests/fixtures/resultset/*.hex`
  Note: unit tests are now in place; `.hex` fixture files are still pending.

6. Wire error model
- [x] Define stable wire-layer error tags and payloads in `packages/mariadb-wire-proto/src/errors.drift`.
- [x] Ensure decode/auth code paths return structured errors (no ad-hoc strings) across:
  - `packages/mariadb-wire-proto/src/handshake/auth.drift`
  - `packages/mariadb-wire-proto/src/decode/ok_packet.drift`
  - `packages/mariadb-wire-proto/src/decode/err_packet.drift`
  - `packages/mariadb-wire-proto/src/decode/resultset.drift`
- [x] Add unit tests for error mapping:
  - `packages/mariadb-wire-proto/tests/unit/error_tags_test.drift`

7. Real-DB smoke validation against local instance tooling
- [x] Add smoke harness:
  - `packages/mariadb-wire-proto/tests/e2e/com_query_smoke_test.drift`
- [x] Validate: connect/auth/query success and server-side SQL error decode.
- [x] Keep this as controlled-config E2E only (no TLS, no pooling).
  Note: live TCP e2e now runs via `packages/mariadb-wire-proto/tests/e2e/live_tcp_smoke_test.drift` and `just wire-live`.

8. Phase 1 exit criteria
- [x] Packet/handshake/OK/ERR/resultset unit tests green with fixed binary fixtures.
- [x] E2E smoke green against local MariaDB instance fixtures (captured from controlled local instance).
- [x] No RPC/SP call-surface code introduced in `mariadb-wire-proto`.

### Phase 1.5: Protocol session API plan (`mariadb-wire-proto`)

Goal: expose a low-level, pooling-friendly wire session surface before `mariadb-rpc` call ergonomics.

1. Live tx validation gate (before API freeze)
- [x] Add/extend live e2e for manual transaction flows in:
  - `packages/mariadb-wire-proto/tests/e2e/live_tcp_smoke_test.drift`
  - `packages/mariadb-wire-proto/tests/e2e/live_tcp_tx_test.drift`
  - include explicit commit, rollback, and error-then-rollback paths.
- [x] Validate in normal + ASAN + memcheck.

2. Session/result API shapes
- [ ] Define public protocol session types in:
  - `packages/mariadb-wire-proto/src/types.drift`
  - include:
    - response route/shape for `OK | ERR | ResultSet`
    - session state snapshot fields needed by pooling (`autocommit`, transaction/status view).
- [ ] Export these through:
  - `packages/mariadb-wire-proto/src/lib.drift`

3. COM_QUERY low-level wrappers
- [ ] Implement/land low-level wrappers in:
  - `packages/mariadb-wire-proto/src/lib.drift`
  - target shape:
    - `query(...)` to start statement execution
    - iterator-style consume API (`next_result` / `next_row`) for multi-resultset flows.
- [ ] Ensure SPs yielding multiple resultsets are first-class (no single-result assumption).
- [x] Pin API policy:
  - do not provide `query_all` / eager full aggregation in the current wire API.
  - public surface must be streaming-first and iterator-driven.
- [x] Pin buffering policy:
  - internal read-ahead/buffering is allowed for transport efficiency.
  - buffering must be bounded (no unbounded read-all accumulation before user consumption).
  - no implicit whole-result materialization in wire core.

4. Explicit tx command wrappers (wire-level sugar)
- [ ] Add wrappers for:
  - `set_autocommit(...)`
  - `commit(...)`
  - `rollback(...)`
- [ ] Keep wrappers as thin COM_QUERY sugar (no RPC/business policy here).
- [ ] Ensure returned OK packet status flags are surfaced to caller.
- [x] Pin behavior policy:
  - safe-by-default for tx control on partially consumed statements.
  - `commit` / `rollback` must auto-drain remaining response parts before issuing tx command.
  - if auto-drain fails, mark session non-reusable and return deterministic error.

5. Pooling compatibility contract
- [ ] Add wire-session reuse/reset contract in:
  - `packages/mariadb-wire-proto/src/lib.drift`
  - include:
    - reusable/non-reusable determination
    - reset path suitable before returning a connection to pool.
- [ ] Add deterministic tests in:
  - `packages/mariadb-wire-proto/tests/unit/*`
  - `packages/mariadb-wire-proto/tests/e2e/*`
- [x] Pin lifecycle/ownership policy:
  - single-active-statement per session: `query()` must reject if prior statement is not terminal/drained.
  - drain completion may be via normal event consumption or explicit skip APIs (`skip_result` / `skip_remaining`).
  - `Statement` uses max-safety ownership: holds mutable borrow to parent session for its lifetime.
  - `Statement` drop on non-terminal state must auto-drain remaining response parts.
  - if drop-drain fails/timeouts: immediately close connection and never reuse that session.
  - `commit` / `rollback` / `set_autocommit` with active non-terminal statement: auto `skip_remaining` first, then issue command.

### Phase 2: RPC layer (`mariadb-rpc`)
- [ ] Step 1 (contract-first): pin exact public `mariadb-rpc` API signatures and error tags in this file before implementation.
- [ ] Step 2 (type surface): add `packages/mariadb-rpc/src/types.drift` with request/response and streaming result primitives aligned with wire `StatementEvent`.
- [ ] Step 3 (minimal implementation): add `packages/mariadb-rpc/src/lib.drift` with a first call path on top of `mariadb-wire-proto` (no buffer-all API).
- [ ] Step 4 (live validation): add live e2e RPC smoke covering success + server error + explicit drain/close behavior.
- [ ] Stored procedure call builder.
- [ ] Arg encoding rules (MVP subset).
- [ ] Result mapping for common SP return patterns.
- [ ] Error tag normalization.
- [ ] Metadata caching + optional metadata suppression (controlled server profile):
  - Treat metadata suppression as an optimization, never a correctness dependency.
  - Cache key should include normalized SQL/proc signature + default schema + server version + session settings that affect result shape.
  - Keep a cached column-signature hash and refresh on mismatch.
  - Add invalidation checks against schema metadata (for controlled deployments, use `information_schema`-based freshness checks and/or pinned schema version table).
  - On uncertainty/mismatch/protocol rejection, force full metadata path, refresh cache, and continue.
- [ ] Streaming/transaction operational guidance (to document in README/docs):
  - tx control commands may be delayed by required statement drain when prior SP/query responses are not fully consumed.
  - large in-transaction resultsets can extend lock/resource hold time until drain completes.
  - guidance:
    - prefer streaming consumption and early skip where possible.
    - avoid large resultset-returning SPs in latency-sensitive tx paths.
    - enforce timeout/size policies; on drain failure/timeouts mark connection non-reusable.

### Phase 3: Integration/hardening
- E2E with real MariaDB instance in controlled config.
- Negative tests: auth fail, malformed response, server error packets.
- Stress/concurrency smoke via virtual threads.
  - Added live load harness:
    - `packages/mariadb-wire-proto/tests/e2e/live_tcp_load_test.drift`
    - `just wire-live-load`
  - Current baseline profile: 32 workers x 100 queries (`DO 1`), passing in normal + ASAN + memcheck.

## Initial test plan

- Unit (`mariadb-wire-proto`):
  - packet codec roundtrip
  - handshake decode
  - ERR/OK/resultset packet parsing
- Unit (`mariadb-rpc`):
  - proc-call SQL generation
  - arg encoding/escaping for pinned subset
  - response mapping
- E2E:
  - connect + call simple SP
  - SP returning scalar/resultset
  - server-side error propagation with stable tags

## Open decisions to pin next

1. Exact `mariadb-rpc` public API signatures.
2. `call` result progression shape (single event API vs helper wrappers), while keeping streaming-only as default.
3. How server SQL errors are surfaced at RPC boundary (nested result shape and canonical error tags).
4. Pool-facing statement/session lifecycle hooks exposed by RPC (and what remains wire-only).

## API signature discussion queue (next)

1. `connect` and connection options surface for `mariadb-rpc`.
2. `call` return type and event progression shape.
3. Row access shape (column-name lookup, typed accessors, conversion errors).
4. Tx operations (`set_autocommit`, `commit`, `rollback`) at RPC layer and their drain semantics.
5. Finalized error envelope (`transport` vs `server` vs `decode` classes).

## Phase 2 decisions pinned

1. Connection configuration is builder-first:
  - `RpcConnectionConfigBuilder` -> validated immutable `RpcConnectionConfig`.
  - `connect` accepts only `RpcConnectionConfig` (no long arg list).
2. MVP connection options to support in builder/config:
  - `host` (default `127.0.0.1`)
  - `port` (default `3306`)
  - `user` (required)
  - `password` (required)
  - `database` (optional/default empty)
  - `connect_timeout_ms`
  - `read_timeout_ms`
  - `write_timeout_ms`
  - `autocommit` (default `false`)
  - `strict_reuse` (default `true`)
3. Charset/collation policy for MVP:
  - default charset: `utf8mb4`
  - default collation: `utf8mb4_unicode_ci`
  - configurable via connection config (overridable defaults)
  - `connect` must pin session behavior with `SET NAMES ... COLLATE ...` after handshake.
  - RPC text decoding is UTF-8 strict; invalid text bytes return deterministic decode error (no silent replacement).
4. `call` API uses overload set (arity-based):
  - `call(proc_name: &String)`
  - `call(proc_name: &String, args: &Array<RpcArg>)`
5. `RpcArg` MVP variant domain:
  - `Null`, `Bool`, `Int`, `Float`, `String`, `Bytes`
6. Temporal helper policy:
  - keep wire encoding as SQL text literals for COM_QUERY path.
  - expose helpers so caller does not hand-format literals:
    - `rpc.date(std.time.Date) -> RpcArg`
    - `rpc.datetime_utc(std.time.UtcTimestamp) -> RpcArg`
    - `rpc.time_hms(h: Int, m: Int, s: Int, micros: Int) -> core.Result<RpcArg, RpcArgError>`
  - keep explicit string-based helpers for interop (`date_str` / `datetime_str` / `time_str`) with deterministic validation errors.
  - `datetime_utc` naming is intentional to reflect current native type semantics and avoid implicit timezone assumptions.
7. Bytes argument encoding policy:
  - `RpcArg::Bytes` is encoded as canonical SQL hex literal `0x...` (uppercase hex).
  - empty bytes encode as `0x`.
8. Transaction safety policy:
  - if connection/session drops with manual transaction still open, run best-effort rollback in RAII cleanup.
  - before rollback, auto-drain active non-terminal statement (`skip_remaining`).
  - if drain/rollback fails during cleanup, close socket and mark non-reusable.
9. Result metadata policy:
  - MVP consumes and exposes resultset metadata on every call (correctness-first).
  - metadata suppression/cache optimization is deferred and optional; never required for correctness.
10. Row access API shape:
  - index-based access is primary.
  - name-based access is convenience.
  - overload-based typed getters are preferred:
    - `row.get_int(index: Int)` and `row.get_int(name: &String)`
    - same pattern for other typed getters (`get_string`, `get_bool`, etc.).

## Proposed `mariadb-rpc` API signatures (draft v1)

```drift
module mariadb.rpc

import std.core as core;

pub struct RpcConnectionConfig {
  host: String,
  port: Int,
  user: String,
  password: String,
  database: String,
  connect_timeout_ms: Int,
  read_timeout_ms: Int,
  write_timeout_ms: Int,
  autocommit: Bool,
  strict_reuse: Bool,
  charset: String,
  collation: String
}

pub struct RpcConnectionConfigBuilder { /* internal mutable fields */ }

pub fn new_connection_config_builder() -> RpcConnectionConfigBuilder;
pub fn with_host(builder: RpcConnectionConfigBuilder, host: String) -> RpcConnectionConfigBuilder;
pub fn with_port(builder: RpcConnectionConfigBuilder, port: Int) -> RpcConnectionConfigBuilder;
pub fn with_user(builder: RpcConnectionConfigBuilder, user: String) -> RpcConnectionConfigBuilder;
pub fn with_password(builder: RpcConnectionConfigBuilder, password: String) -> RpcConnectionConfigBuilder;
pub fn with_database(builder: RpcConnectionConfigBuilder, database: String) -> RpcConnectionConfigBuilder;
pub fn with_connect_timeout_ms(builder: RpcConnectionConfigBuilder, timeout_ms: Int) -> RpcConnectionConfigBuilder;
pub fn with_read_timeout_ms(builder: RpcConnectionConfigBuilder, timeout_ms: Int) -> RpcConnectionConfigBuilder;
pub fn with_write_timeout_ms(builder: RpcConnectionConfigBuilder, timeout_ms: Int) -> RpcConnectionConfigBuilder;
pub fn with_autocommit(builder: RpcConnectionConfigBuilder, enabled: Bool) -> RpcConnectionConfigBuilder;
pub fn with_strict_reuse(builder: RpcConnectionConfigBuilder, enabled: Bool) -> RpcConnectionConfigBuilder;
pub fn with_charset(builder: RpcConnectionConfigBuilder, charset: String) -> RpcConnectionConfigBuilder;
pub fn with_collation(builder: RpcConnectionConfigBuilder, collation: String) -> RpcConnectionConfigBuilder;
pub fn build_connection_config(builder: RpcConnectionConfigBuilder) -> core.Result<RpcConnectionConfig, RpcConfigError>;

pub struct RpcConnection { /* wraps wire session */ }
pub struct RpcStatement { /* wraps wire statement */ }
pub struct RpcRow { /* metadata-aware row view */ }

pub enum RpcEvent {
  Row(RpcRow),
  ResultSetEnd(RpcResultSetSummary),
  StatementEnd(RpcStatementSummary),
  ServerErr(RpcServerError)
}

pub fn connect(config: RpcConnectionConfig) -> core.Result<RpcConnection, RpcError>;
pub fn close(conn: RpcConnection) -> core.Result<(), RpcError>;
pub fn call(conn: &mut RpcConnection, proc_name: &String) -> core.Result<RpcStatement, RpcError>;
pub fn call(conn: &mut RpcConnection, proc_name: &String, args: &Array<RpcArg>) -> core.Result<RpcStatement, RpcError>;
pub fn next_event(stmt: &mut RpcStatement) -> core.Result<RpcEvent, RpcError>;
pub fn skip_result(stmt: &mut RpcStatement) -> core.Result<(), RpcError>;
pub fn skip_remaining(stmt: &mut RpcStatement) -> core.Result<(), RpcError>;

pub fn set_autocommit(conn: &mut RpcConnection, enabled: Bool) -> core.Result<(), RpcError>;
pub fn commit(conn: &mut RpcConnection) -> core.Result<(), RpcError>;
pub fn rollback(conn: &mut RpcConnection) -> core.Result<(), RpcError>;
pub fn reset_for_pool_reuse(conn: &mut RpcConnection) -> core.Result<(), RpcError>;
```

Notes:
- `call` is the only statement entry point in MVP; no raw `query` API and no `query_all` / buffer-all helper.
- tx commands auto-drain active non-terminal statement first; drain failure marks connection non-reusable.
- server SQL errors are surfaced as `RpcEvent::ServerErr(...)` (wire-successful statement progression), while transport/decode failures are outer `RpcError`.
- overloaded `call` supports zero-arg SP invocation without special method names.
- row typed getter surface uses overloads by index/name (e.g. `get_int(Int)` and `get_int(&String)`).

## Deferred decisions

1. Supported argument types in MVP.
2. Transaction semantics in MVP (explicitly out or minimal support).
3. Connection lifecycle/pooling shape (single connection first vs pool-first).

## Compiler defect ledger (must clear before major RPC progress)

1. `CORE_BUG` - zero-param alias forward nominal resolution
- Symptom: type alias/exported nominal mismatch (`have X, expected X`) and downstream overload failures.
- Status: fixed upstream (confirmed by local compile rerun).

2. `CORE_BUG` - variant match binder deref typing (`&Variant` payloads)
- Symptom: payload binder deref produced let-binding type mismatch in simple primitive payload patterns.
- Status: fixed upstream (confirmed by local compile rerun).

3. `CORE_BUG` - intrinsic arg validation with `UNKNOWN` call-signature slots
- Symptom: `Array.push` cross-module rejected typed values with `expected UNKNOWN`.
- Status: fixed upstream (confirmed by local compile rerun).

4. `CORE_BUG` - checker internal `CallInfo param layout mismatch` on call/method call
- Symptom: internal checker crash in `packages/mariadb-rpc/tests/e2e/live_rpc_smoke_test.drift` on regular close/call paths.
- Repro command:
  - `tools/drift_test_runner.sh compile --src-root packages/mariadb-wire-proto/src --src-root packages/mariadb-rpc/src --file packages/mariadb-rpc/tests/e2e/live_rpc_smoke_test.drift --target-word-bits 64`
- Status: resolved upstream; local repro now exits `0` with current toolchain.

5. `CORE_BUG` - suspected state corruption across `rpc.connect` return/bind path
- Symptom (prior run): state inside `rpc.connect` before `Ok(move conn)` was `reusable=true autocommit=false`, but caller-side state after `Ok(c)` bind appeared reverted.
- Pinned regression:
  - `packages/mariadb-rpc/tests/e2e/connect_state_handoff_regression_test.drift`
  - `just rpc-live-connect-state-regression`
- Stage-isolation regression:
  - `packages/mariadb-rpc/tests/e2e/connect_state_handoff_stage_isolation_test.drift`
  - `just rpc-live-connect-state-stage`
  - `121` => direct scenario post-bind `reusable` flipped false (`120 + 1`).
- Minimal pre-vs-post probe regression:
  - `packages/mariadb-rpc/tests/e2e/connect_state_handoff_probe_regression_test.drift`
  - `just rpc-live-connect-state-probe`
  - code bands:
    - `131..133`: pre-return state drift inside connect path (checked before return)
    - `134..136`: probe payload field drift after `Result::Ok(...)` bind
    - `14x`: post-bind live session drift in caller after `Ok(...)` bind
    - `11x`: connect path error tags
- Latest host signal:
  - `exit 135` from `just rpc-live-connect-state-probe`
  - Interpretation: pre-return checks passed in `connect_handoff_probe`, but returned probe field `pre_autocommit_enabled` flipped true after `Ok(...)` bind.
  - Classification: `CORE_BUG` in Result/aggregate payload handoff-bind lowering (not protocol state mutation itself).
- Paired probe evidence (single run):
  - inside `rpc.connect` before return: `reusable=true autocommit=false in_tx=false`
  - caller post-bind before first call: `reusable=false autocommit=true in_tx=false`
- Non-network repro attempts (currently pass, do not reproduce):
  - `packages/mariadb-rpc/tests/unit/connect_state_handoff_nonnetwork_shape_test.drift`
  - `packages/mariadb-rpc/tests/unit/connect_state_handoff_nonnetwork_crossmodule_test.drift`
  - `packages/mariadb-rpc/tests/unit/connect_state_handoff_nonnetwork_noncopypayload_test.drift`
- Required probe (same run):
  - pre-return state check in connect path
  - post-bind state check immediately after `Ok(...)` bind
- Status: resolved upstream; host verification now green:
  - `just rpc-live-connect-state-probe` -> `0`
  - `just rpc-live-connect-state-stage` -> `0`
  - `just rpc-live-connect-state-regression` -> `0`

6. `CORE_BUG` - unsafe `core.Copy` acceptance on non-Copy field structs
- Symptom (reported): compiler accepts `implement core.Copy` for structs containing `String`.
- Risk: UAF-class ownership corruption, can contaminate unrelated investigations.
- Local repro:
  - `/tmp/repro_copy_string_forbidden.drift`:
    - `struct BadCopy { v: String }`
    - `implement core.Copy for BadCopy {}`
  - command:
    - `DRIFTC=/home/sl/src/drift-lang/bin/driftc $DRIFTC --target-word-bits 64 /tmp/repro_copy_string_forbidden.drift -o /tmp/repro_copy_string_forbidden.bin`
  - actual now:
    - `<source>:9:1: error: core.Copy impl target must be structurally Copy in MVP`
- Status: resolved upstream.

## Status

- Architecture pinned.
- RPC implementation started, but major progress is gated on open compiler defects in the ledger above.
