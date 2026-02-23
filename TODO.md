# TODO

## Wire protocol remaining

- [ ] #9 `COM_RESET_CONNECTION` support
- [ ] #11 Capability flags validation/normalization
- [ ] #17 Consolidate duplicated `_decode_text_row`
- [ ] #19 `WireConnectOptions` design-layer cleanup (expose high-level names, hide raw bitmasks)
- [ ] #1 Lenenc bounds check clarity rewrite
- [ ] #13 Max payload size cap on read
- [ ] #14 `_duration_ms` clamp documentation
- [ ] Hex fixture files for unit tests (currently inline hex; `.hex` files deferred)

## Phase 2: RPC layer (`mariadb-rpc`)

- [ ] Step 1 (contract-first): finalize public `mariadb-rpc` API signatures and error-tag contract
- [ ] Step 2 (type surface): `packages/mariadb-rpc/src/types.drift` — request/response and streaming result primitives aligned with wire `StatementEvent`
- [ ] Step 3 (minimal implementation): `packages/mariadb-rpc/src/lib.drift` — first call path on top of `mariadb-wire-proto` (no buffer-all API)
- [ ] Step 4 (live validation): live e2e RPC smoke covering success + server error + explicit drain/close behavior
- [ ] Stored procedure call builder
- [ ] Arg encoding rules (MVP subset)
- [ ] Result mapping for common SP return patterns
- [ ] Error tag normalization
- [ ] Metadata caching + optional metadata suppression (optimization, never correctness dependency)
- [ ] Streaming/transaction operational guidance (document in README/docs)

## Phase 3: Integration/hardening

- [ ] Negative tests: auth fail, malformed response, server error packets
- [ ] Stress/concurrency smoke via virtual threads (baseline exists: 32 workers x 100 queries)
- [ ] Expand fixture corpus: capture -> packetize -> deterministic replay tests

## Pinned Phase 2 decisions

1. **Connection config**: builder-first (`RpcConnectionConfigBuilder` -> `RpcConnectionConfig`). `connect` accepts only `RpcConnectionConfig`.
2. **MVP connection options**: host, port, user, password, database, connect/read/write timeout_ms, autocommit (default false), strict_reuse (default true).
3. **Charset/collation**: default `utf8mb4` / `utf8mb4_unicode_ci`, configurable. `connect` pins session with `SET NAMES ... COLLATE ...`. UTF-8 strict decoding (no silent replacement).
4. **`call` API**: overloaded by arity — `call(proc_name)` and `call(proc_name, args)`.
5. **`RpcArg` variants**: `Null`, `Bool`, `Int`, `Float`, `String`, `Bytes`.
6. **Temporal helpers**: `rpc.date()`, `rpc.datetime_utc()`, `rpc.time_hms()` plus string-based `date_str`/`datetime_str`/`time_str` with validation.
7. **Bytes encoding**: `RpcArg::Bytes` -> SQL hex literal `0x...` (uppercase). Empty = `0x`.
8. **Transaction safety**: best-effort rollback in RAII cleanup, auto-drain active statement first, mark non-reusable on failure.
9. **Result metadata**: MVP exposes metadata on every call (correctness-first). Suppression/cache deferred.
10. **Row access**: index-based primary, name-based convenience. Overload-based typed getters (`get_int(Int)` / `get_int(&String)`).

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

## Open decisions

1. `call` result progression shape (single event API vs helper wrappers), while keeping streaming-only as default.
2. How server SQL errors are surfaced at RPC boundary (nested result shape and canonical error tags).
3. Pool-facing statement/session lifecycle hooks exposed by RPC (and what remains wire-only).
4. Connection lifecycle/pooling shape (single connection first vs pool-first).
