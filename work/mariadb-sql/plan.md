# mariadb-sql — pipelined prepared-statement SQL layer

**Status: PLANNED — not started. Awaiting green light before any implementation.**
Origin: PhaseDrift milestone-1 request for direct parameterized SQL alongside the SP-oriented `mariadb-rpc`.
Last design revision: 2026-06-07.

This document captures the full design and all decisions so work can resume later without re-deciding anything. Nothing here is implemented yet.

---

## Context

PhaseDrift (typed workflow runtime, sibling of Singular, consumer of `mariadb-rpc` 0.6) needs direct single-statement parameterized SQL for its business-record path — one opaque-JSON row per record, replaced whole — interleaved with stored-proc `call()`s on the SAME connection inside one explicit transaction (`set_autocommit(false) … commit()/rollback()`).

Their four statements (single-statement, fully parameterized, fixed SQL text on their side):
1. `SELECT … FROM tb_pd_record WHERE record_path = ? FOR UPDATE` — locking read.
2. `SELECT …` plain — validated read / advisory peek.
3. `UPDATE tb_pd_record SET value_json = ?, revision = revision + 1 WHERE record_path = ? AND revision = ?` — whole-value replace with optimistic revision predicate.
4. `INSERT INTO tb_pd_record (…) VALUES (…)` — create, PK uniqueness as the nonexistence claim.

Today `RpcConnection` exposes only `call(proc_name, args)` (`_build_call_sql` constructs CALL text exclusively). There is no way to issue a non-CALL statement.

## Owner decisions (FIXED — do not relitigate)

1. **Pipelined prepare+execute, no client-side substitution/lexer/rendering.** Write `COM_STMT_PREPARE`(sql with `?` markers) + `COM_STMT_EXECUTE`(stmt_id `0xFFFFFFFF`, binary params) back-to-back, then read both responses. One network round trip per execution. SQL text and binary parameter values stay separate; the server parses placeholders under its own sql_mode; `?` inside literals/comments is the server's concern. An explicit unsupported-server error — **never** fall back to textual interpolation.
2. **`mariadb-rpc` is untouched** — public scope, semantics, and version (0.6.0) unchanged, zero diffs. Generic SQL lives in a NEW package `mariadb-sql` backed by `mariadb-wire-proto`. Do not duplicate protocol state machines — shared internal wire machinery serves both text and binary paths.
3. **No `CLIENT_FOUND_ROWS` / `with_found_rows()` / ROW_COUNT() matrix.** PhaseDrift's UPDATE does `revision = revision + 1`, so changed-rows semantics already give `affected_rows == 1` on match and `0` on missing-record-or-revision-conflict. (FOUND-rows distinguishes predicate-matched from predicate-failed; it is not a substitute for command-level idempotency, which PhaseDrift owns.)
4. **One `exec()` API** — no query/exec split, no statement-builder.
5. Versions: `mariadb-wire-proto` 0.4.0 → **0.4.1** (additive); `mariadb-rpc` **0.6.0 unchanged**; new `mariadb-sql` **0.1.0**.
6. Certification: `just test` required green; attempt `just stress`/`just perf` (the 0.33.13 acquire-arity deploy blocker is **FIXED in toolchain 0.33.14**, so these are expected to pass); any failure other than that specific, documented signature is a real delivery blocker.
7. Deliverable includes a short draft reply to PhaseDrift.

### Explicitly REMOVED from scope (earlier draft, now obsolete)
`render_exec_sql`; client-side placeholder replacement; SQL lexer states; placeholder escaping rules; sql_mode caveats; client-side executable-comment handling; any public rendering API + its unit tests; any change to `mariadb-rpc` config; any `mariadb-rpc` version bump; `CLIENT_FOUND_ROWS`/`with_found_rows`; ROW_COUNT() / found-vs-changed test matrix.

---

## Protocol facts (verified against mariadb.com/docs binary-protocol pages)

