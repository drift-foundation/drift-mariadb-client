# Effective MariaDB Wire Protocol Usage

Audience: advanced users working directly with `mariadb-wire-proto`.

Status: living guide. Update as API stabilizes.

## Scope

- Low-level protocol session usage.
- Streaming statement/result consumption.
- Pool-safe drain/reset semantics.

## Architecture overview

### Package structure

```
mariadb-wire-proto/src/
  lib.drift              — Package facade. Session/statement lifecycle, pool reuse, auth.
  types.drift            — All wire-level type definitions.
  errors.drift           — PacketDecodeError + error tag constants.
  transport.drift        — Packet-level I/O (read/write with framing and sequence tracking).
  capabilities.drift     — Client capability flag negotiation.
  protocol/
    constants.drift      — Handshake field sizes, charset ID, CLIENT_* flag values.
  packet/
    header.drift         — 4-byte packet header encode/decode.
    lenenc.drift         — Length-encoded integer/string/bytes codec.
  handshake/
    hello.drift          — Initial Handshake Packet decoder (protocol v10).
    auth.drift           — HandshakeResponse41 encoder.
  command/
    com_query.drift      — COM_QUERY payload encoder.
    com_ping.drift       — COM_PING payload encoder.
    com_quit.drift       — COM_QUIT payload encoder.
    com_reset_connection.drift — COM_RESET_CONNECTION payload encoder.
  decode/
    ok_packet.drift      — OK packet decoder (0x00 header).
    err_packet.drift     — ERR packet decoder (0xFF header).
    resultset.drift      — First-response routing + text-row decoder.
```

### Connection flow

```
Client                          Server
  |--- TCP connect -------------->|
  |<-- Initial Handshake Packet --|  (server version, capabilities, scramble)
  |--- HandshakeResponse41 ------>|  (capabilities, auth token, database)
  |<-- OK / ERR / AuthSwitch ----|  (auth result)
  |                               |
  |--- COM_QUERY (SQL) ---------->|  (query/call/set/commit)
  |<-- OK / ERR / ResultSet ------|  (response stream)
  |                               |
  |--- COM_QUIT ----------------->|  (graceful close)
```

## Core mental model

- One TCP stream carries sequential packets for all statements on a session.
- You cannot safely issue/interpret next command responses until current statement is fully drained.
- Streaming is the default; no eager full-result aggregation in core API.

## Session state machine

```
                ┌──────────────────────────┐
                │         Ready            │
                │  (not closed, reusable,  │
                │   no active statement)   │
                └──────┬───────────────────┘
                       │ query() / ping() / commit() / etc.
                       ▼
                ┌──────────────────────────┐
                │          Busy            │
                │  (active_statement=true)  │
                │  Must drain before next  │
                └──────┬───────────────────┘
                       │ statement completes / skip_remaining
                       ▼
                ┌──────────────────────────┐
                │         Ready            │
                └──────────────────────────┘

  On transport error at any point:
                       │ _mark_dead()
                       ▼
                ┌──────────────────────────┐
                │          Dead            │
                │  (is_closed=true,        │
                │   reusable=false)        │
                │  All operations error.   │
                └──────────────────────────┘
```

Guards: `_require_ready()` enforces the Ready state before any command. `_begin_command()` combines the ready check with sequence ID reset.

## Statement event model

Events from `next_event()` in order within one resultset:

1. **`Row(cells)`** — one row of data. Repeat for all rows.
2. **`ResultSetEnd`** — current resultset exhausted. If the server's `MORE_RESULTS_EXISTS` flag is set, another resultset follows (back to step 1). Otherwise, a terminal event follows.
3. **`StatementEnd(ok)`** — terminal. Statement complete, session released.
4. **`StatementErr(err)`** — terminal. Server SQL error, session released.

Statement modes (internal):

| Mode | Meaning |
|---|---|
| `MODE_RESULTSET (3)` | Reading column defs or data rows |
| `MODE_NEED_NEXT_FIRST (4)` | Multi-result: waiting for next result's first packet |
| `MODE_PENDING_OK (1)` | Next `next_event()` yields `StatementEnd` |
| `MODE_PENDING_ERR (2)` | Next `next_event()` yields `StatementErr` |
| `MODE_DONE (5)` | Terminal — no more events |

### RAII drain

When a `Statement` goes out of scope before reaching a terminal event, the `Destructible` impl calls `skip_remaining()` to drain all pending packets. If drain fails, the session is marked dead. This prevents protocol desync from leaked statements.

## API surface

### Session lifecycle

| Function | Precondition | Effect |
|---|---|---|
| `connect(opts)` | — | TCP connect, handshake, auth. Returns `WireSession`. |
| `close(session)` | Any state | Sends COM_QUIT, closes stream. Idempotent on closed sessions. |
| `session_state(session)` | Any | Returns `WireSessionState` snapshot. |
| `session_is_reusable(session)` | Any | `reusable && !closed && !active_statement`. |

### Statement operations

| Function | Precondition | Effect |
|---|---|---|
| `query(session, sql)` | Ready | Sends COM_QUERY, returns `Statement`. Sets `active_statement=true`. |
| `next_event(stmt)` | Active | Returns next `StatementEvent`. On terminal, releases session. |
| `skip_result(stmt)` | Active | Drains rows until `ResultSetEnd` or terminal. |
| `skip_remaining(stmt)` | Active | Drains everything until terminal event. |

### Transaction control

| Function | Precondition | Effect |
|---|---|---|
| `set_autocommit(session, enabled)` | Ready | Executes `SET autocommit=0/1`. |
| `commit(session)` | Ready | Executes `COMMIT`. |
| `rollback(session)` | Ready | Executes `ROLLBACK`. |

### Server management