- **COM_STMT_PREPARE** (`0x16`): payload `0x16` + SQL bytes. Response: prepare-OK = `0x00`(1) + statement_id(LE4) + column_count(LE2) + param_count(LE2) + reserved(1) + warnings(LE2); then if param_count>0: param-def packets + EOF; if column_count>0: column-def packets + EOF (EOFs present — this client never negotiates `CLIENT_DEPRECATE_EOF`). On failure: a single ERR packet.
- **COM_STMT_EXECUTE** (`0x17`): `0x17` + statement_id(LE4) + flags(1, `0` = `CURSOR_TYPE_NO_CURSOR`) + iteration_count(LE4 = 1) + if param_count>0: NULL bitmap `(n+7)/8` bytes at **offset 0** (bit i = param i is NULL) + send_types_flag(1) + if flag=1: per-param `[type byte, flag byte]` (flag bit 128 = unsigned; none of our six shapes is unsigned) + binary values. stmt_id `-1`/`0xFFFFFFFF` = "last statement prepared on this connection **if no COM_STMT_PREPARE has failed since**". Pipelining (prepare+execute(-1) then read both) is explicitly documented. **No capability bit** gates this → gate on server version (MariaDB ≥ 10.2).
- **Binary resultset row**: `0x00`(1) + NULL bitmap with **2-bit offset** (column i null iff bit `i+2`; size `(cols+9)/8`) + per-column binary values: TINY=1B; SHORT/YEAR=2B LE; INT24/LONG=4B LE; LONGLONG=8B LE (signedness from ColumnDef UNSIGNED_FLAG = bit value 32); FLOAT=4B IEEE LE; DOUBLE=8B IEEE LE; DECIMAL/NEWDECIMAL=lenenc string; VARCHAR/STRING/BLOB/JSON/ENUM/SET/BIT/etc.=lenenc bytes; DATE/DATETIME/TIMESTAMP=len byte (0/4/7/11)+Y(2)Mo(1)D(1)[h(1)mi(1)s(1)[micro(4)]]; TIME=len byte (0/8/12)+sign(1)+days(4)+h/mi/s(1 each)[+micro(4)]. Params in EXECUTE use the same encoding.
- **COM_STMT_CLOSE** (`0x19` + statement_id LE4): **no server response** → cleanup is round-trip-free. Close uses the REAL stmt id from the prepare-OK we drain (docs don't allow `-1` for close).

---

## Repo facts (verified)

- `RpcConnection.wire_session` is a **pub field** (mariadb-rpc lib.drift). Pool leases expose only `lease.conn() -> &mut rpc.RpcConnection` (managed.drift:147); `LeasedConn.conn` is `Optional<RpcConnection>` taken only by the release path — **connections can never be moved out of a lease**. Therefore the exec engine MUST be callable on `&mut rpc.RpcConnection`, not only on an owning wrapper.
- Wire `Statement` state machine (mariadb-wire-proto lib.drift ~710–836) is row-format-agnostic except ONE call site, `decode_text_row` at ~796 — that is the sharing seam. `ResultSetCell` / `StatementEvent` are matched **exhaustively** in mariadb-rpc (8 match arms), so they must NOT gain variants → binary rows travel through NEW types.
- `transport.expected_seq_id` is pub; two command packets can be framed (each seq 0) and written back-to-back before any read.
- `HandshakeHello.server_version` is decoded but dropped; `WireSession` must retain it (additive field; precedent: `metadata_suppression_negotiated` added at patch level, commit cf5119d).
- CACHE_METADATA ext-cap is already negotiated (`negotiate_mariadb_ext_capabilities`, value 16); text path honors `metadata_follows=0` via `_decode_column_count`. The binary path must mirror this AND retain prepare-response column defs (binary rows are undecodable without column types).
- `CLIENT_PS_MULTI_RESULTS` is deliberately stripped in capabilities.drift; `CLIENT_MULTI_STATEMENTS` is never requested (multi-statement text is rejected by the server at prepare).
- `tools/emit_test_plan.py`: `UNIT_ROOTS` is a hardcoded list (new package dir must be added); `LIVE_TESTS` is curated (every live test registered explicitly); `infer_artifact()` needs the `packages/mariadb-sql/` prefix; `deployed_dep_flags()` needs `mariadb-sql` once a perf scenario imports it. `justfile` `author-claim` loop hardcodes the two current artifacts — add `mariadb-sql`.
- Versioning convention (history.md + cf5119d): additive ⇒ patch bump; minor reserved for breaking changes.
- 0.33.13 acquire-arity deploy blocker: **FIXED in 0.33.14** (work/acquire-timeout/TOOLCHAIN-VERDICT-overload-publish-boundary.md). Memory `project_deploy_acquire_blocker.md` is stale and should be updated when work resumes.

---

## Architecture decision (the main open question: SQL + SP on one owned connection without changing rpc)

**Free-function exec engine on `&mut rpc.RpcConnection` + a thin owning `SqlConnection` facade.** One implementation, two entry shapes:

```drift
// packages/mariadb-sql/src/lib.drift, module mariadb.sql
pub struct SqlConnection { pub inner: rpc.RpcConnection }

// THE engine — also the pool-lease entry point:
pub fn exec(conn: &mut rpc.RpcConnection, sql: &String, args: &Array<rpc.RpcArg>) nothrow
    -> core.Result<SqlStatement, SqlError>      // reaches conn.wire_session (pub field)

pub fn connect(config: rpc.RpcConnectionConfig) -> core.Result<SqlConnection, SqlError>  // delegates rpc.connect
pub fn from_rpc(conn: rpc.RpcConnection) -> SqlConnection
pub fn into_rpc(conn: SqlConnection) -> rpc.RpcConnection

implement SqlConnection {
    exec(sql) / exec(sql, args) -> SqlStatement        // = mariadb.sql.exec(&mut self.inner, …)
    call(proc) / call(proc, args) -> rpc.RpcStatement  // delegate — SP path stays rpc's text protocol
    set_autocommit / commit / rollback / ping / close / is_alive  // delegate to rpc
}
```

**Rationale against the four constraints:**
- *No rpc changes*: everything reaches rpc through its existing public surface (`wire_session` pub field, `call`, tx methods). rpc stays 0.6.0, zero diffs.
- *Pool compatibility*: pooled consumers can't move a conn out of a lease, so the load-bearing piece is the free function `sql.exec(c, …)` on the `&mut RpcConnection` from `lease.conn()`, alongside `c.call(…)` / `c.commit()`. `SqlConnection`'s methods are one-line delegations to it. `from_rpc/into_rpc` cover the owned-migration case.
- *No duplicated state machines*: `exec` calls one new wire entry point (`wire.exec_prepared`) that reuses the existing `Statement` machinery via an internal behavior-neutral refactor; mariadb-sql contains zero packet logic; SP calls reuse rpc's existing text path.
- *Interleaving*: exec and call operate on the SAME `WireSession`; the existing `active_statement` discipline serializes statements, so `set_autocommit(false) → exec(SELECT..FOR UPDATE) → call(sp) → exec(UPDATE) → commit()` is straight-line.

**Argument vocabulary**: public API reuses **`rpc.RpcArg`** (one vocabulary for `exec` and `call`). wire-proto must not depend on rpc, so wire gets its own `StmtParam` variant; mariadb-sql converts `RpcArg → StmtParam` (6 trivial arms).

**Rejected alternatives:** (B) free functions only — loses `conn.exec()` UX and likely blocked by orphan rule for inherent `implement` on a foreign type (no in-repo precedent; confirm with a 5-min spike, design doesn't depend on it). (C) `SqlConnection` owns a `WireSession` and runs SPs as prepared `CALL ?` — needs `PS_MULTI_RESULTS` (deliberately stripped), binary multi-result handling, and duplicates connect/SET NAMES/budget machinery for no PhaseDrift benefit.

---

## Step 0 — Live probes (settle unknowns BEFORE building)

`packages/mariadb-wire-proto/tests/spike/pipelined_probe_test.drift` (spike dir not globbed by gates; run via `just check-one …`), hand-rolled over transport primitives against mdb114-a:
- **P1**: pipelined prepare+execute(-1) happy path; capture a ≥9-column SELECT to pin binary NULL-bitmap length/offset.
- **P2**: response sequence-ids under pipelining (assumed each response restarts at seq 1 after per-command seq-0 framing).
- **P3**: exact behavior/error code of execute(-1) after a failed prepare (expected ER_UNKNOWN_STMT_HANDLER 1243).
- **P4**: prepare/execute response metadata under negotiated CACHE_METADATA (does prepare always carry column defs; does execute send `metadata_follows=0`).
- **P5**: COM_STMT_CLOSE silence (ping immediately after; `Prepared_stmt_count` delta 0).
- Plus a compile spike: can a foreign package `implement` on rpc's type? (informational only).

If a probe contradicts a stated protocol/repo fact, update this doc before proceeding.

---

## 1. Wire-proto additions (mariadb-wire-proto 0.4.1 — all additive)

**types.drift**:
- `pub variant StmtParam { Null, Bool(value: Bool), Int(value: Int), Float(value: Float), String(value: String), Bytes(value: Array<Byte>) }`
- `pub struct PrepareOk { statement_id: Uint, num_columns: Int, num_params: Int, warnings: Uint }`
- `pub variant BinaryCell { Null, Int(value: Int), Uint(value: Uint), Float(value: Float), Bytes(value: Array<Byte>), DateTime(value: WireDateTime), Time(value: WireTime) }`
- `pub struct WireDateTime { year, month, day, hour, minute, second, micro: Int }`
- `pub struct WireTime { negative: Bool, days, hour, minute, second, micro: Int }`
- `pub struct PreparedStatement { pub inner: Statement, pub statement_id: Uint }` (composition → existing `Destructible` drain covers abandonment).
- `pub variant PreparedStatementEvent { Row(value: Array<BinaryCell>), ResultSetEnd, StatementEnd(value: OkPacket), StatementErr(value: ErrPacket) }`
- `WireSession` gains `pub server_version: String` (populated from `hello.server_version` in connect; sole construction site is lib.drift connect).

**New `src/command/com_stmt.drift`** (pattern: com_query.drift): command ids `0x16/0x17/0x19`; `STMT_ID_LAST_PREPARED: Uint = 4294967295`; `CURSOR_TYPE_NO_CURSOR: Byte = 0`; MYSQL_TYPE consts (NULL=6, TINY=1, LONGLONG=8, DOUBLE=5, VAR_STRING=253/STRING=254, BLOB=252). Encoders: `encode_com_stmt_prepare_payload(sql)`; `encode_com_stmt_execute_payload(stmt_id, params)` — NULL bitmap offset 0, send_types=1, Bool→TINY 0/1, Int→LONGLONG LE two's-complement, Float→DOUBLE IEEE LE, String/Bytes→lenenc (reuse packet/lenenc.drift), Null→bitmap only; `encode_com_stmt_close_payload(stmt_id)`.

**New `src/decode/binary_resultset.drift`**: `decode_prepare_ok(payload)`; `decode_binary_row(payload, &Array<ColumnDef>) -> Array<BinaryCell>` — null bit `i+2`, bitmap `(cols+9)/8`; v1 type coverage: TINY/SHORT/INT24/LONG/YEAR→Int (sign/zero-extend per UNSIGNED_FLAG=32); LONGLONG→Int or Uint; FLOAT/DOUBLE→Float; DECIMAL/NEWDECIMAL→Bytes (server text); string/blob/json/enum/set/bit→Bytes (lenenc); DATE/DATETIME/TIMESTAMP→DateTime (lengths 0/4/7/11); TIME→Time (0/8/12); unknown → tag `binary-row-unsupported-column-type` (offset=col idx); truncation → `binary-row-truncated`. Byte comparisons via if/else (Drift `match` can't take literal patterns). New error tags also: `prepare-ok-malformed`, `pipelined-unsupported-server`, `prepare-metadata-missing`. IEEE-754: if std lacks bit↔Float intrinsics, implement a contained software (de)composer here, unit-tested against known vectors.

**State-machine sharing (lib.drift refactor, behavior-neutral)**: extract `next_event` body into internal `_next_event_raw -> RawStatementEvent { RowPayload(Array<Byte>), ResultSetEnd, StatementEnd(OkPacket), StatementErr(ErrPacket) }`. Then:
- `next_event` = raw + `decode_text_row` (byte-identical existing behavior).
- new `prepared_next_event(&mut PreparedStatement)` = raw + `decode_binary_row(payload, &inner.column_defs)`.
- `skip_result` / `skip_remaining` rebased on raw (drop row payloads undecoded → makes Destructible drain correct for binary resultsets).
One machine, two row decoders, zero duplication.

**Transport**: `pub fn session_write_command_pair(session, p1, p2)` — frame both with seq 0, single write, leave `expected_seq_id = 1`.

**Pipelined entry point**:
```drift
pub fn exec_prepared(session: &mut WireSession, sql: &String, params: &Array<StmtParam>) nothrow
    -> core.Result<PreparedStatement, PacketDecodeError>
```
1. `_require_ready`; gate via pub helper `server_supports_pipelined(&session.server_version)` (strip optional `5.5.5-` prefix; require "MariaDB"; major.minor ≥ 10.2) → else `pipelined-unsupported-server`. No textual fallback, ever.
2. Encode both payloads; write as one command pair (single flush ⇒ one round trip). Write failure → `_mark_dead`.
3. Read prepare response first packet:
   - **ERR (prepare failed)**: decode prepare ErrPacket. Drain the execute response (reset seq, read; per docs `-1` is invalid after a failed prepare → deterministic ERR; defensively route anything else through `_init_statement_from_first` + raw-skip to terminal). Build `PreparedStatement` whose inner Statement is `MODE_PENDING_ERR` carrying the **prepare** error; `active_statement = true`; connection clean & reusable. No CLOSE (no statement exists). The "stmt -1 references an earlier leaked statement" hazard is structurally excluded: this fn is the ONLY prepared-statement producer and always closes before returning, so at most one server-side statement ever exists, inside this function.
   - **prepare-OK**: `decode_prepare_ok`; drain `num_params` param-defs + EOF; decode and **retain** `num_columns` column-defs + EOF (needed when the execute response suppresses metadata under CACHE_METADATA).
4. (OK path) Send `COM_STMT_CLOSE`(real stmt_id) NOW — stateless write, no response; server processes serially so it runs after execute completes → zero extra round trips; statement lifetime bounded inside this function.
5. Reset seq; read execute first packet; `active_statement = true`; construct `Statement` exactly as `query()` does (`_init_statement_from_first`). If it lands in `MODE_RESULTSET` with `column_defs_remaining == 0` (metadata suppressed), install retained prepare defs. Failures → `_mark_dead` + `active_statement = false`.
6. Return `PreparedStatement(inner, statement_id)`.

Multi-result note: single-statement SQL never sets MORE_RESULTS (we don't request PS_MULTI_RESULTS / MULTI_STATEMENTS); the shared machine still tolerates `MODE_NEED_NEXT_FIRST` defensively.

Exports added: StmtParam, PrepareOk, BinaryCell, WireDateTime, WireTime, PreparedStatement, PreparedStatementEvent, exec_prepared, prepared_next_event / prepared_skip_result / prepared_skip_remaining, server_supports_pipelined. Manifest `modules` list += the two new files.

## 2. mariadb-sql package (0.1.0)

`packages/mariadb-sql/src/lib.drift`, `module mariadb.sql;`, deps wire-proto "0.4" + rpc "0.6".
- `pub error SqlError { tag, message }` (tags `sql-*`; transport classifier = `sql-wire-` prefix).
- `SqlServerError { error_code, sql_state, message }`; `SqlStatementSummary { affected_rows, last_insert_id, status_flags, warnings }` (Copy); `SqlEvent { Row(SqlRow), ResultSetEnd, StatementEnd(..), ServerErr(..) }`; `SqlStatement { pub inner: wire.PreparedStatement }` with next_event/skip_result/skip_remaining mapping `PreparedStatementEvent` 1:1.
- `SqlRow { pub values: Array<wire.BinaryCell> }` — **typed** getters (vs RpcRow's text parsing): is_null, get_int (Uint-in-range coerces), get_uint, get_float (int widening ok), get_string (UTF-8 check → `sql-row-utf8-failed`), get_bytes, get_datetime, get_datetime_string (`YYYY-MM-DD HH:MM:SS[.ffffff]`), get_time. Type mismatch → `sql-row-type-mismatch` (index + actual kind); no silent coercions.
- `exec`: map RpcArg→StmtParam, call `wire.exec_prepared(&mut conn.wire_session, …)`, map errors (`pipelined-unsupported-server` → `sql-unsupported-server`, distinct & non-retryable; others → `sql-wire-<tag>`); `exec` no-args overload mirrors call().
- `SqlConnection` + delegations per architecture section.

Integration: manifest third artifact (kind library, entry lib.drift, assets incl. new docs); justfile `author-claim` loop += mariadb-sql; emit_test_plan.py: UNIT_ROOTS += `("mariadb-sql", "packages/mariadb-sql/tests/unit")`, `infer_artifact` += path prefix, LIVE_TESTS += sql block, `deployed_dep_flags` += mariadb-sql.

## 3. Tests

**Wire unit** (auto-globbed):
- `com_stmt_encode_test.drift`: prepare bytes; execute 0-param (no bitmap/types section); execute `[Null, Int, String]` → bitmap `0b001`, send_types=1, type bytes `[6,0][8,0][254,0]`, negative LONGLONG two's-complement, stmt id `FF FF FF FF`, flags 0, iteration `01 00 00 00`, Bytes→lenenc, Bool→TINY; bitmap edge at 8/9 params (1 vs 2 bytes); close payload `19 xx xx xx xx`.
- `prepare_ok_decode_test.drift`: happy field extraction; short packet; bad header.
- `binary_row_decode_test.drift`: every v1 type signed+unsigned (UNSIGNED_FLAG; unsigned LONGLONG > Int::MAX → Uint), FLOAT/DOUBLE IEEE vectors, NEWDECIMAL text, string/blob/json bytes, DATETIME 0/4/7/11, TIME 0/8/12 incl. negative, NULL bitmap offset-2 across ≥9 cols (2-byte bitmap), truncation errors, unsupported type → tag.
- `pipelined_gate_test.drift`: version matrix — `11.4.x-MariaDB` true, `5.5.5-10.6.7-MariaDB` true, `10.1.48-MariaDB` false, MySQL `8.0.36` false, garbage false.
- Regression: existing wire unit/fixture-replay tests pass unchanged after the `_next_event_raw` refactor.

**mariadb-sql unit** (DB-free): RpcArg→StmtParam (6 arms); SqlRow getters over hand-built cells (types, mismatch tags, null, oob, datetime rendering incl. micro/zero-date); error-tag mapping + transport classifier.

**Fixture** (`tests/fixtures/appdb_schema.sql`, additive): `tb_exec_test(id INT AUTO_INCREMENT PK, k VARCHAR(64) NOT NULL UNIQUE, v VARCHAR(255) NULL, revision INT NOT NULL DEFAULT 0)`; `tb_sql_types` (TINYINT/SMALLINT/INT/BIGINT/BIGINT UNSIGNED/FLOAT/DOUBLE/DECIMAL(12,4)/VARCHAR/VARBINARY/BLOB/DATE/DATETIME(6)/TIMESTAMP/TIME(6), nullable). **No rowcount SP.** Reload: `just db-load-schema mdb114-a`.

**Live** (all registered in LIVE_TESTS; conventions per existing live tests: 127.0.0.1:34114, scenario exit codes, rollback hygiene):
1. wire `live_stmt_pipeline_test.drift`: exec_prepared SELECT happy path under CACHE_METADATA (assert suppression negotiated yet rows still typed); prepare-failure drain then `wire.query("SELECT 1")` succeeds (sequence clean); param-count mismatch → execute ServerErr, connection usable; INSERT/UPDATE OK summaries; abandonment mid-rows → destructor drain → session reusable.
2. sql `live_sql_exec_test.drift`: PhaseDrift's four statement shapes verbatim against tb_exec_test — INSERT → affected_rows==1 & last_insert_id>0; SELECT by key; SELECT..FOR UPDATE in explicit txn; revision-guard UPDATE → 1 on match, 0 on stale revision and on missing key (changed-rows, no FOUND_ROWS).
3. sql `live_sql_types_test.drift`: params + typed getters round-trip all v1 types incl. NULLs, unsigned BIGINT, decimal text, datetime/time.
4. sql `live_sql_errors_test.drift`: dup key → ServerErr 1062 + sql_state, connection usable after; `"SELECT 1; SELECT 2"` → server rejects at prepare (no client lexing exists); syntax-error pipelined drain at sql layer.
5. sql `live_sql_interleave_test.drift` (the PhaseDrift contract): one SqlConnection — set_autocommit(false) → exec(SELECT..FOR UPDATE) → call("sp_add") → exec(revision UPDATE) → exec(INSERT) → commit, verify persisted; same with rollback, verify gone.
6. sql `live_sql_stmt_leak_test.drift`: `Prepared_stmt_count` before/after 50 mixed execs (success + server-error + prepare-failure) → delta 0.

**Pipelining/perf**: wire-capture (`just wire-capture`) — assert exactly one client flush carries PREPARE+EXECUTE; compare bytes/packets vs textual CALL baseline. Durable gate: `perf/scenarios/sql_exec_perf.drift` (N pipelined execs of the PhaseDrift UPDATE) registered in emit_test_plan.py `PERF_SCENARIOS` + tools/perf_baseline.py `SCENARIOS` (matching names) + `deployed_dep_flags`; `just perf-record-baseline` to seed (bytes/packets gated; elapsed excluded).

## 4. Docs / versions / bookkeeping

- New `docs/effective-mariadb-sql.md` (modeled on effective-mariadb-rpc.md): pipelined execution + one-round-trip claim, dual error model, pool-lease usage via the free-function form, `sql-unsupported-server` (never textual fallback), typed getters vs RpcRow, the changed-rows note (PhaseDrift's revision-increment makes affected_rows==1/0 exactly predicate-matched/failed; FOUND-rows neither needed nor offered), multi-MB write-write caveat.
- `docs/integration-guide.md`: mariadb-sql section + interleaving example.
- `drift/manifest.json`: wire-proto → **0.4.1** (rpc's "0.4" range keeps resolving), rpc **0.6.0 unchanged**, mariadb-sql **0.1.0**.
- `history.md` dated entry; `TODO.md` line. Update stale memory `project_deploy_acquire_blocker.md` (verdict: fixed in 0.33.14).
- **No git operations** (owner commits); `just reseal` owner-run (signing key).

## 5. Ordered execution steps (when work resumes)

0. Probes P1–P5 + implement-on-foreign-type spike (`just check-one packages/mariadb-wire-proto/tests/spike/pipelined_probe_test.drift`; DB up). Reconcile any contradictions into this doc.
1. Wire encoders/decoders + types + error tags + wire unit tests. Verify: `just compile-check packages/mariadb-wire-proto/src/lib.drift`; `just check-one <each unit test>`.
2. `_next_event_raw` refactor + raw-based skips + `WireSession.server_version` + `session_write_command_pair`. Verify: all existing wire unit tests via check-one + wire live smoke + metadata-suppression live test.
3. `exec_prepared` + PreparedStatement + gating; `live_stmt_pipeline_test.drift` registered + green; re-run `live_rpc_smoke_test.drift` (proves rpc untouched).
4. Package scaffolding (manifest, emit_test_plan.py, justfile author-claim). Verify: `python3 tools/emit_test_plan.py test` lists sql jobs; `just compile-check packages/mariadb-sql/src/lib.drift`.
5. mariadb-sql implementation + unit tests.
6. Fixture additions + `just db-load-schema mdb114-a`; live tests 2–6 registered + green.
7. Wire-capture verification + perf scenario + `just perf-record-baseline`.
8. Docs, history, TODO, memory update.
9. Certification: `just test` (plain+ASAN+memcheck — REQUIRED green); `just stress`; `just perf` (expected to pass — 0.33.13 issue fixed in 0.33.14). If and ONLY if stress/perf fail with the documented acquire-overload publish-boundary signature, capture exact output and report blocked-pending-toolchain; any other failure blocks delivery. No git; owner runs `just reseal` + commits.

## 6. Risks / unknowns (each probe-gated)

- CACHE_METADATA × prepared statements (P4) — design retains prepare defs and honors the flag both ways.
- Pipelined response sequence numbering (P2) — assumes each response restarts at seq 1.
- stmt `-1` after failed prepare (P3) — doc guarantees invalidity; exact code pinned by probe; drain path written defensively to terminal regardless of packet kind.
- Binary NULL-bitmap length wording (`(cols+7)/8` vs `(cols+9)/8` with 2-bit offset) — pinned by P1 capture vectors.
- IEEE-754 conversion availability in Drift std — software fallback contained + unit-tested.
- `5.5.5-` version prefix — handled in gate; pinned against existing handshake fixture.
- Version-gate floor 10.2 (no capability bit exists) — explicit error makes a wrong floor fail loudly, not unsafely.
- TCP write-write deadlock only for multi-MB pipelined payloads (PhaseDrift statements are tiny) — documented limitation.
- Foreign-type inherent `implement` for `conn.exec()` sugar — presumed disallowed; design independent; spike confirms.

## 7. Deliverable: draft reply to PhaseDrift (to send when work is greenlit/shipped)

- Scope fits; shipping as a NEW package **mariadb-sql 0.1.0** (mariadb-rpc stays SP-oriented & unchanged). One API: `exec(sql, args)` — no query/exec split.
- Execution model: pipelined server-side prepare+execute (one round trip, binary params; no client-side interpolation — the server parses `?` under its own sql_mode, so `?` in literals/comments is handled server-side). Explicit error on non-MariaDB / pre-10.2 servers; never a textual fallback.
- Same-connection interleaving: `SqlConnection` owns the connection and offers exec + call (their SPs) + transaction controls; pooled leases work via the free-function form. Their commit/rollback grouping works as requested.
- Affected-rows: their revision-increment UPDATE makes changed-rows semantics exactly "predicate matched"=1 / "missing-or-conflict"=0 — FOUND_ROWS not needed; `StatementEnd` carries affected_rows + last_insert_id.
- Error codes: numeric `error_code` + sql_state on ServerErr (their 1062/1213/1205 classification works).
- Conservative surface confirmed: single statement per exec (multi-statement rejected by the server at prepare), parameterized args only; their four statements are covered verbatim in our live suite. We welcome their consumer-side conformance test against our branch.