| Function | Precondition | Effect |
|---|---|---|
| `ping(session)` | Ready | Sends COM_PING, expects OK. |
| `reset_connection(session)` | Ready | Sends COM_RESET_CONNECTION. |
| `reset_for_pool_reuse(session)` | Not closed, no active stmt | Two-tier reset (see below). |

## Capability negotiation

`normalize_capabilities()` computes the effective capability bitmask:

1. Force required flags: `PROTOCOL_41 | TRANSACTIONS | SECURE_CONNECTION | PLUGIN_AUTH | PLUGIN_AUTH_LENENC`.
2. Enable `MULTI_RESULTS` (needed for stored procedure multi-resultsets).
3. Strip unsupported: `LOCAL_FILES`, `PS_MULTI_RESULTS`, `SESSION_TRACK`.
4. Set `CONNECT_WITH_DB` if a database is specified.
5. Intersect with server capabilities.
6. Verify all required flags survived intersection.

## Authentication

Only `mysql_native_password` is supported. Auth flow:

1. Compute SHA1 token: `SHA1(password) XOR SHA1(scramble + SHA1(SHA1(password)))`.
2. Send in `HandshakeResponse41`.
3. If server responds with auth switch request (0xFE), re-compute token with new scramble and re-send.

## Pool reuse strategy

`reset_for_pool_reuse()` uses a two-tier approach:

1. **COM_RESET_CONNECTION** (preferred): single round-trip, server resets all session state. If server responds with error 1047 (unsupported), records this and falls through.
2. **Manual fallback**: `ROLLBACK` (if in transaction) → `SET autocommit=1` (if off) → `PING` (liveness check). Any failure marks session dead.

## Sequence ID tracking

Every MariaDB packet carries a 1-byte sequence ID. Within a command exchange:
- Client sends command packet with `seq=0`.
- Server responds with `seq=1`, `seq=2`, etc.
- `session_reset_seq()` resets to 0 at each new command boundary.
- `session_read_packet()` validates expected sequence; mismatch is fatal (protocol desync).

## Error tags

All errors are `PacketDecodeError` with a string `tag` for programmatic matching:

**Transport errors:**
- `wire-read-eof`, `wire-read-failed` — TCP read failure
- `wire-write-failed` — TCP write failure
- `wire-header-decode-failed`, `wire-header-negative-len`, `wire-payload-too-large` — Framing errors
- `wire-sequence-mismatch` — Sequence ID validation failure

**Handshake errors:**
- `handshake-truncated`, `handshake-invalid-protocol-version`, `handshake-missing-server-version-nul`
- `auth-rejected`, `auth-switch-unsupported-plugin`, `auth-invalid-response`
- `server-missing-required-capability`, `server-missing-connect-with-db`

**Command errors:**
- `session-closed`, `session-not-reusable`, `active-statement-present` — Guard failures
- `connect-failed`, `close-failed`, `ping-unexpected-response`
- `reset-connection-unsupported`, `reset-connection-server-err`
- `statement-consumed`, `statement-invalid-mode`

**Decode errors:**
- `short-ok-packet`, `invalid-ok-header`, `short-err-packet`, `invalid-err-header`
- `resultset-row-truncated`, `resultset-row-trailing-bytes`, `resultset-missing-terminator`
- `lenenc-out-of-bounds`, `lenenc-truncated`, `lenenc-null`

## Optional resultset metadata suppression

`connect()` negotiates `MARIADB_CLIENT_CACHE_METADATA` with the server via MariaDB extended capabilities. When the server agrees, it may omit column-definition packets and the column-def EOF terminator from resultsets, reducing per-resultset wire overhead.

**How it works:**

1. During handshake, the client advertises `MARIADB_CLIENT_CACHE_METADATA` in the extended capabilities field of HandshakeResponse41 (bytes 19-22 of the 23-byte reserved area).
2. The server advertises its support in the Initial Handshake Packet (bytes 6-9 of the 10-byte reserved area).
3. If both sides agree, the column-count packet gains an extra `metadata_follows` lenenc integer after the column count.
4. Per-resultset, the client inspects `metadata_follows`:
   - **1 (present)**: consume column defs + EOF terminator as normal.
   - **0 (suppressed)**: skip directly to row data. Column count is still available; column defs array is empty.

**Packet savings per resultset (N columns):**

| Path | Packets before rows |
|---|---|
| Metadata present | 1 (count) + N (coldefs) + 1 (EOF) = N+2 |
| Metadata suppressed | 1 (count) = 1 |

For a 2-resultset stored procedure with 3 columns each, this saves 8 packets per call.

**Session field:** `metadata_suppression_negotiated` (Bool) on `WireSession` tracks whether the capability was successfully negotiated for the current session.

**Correctness guarantee:** The client never assumes metadata is suppressed. It reads the `metadata_follows` flag from every column-count packet and branches accordingly. A server that sends metadata despite negotiation will be handled correctly.

**Impact on callers:**
- `statement_column_count()` always returns the correct count.
- `statement_column_defs()` returns an empty array when metadata is suppressed.
- Row decoding is unaffected (uses column_count, not column_defs).
- RPC-layer index-based row access works unchanged.

**Benchmarking:** The wire-capture proxy (`just wire-capture`) can be used to compare actual bytes/packets on the wire. Manual-handshake tests (which pass `mariadb_ext_capabilities=0`) exercise the metadata-present baseline; high-level `connect()` tests exercise the suppressed path.

## Performance and memory guidance

- Stream rows as consumed by caller.
- Internal read-ahead may exist, but must stay bounded.
- Do not materialize full resultsets by default.

## Transaction + resultset implications

- Large in-transaction resultsets can delay commit/rollback due to required drain.
- This can extend lock/resource hold duration on server side.
- Prefer small tx result payloads or explicit skip paths when possible.
