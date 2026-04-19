# History

## 2026-02-17

### Repository/tooling foundations
- Added and iterated local MariaDB instance tooling via `tools/db_instance.sh` and `just` recipes.
- Hardened instance operations for safety:
  - strict instance-name validation (`^mdb[0-9]+-[a-z]$`)
  - canonical path-root guards for runtime/config directories
  - safer delete flow (guard checks and marker-file handling)
  - removed `source` usage for env files; replaced with explicit key parsing
  - switched compose invocation from command-string style to argv arrays
- Refactored transient layout to keep runtime/config under one project-local root:
  - `tmp_db_instances/<instance>/runtime`
  - `tmp_db_instances/<instance>/config`
- Improved idempotency for instance lifecycle commands (`create`, `up`, `down`) to avoid accidental destructive behavior.

### Documentation and developer UX
- Expanded `README.md` with:
  - dependency section
  - local MariaDB dev instance usage
  - naming/port scheme
  - safety notes
  - protocol reference links
  - build/test flag notes (`DRIFT_ASAN`, `DRIFT_MEMCHECK`, `DRIFT_MASSIF`, `DRIFT_ALLOC_TRACK`)
- Added/updated `justfile` recipes for DB workflows and wire-proto validation.
- Reworked wire validation UX to avoid giant inline shell command output:
  - introduced `tools/drift_test_runner.sh`
  - moved wire compile/run logic into reusable script modes
  - kept just recipes as thin wrappers

### Wire protocol package (`packages/mariadb-wire-proto`) progress

#### Core packet and handshake coverage
- Implemented packet header encode/decode:
  - `src/packet/header.drift`
- Implemented lenenc integer/string codecs:
  - `src/packet/lenenc.drift`
- Implemented handshake hello decode:
  - `src/handshake/hello.drift`
- Implemented handshake response encode:
  - `src/handshake/auth.drift`
- Added protocol constants module:
  - `src/protocol/constants.drift`
- Added base types and error model:
  - `src/types.drift`
  - `src/errors.drift`

#### COM_QUERY + response discrimination
- Implemented COM_QUERY payload encode:
  - `src/command/com_query.drift`
- Implemented first-response routing (OK vs ERR vs resultset header):
  - `src/decode/resultset.drift` (`discriminate_first_response`)

#### OK/ERR decode
- Implemented OK packet decode:
  - `src/decode/ok_packet.drift`
  - supports header, affected rows, last insert id, status flags, warnings
- Implemented ERR packet decode:
  - `src/decode/err_packet.drift`
  - supports error code, optional SQLSTATE marker/state, UTF-8 error message

#### Resultset decode (initial deterministic MVP path)
- Implemented initial text-resultset packet-sequence decode:
  - `src/decode/resultset.drift` (`decode_text_resultset_packets`)
  - handles:
    - column-count packet
    - fixed number of column-definition packets (opaque for now)
    - text-row decode with lenenc strings and null marker
    - EOF terminator detection (`0xFE` and payload length heuristic)
- Added resultset data types:
  - `ResultSetCell` variant
  - `ResultSetDecoded` struct

#### Public exports wiring
- Updated package facade in `src/lib.drift` to export/bridge implemented APIs and types as they were added.

### Tests added and expanded
- Added executable unit tests (with module + main entrypoints):
  - `tests/unit/packet_header_test.drift`
  - `tests/unit/lenenc_test.drift`
  - `tests/unit/handshake_decode_test.drift`
  - `tests/unit/handshake_auth_test.drift`
  - `tests/unit/com_query_test.drift`
  - `tests/unit/response_discriminator_test.drift`
  - `tests/unit/ok_packet_test.drift`
  - `tests/unit/err_packet_test.drift`
  - `tests/unit/resultset_decode_test.drift`
- Kept compile/run checks integrated through `just wire-check` and `just wire-check-unit ...`.

### Drift toolchain integration notes observed during implementation
- Updated local recipes/runner to align with toolchain constraints and defaults:
  - explicit `--entry <module>::main` for non-`main` module test files
  - compile phase unsets runner-only flags (`DRIFT_MEMCHECK`, `DRIFT_MASSIF`) so they apply only at execution time
- Confirmed archive runtime linking path in active use (`libdrift_rt.a` variants) in wire checks.

### Compiler defects encountered and handled during integration
- Multiple toolchain-side defects were encountered and reported during this iteration (examples include internal tracebacks and diagnostics-quality gaps).
- After upstream fixes landed, wire tests progressed and remaining user-land updates were completed.
- Notable integration pattern followed:
  - stop on suspected core defect
  - isolate/reproduce
  - resume package work once compiler-side fix confirmed

### Progress tracking
- Updated `progress.md` checklist items as completed for:
  - COM_QUERY encode + first response discriminator
  - OK packet decode
  - ERR packet decode
  - associated unit tests

## 2026-02-18

### Fixture capture and replay workflow improvements
- Added automatic SQL transcript generation to fixture extraction flow:
  - `just wire-fixture-extract <scenario> <run-id>` now also runs `tools/write_scenario_sql.py`.
  - `scenario.sql` is written for both:
    - `tests/fixtures/packetized/<scenario>/<run-id>/`
    - `tests/fixtures/scenarios/bin/<scenario>/<run-id>/`
- Updated `README.md` capture docs to describe:
  - wire-capture usage
  - packetized extraction outputs
  - `scenario.sql` auto-generation behavior
  - file layout for raw and packetized artifacts

### New captured transaction scenarios
- Added and extracted new transaction fixture runs:
  - `tx_manual_commit_sp_chain`
  - `tx_manual_rollback_sp_chain`
  - `tx_error_then_rollback` (interactive capture preserving post-error rollback commands)
- Verified generated `scenario.sql` transcripts for each run and packetized counterpart.

### Schema updates for fixture scenarios
- Updated `tests/fixtures/appdb_schema.sql`:
  - included consolidated appdb reset + stored procedure definitions used by captures
  - added `sp_multi_rs()` returning two resultsets in one call
- Reapplied schema to local dev instance and verified procedure availability.

### Replay test coverage expansion
- Added new unit replay entrypoint:
  - `packages/mariadb-wire-proto/tests/unit/tx_fixture_replay_test.drift`
  - validates commit/rollback/error chains by decoding captured OK/ERR packets and response routing.
- Expanded existing SP fixture replay coverage:
  - `packages/mariadb-wire-proto/tests/unit/sp_fixture_replay_test.drift`
  - added multi-resultset replay assertions for `CALL sp_multi_rs()`:
    - first resultset decode
    - second resultset decode
    - trailing final OK packet decode

### Validation runs completed
- `just wire-check-unit packages/mariadb-wire-proto/tests/unit/tx_fixture_replay_test.drift` passed.
- `DRIFT_ASAN=1 just wire-check-unit packages/mariadb-wire-proto/tests/unit/tx_fixture_replay_test.drift` passed.
- `DRIFT_MEMCHECK=1 just wire-check-unit packages/mariadb-wire-proto/tests/unit/tx_fixture_replay_test.drift` passed (0 errors/leaks).
- `just wire-check-unit packages/mariadb-wire-proto/tests/unit/sp_fixture_replay_test.drift` passed after multi-resultset additions.
- `DRIFT_ASAN=1 just wire-check-unit packages/mariadb-wire-proto/tests/unit/sp_fixture_replay_test.drift` passed.
- `DRIFT_MEMCHECK=1 just wire-check-unit packages/mariadb-wire-proto/tests/unit/sp_fixture_replay_test.drift` passed (0 errors/leaks).

### Live transaction e2e added
- Added dedicated live transaction e2e entrypoint:
  - `packages/mariadb-wire-proto/tests/e2e/live_tcp_tx_test.drift`
- Added recipe:
  - `just wire-live-tx`
- Scenarios covered in one authenticated TCP session flow per scenario:
  - manual commit chain (`SET autocommit=0; CALL sp_1(); CALL sp_2(); COMMIT; SET autocommit=1;`)
  - manual rollback chain (`SET autocommit=0; CALL sp_1(); CALL sp_2(); ROLLBACK; SET autocommit=1;`)
  - error-then-rollback (`SET autocommit=0; CALL sp_1(); CALL sp_error(); ROLLBACK; SET autocommit=1;`)
  - multi-resultset procedure (`CALL sp_multi_rs();`) with explicit decode of:
    - first resultset
    - second resultset
    - trailing final OK packet

### Live e2e validation
- `just wire-live-tx` passed.
- `DRIFT_ASAN=1 just wire-live-tx` passed.
- `DRIFT_MEMCHECK=1 just wire-live-tx` passed (0 errors/leaks; expected virtual-thread stack-switch warnings from valgrind).

### Progress tracking updates
- Updated `progress.md` (Phase 1.5) to mark live tx gate complete:
  - added dedicated live tx e2e file reference
  - marked normal + ASAN + memcheck validation complete

## 2026-02-19

### Compiler integration: Result/variant payload handoff corruption closure
- Integrated latest toolchain fixes targeting aggregate payload handoff/bind corruption paths:
  - LLVM variant payload sizing corrected for forward/alias nominal recursive fields in `_size_align_typeid`.
  - Undersized `Result::Ok` aggregate payload storage issue addressed (post-bind state flip class).
  - Regressions added/kept upstream for:
    - forward-nominal variant payload sizing (`test_variant_payload_forward_nominal_size`)
    - match binder extraction
    - `CopyValue`/phi retain path
    - non-copy ref-scrutinee rejection

### Repository-side regression reruns
- Ran live handoff regressions with current compiler from this repo:
  - `just rpc-live-connect-state-probe`
  - `just rpc-live-connect-state-stage`
  - `just rpc-live-connect-state-regression`
- In this sandboxed environment, runs compile/link successfully but live connect path fails at runtime (`exit 11/111`) due environment access limits, so end-to-end host confirmation must be read from host runs.

### Non-network verification in-repo
- Executed non-network rpc handoff unit suite:
  - `tools/drift_test_runner.sh run-all --src-root packages/mariadb-wire-proto/src --src-root packages/mariadb-rpc/src --test-root packages/mariadb-rpc/tests/unit --target-word-bits 64`
- Result: pass.

### Host closure note
- Host-side repro closure target for this defect remains:
  - before fix signal: `EXIT:135` (`connect_state_handoff_probe_regression_test`)
  - after fix target: `EXIT:0`
- Host verification run (outside sandbox) completed:
  - `just rpc-live-connect-state-probe` -> `0`
  - `just rpc-live-connect-state-stage` -> `0`
  - `just rpc-live-connect-state-regression` -> `0`
  - summary: `probe=0 stage=0 regression=0`

## 2026-02-20

### Test workflow and docs
- Added top-level full-suite recipe:
  - `just test`
  - runs unit/compile-first checks through live DB e2e in a fixed order.
- Added `rpc-check` recipe for `packages/mariadb-rpc/tests/unit`.
- Updated `README.md` with:
  - `just test` section
  - execution order
  - required preconditions (`DRIFTC`, live local DB, fixture schema loaded).

### RPC API cleanup (probe-only surface removal)
- Removed temporary probe-only public API from `mariadb.rpc`:
  - removed `RpcConnectHandoffProbe`
  - removed `connect_handoff_probe(...)`
  - removed probe recipe from `justfile`.
- Kept connect-state regressions that validate behavior via existing public API paths.

### Ownership fix for non-Copy wire error payload
- Enforced structural-Copy policy in wire types:
  - removed invalid `core.Copy` impl for `ErrPacket` (contains `String` fields).
- Updated pending error event extraction to non-Copy-safe ownership path:
  - `packages/mariadb-wire-proto/src/lib.drift` now uses `mem.replace(...)` to extract `statement.pending_err` before returning `StatementEvent::StatementErr(...)`.
- This avoids illegal projected-place moves and copy violations under MVP ownership rules.

### RPC streaming/pool-safety slice (event-stream model hardening)
- Expanded live RPC e2e coverage in `packages/mariadb-rpc/tests/e2e/live_rpc_smoke_test.drift`:
  - `scenario_multi_resultset_selective_skip`
    - validates selective multi-resultset consumption using:
      - `stmt.skip_result()` (skip first resultset)
      - `stmt.next_event()` (consume second resultset rows)
      - `stmt.skip_remaining()` (drain terminal remainder).
  - `scenario_partial_consume_then_reset`
    - validates partial consume + explicit drain + pool reset path:
      - consume a subset of rows
      - `skip_remaining()`
      - `reset_for_pool_reuse()`
      - verify session state normalized for reuse (`reusable=true`, `autocommit=true`, `in_tx=false`).
- Updated `docs/effective-mariadb-rpc.md` from placeholder to concrete guidance:
  - streaming-first statement model
  - single-active-statement rule
  - explicit drain semantics
  - pool reset lifecycle contract and error layering notes.

### Validation
- `just rpc-check` passed.
- `just rpc-live` (outside sandbox) passed with structured `std.log` JSON events emitted on stderr.

## 2026-02-23

### Wire proto cleanup rounds completed (`packages/mariadb-wire-proto`)
- Round 1 (#4, #6): server error detail propagation.
  - `PacketDecodeError` now carries `server_error_code` and `server_message`.
  - Tx command error path (`_query_expect_ok`) now preserves decoded `ErrPacket` details.
  - Auth reject path now decodes ERR packets and surfaces server message/code.
- Round 2 (#3, #7): clean shutdown path.
  - Added `src/command/com_quit.drift`.
  - `close()` now sends `COM_QUIT` best-effort before socket close.
- Round 3 (#2, #2b): sequence tracking cleanup and validation.
  - Removed dead `next_sequence_id`.
  - Added `expected_seq_id` and session-aware packet I/O:
    - `_session_read_packet()` validates incoming sequence IDs.
    - `_session_write_packet()` stamps/advances sequence IDs.
    - `_session_reset_seq()` resets command boundary sequencing.
- Round 4 (#12): column metadata surfaced in streaming API.
  - Added `ColumnDef` and `Statement.column_defs`.
  - Added `_decode_column_def()` and accessors:
    - `statement_column_defs(&Statement)`
    - `statement_column_count(&Statement)`
- Round 5 (#15, #16, #8): pool reuse hardening and liveness checks.
  - Added `state.reusable` guards to `set_autocommit` / `commit` / `rollback`.
  - Added `COM_PING` support (`src/command/com_ping.drift`, `ping()`).
  - `reset_for_pool_reuse()` now performs ping before declaring reusable.
- Round 6 (#22, #23): column-def decode hardening.
  - Column-def decode failures now propagate and mark session dead.
  - Added 0x0C fixed-length marker validation.
  - Corrected bad-marker error offset to report byte position (`fixed_start`).
- Round 7 (#1, #17): lenenc bounds correction + decode dedup.
  - Fixed off-by-one bounds checks in lenenc decode (`2/3/8` byte variants).
  - Consolidated duplicated row decode logic:
    - canonical `decode_resultset.decode_text_row(...)`
    - removed duplicate helper from `lib.drift`.

### Validation
- `just test` passed (wire-check, rpc-check, and live/e2e recipes green in host workflow).

### Round 8 (#9, #9b): COM_RESET_CONNECTION for pool reset

Created `src/command/com_reset_connection.drift` (same pattern as com_ping/com_quit).

Added `reset_connection()` to `lib.drift`:
- Same guard pattern as `ping()` (closed/reusable/active_statement checks)
- Sends COM_RESET_CONNECTION (byte 31), reads response
- OK (0x00): applies status flags, returns Ok
- ERR (0xFF) with error code 1047 (`ER_UNKNOWN_COM_ERROR`): returns `"reset-connection-unsupported"` (server doesn't support command)
- ERR (0xFF) with other error codes: returns `"reset-connection-server-err"` with full server error details
- ERR decode failure: returns generic `"reset-connection-server-err"` (defensive)
- Other: marks session dead, returns `"reset-connection-unexpected-response"`

Added `server_capabilities: Uint` and `reset_connection_supported: Bool` to `WireSession`:
- `server_capabilities` stored from `hello.capabilities` during connect
- `reset_connection_supported` initialized `true`, set `false` only on `"reset-connection-unsupported"` response

Updated `reset_for_pool_reuse()`:
- Primary path: if `reset_connection_supported`, try `reset_connection()`. On success, early return (OK proves liveness, no ping needed).
- On unsupported: set `reset_connection_supported = false`, fall through to legacy path.
- On real server error: propagate directly to caller.
- On fatal failure (session dead): bail out immediately.
- Legacy fallback: ROLLBACK + SET autocommit=1 + ping (unchanged from before).

Key design decisions:
- ERR response from `reset_connection` does NOT mark session dead. An ERR packet is a valid protocol response; the stream remains synchronized.
- Only error code 1047 triggers the "unsupported" classification and disables future reset attempts. Real server errors are surfaced, not masked.
- Capability-bit pre-gate: won't-fix — no standard capability bit exists for COM_RESET_CONNECTION; reactive probe-and-disable is the standard approach.
- Explicit unsupported-fallback regression test: deferred until replay/mock harness exists.

### Validation
- `just test` — all pass (wire-check 14/14, rpc-check 4/4, all live/e2e green).

### Tracking and execution-order updates
- Marked `#9.x` as closed in todo/progress tracking with explicit resolution notes:
  - capability-bit pre-gate for `COM_RESET_CONNECTION`: won't-fix
  - explicit unsupported-fallback regression in full flow: deferred until replay/mock harness
- Set next execution order to start with protocol state-machine foundation, then close:
  - `#11` capability flags validation/normalization
  - `#19` WireConnectOptions design-layer cleanup
  - `#13` max payload size cap
  - `#14` timeout clamp policy/documentation
  - `#20` hex fixture policy

### State-machine audit (pre-design report)

Audit of `lib.drift` to map implicit states, transitions, and guard patterns before designing the state-machine foundation.

Session-level states (implicit, derived from WireSession fields):
- **Connecting**: before `connect()` returns — no `WireSession` exists yet.
- **Ready**: `is_closed=false`, `reusable=true`, `active_statement=false`.
- **Busy** (statement active): `active_statement=true`.
- **Not-reusable**: `reusable=false`, `is_closed=false`.
- **Dead/Closed**: `is_closed=true` (implies `reusable=false`).

Statement-level states (already explicit via MODE_* constants): MODE_RESULTSET (3), MODE_PENDING_OK (1), MODE_PENDING_ERR (2), MODE_NEED_NEXT_FIRST (4), MODE_DONE (5).

Guard pattern (repeated 6 times in `query`, `set_autocommit`, `commit`, `rollback`, `ping`, `reset_connection`): 3-check preamble (is_closed → "session-closed", !reusable → "session-not-reusable", active_statement → "active-statement-present"). `reset_for_pool_reuse` skips reusable check; `close` only checks is_closed.

Failure semantics (two categories): transport/protocol failure → `_mark_dead` → permanently dead; server-level ERR → session alive, stream synchronized.

Key observations:
1. Guard pattern was the main centralization candidate (6 copies → `_require_ready`).
2. Statement mode was already a well-structured state machine.
3. Session state is implicit but simple (4 states from 3 booleans).
4. `reset_for_pool_reuse` is the most complex transition (multi-step with fallback).
5. `_mark_dead` is the catch-all terminal transition.

### State-machine foundation implementation

Three phases, each independently verifiable via `just test`. No public API changes.

Phase 1 (transition regression tests): pinned guard and transition behavior in `tests/e2e/live_session_state_test.drift` covering closed/not-reusable/active-statement guards, Busy→Ready, drop auto-drain, and reset-for-reuse.

Phase 2 (guard/command centralization): extracted `_require_ready`, `_begin_command`, `_command_send_recv` in `lib.drift`. Normalized query()'s error tag from "statement-already-active" to "active-statement-present" (intentional diagnostics change).

Phase 3 (transport extraction): moved packet I/O to `src/transport.drift`. `lib.drift` calls through via import.

### State-machine foundation slice completed
- Added dedicated live transition regression coverage:
  - `packages/mariadb-wire-proto/tests/e2e/live_session_state_test.drift`
  - scenarios cover closed/not-reusable/active-statement guards, Busy->Ready transition, drop auto-drain, and reset-for-reuse normalization.
- Centralized session guard/command patterns in `packages/mariadb-wire-proto/src/lib.drift`:
  - `_require_ready(...)`
  - `_begin_command(...)`
  - `_command_send_recv(...)`
- Normalized active-statement guard diagnostics so `query()` aligns with other guarded commands:
  - now uses `"active-statement-present"` instead of `"statement-already-active"`.
- Extracted packet transport operations into:
  - `packages/mariadb-wire-proto/src/transport.drift`
  - `lib.drift` now routes packet read/write/sequence helpers via transport module.
- Added live recipe integration:
  - `just wire-live-state`
  - included in top-level `just test` flow.

### Validation
- `just wire-check` passed for the state-machine batch.

## 2026-02-24

### Wire protocol cleanup closures (#11, #19, #13)

- Implemented capability normalization in new module:
  - `packages/mariadb-wire-proto/src/capabilities.drift`
  - `normalize_capabilities(requested, server, has_database)` now:
    - forces required protocol flags
    - enables `CLIENT_MULTI_RESULTS` by default
    - strips unsupported flags (`LOCAL_FILES`, `PS_MULTI_RESULTS`, `SESSION_TRACK`)
    - intersects against server-advertised capabilities
    - returns deterministic errors for missing required server support
- Wired normalization into connect path:
  - `packages/mariadb-wire-proto/src/lib.drift` now normalizes `opts.client_capabilities` before handshake response encode.
- Added protocol constants and charset naming cleanup:
  - `packages/mariadb-wire-proto/src/protocol/constants.drift`
  - `DEFAULT_CHARSET_ID` export in wire proto and adoption across live tests/RPC connect paths.
- Added capability regression coverage:
  - `packages/mariadb-wire-proto/tests/unit/capability_normalization_test.drift`
- Added explicit max-payload defensive guard in transport read paths:
  - `packages/mariadb-wire-proto/src/transport.drift`
  - returns `wire-payload-too-large` when payload exceeds header max.

### Tracking/docs synchronization

- Updated `work/proto-cleanup/progress.md` with #11/#19 closure details and #13 completion notes.
- Updated `work/proto-cleanup/todo.md` to remove closed #11/#19/#13 items and set next active item to #14 timeout semantics policy/documentation.

### Test runner transition: parallel runner is now default

- `just test` now runs the parallel-compile/serial-run flow previously under `test-par`.
- All test recipes now use `tools/drift_test_parallel_runner.sh`.
- Removed legacy runner `tools/drift_test_runner.sh`.
- Confirmed execution model:
  - recipe-level flow stays serial in `just test`
  - compile fan-out is parallel only inside `run-all`
  - `run-one` recipes remain compile+run serial.

### #14 timeout policy closure and duration helper dedup

- Closed timeout-semantics item with clamp-only policy (no sentinel semantics).
- Deduplicated timeout conversion helper:
  - removed duplicate `_duration_ms` from `packages/mariadb-wire-proto/src/lib.drift`
  - promoted `packages/mariadb-wire-proto/src/transport.drift` helper to exported `duration_ms(...)`
  - updated wire-proto call sites to use `transport.duration_ms(...)`
- Documented timeout contract:
  - `WireConnectOptions` timeout fields must be `> 0`
  - values `<= 0` are clamped to `1ms` as defense-in-depth for direct wire-proto callers
  - RPC layer remains strict (`> 0` validation in config builder).

## 2026-03-09

### Deployed toolchain validation (`~/opt/drift/current`)

- Revalidated the repo as an external package consumer against deployed Drift toolchain `0.27.17-dev` (ABI 4).
- Confirmed the build uses the signed stdlib package from:
  - `~/opt/drift/current/lib/stdlib/std.dmp`
  - signature verified from `std.dmp.sig` against the deployed trust store
- Confirmed runtime linking uses only deployed prebuilt archives from:
  - `~/opt/drift/current/lib/runtime`
- Confirmed deployed wrapper isolation:
  - `PYTHONSAFEPATH=1`
  - no references to local `~/src/drift-lang` in the build chain
- Final result:
  - full project test suite passes against the deployed toolchain
  - package-consumer convergence is validated for this repo with no local toolchain fallback

## 2026-03-19

### Package/deploy modernization

- Added first-class package metadata and signing inputs:
  - `drift-manifest.json`
  - `drift-lock.json`
  - `the-drift-foundation.author-profile`
- Split the repo into two publishable co-artifacts:
  - `mariadb-wire-proto`
  - `mariadb-rpc`
- Added local package lifecycle workflow:
  - `just prepare`
  - `just deploy`
- Updated the test runner to derive local co-artifact source roots and external package deps from the manifest rather than hardcoded source-root wiring.
- Added package-version tracking in the manifest for both artifacts and co-artifact dependency linkage.

### Toolchain compatibility updates

- Updated all Drift module declarations to current syntax with trailing `;`.
- Added `create_connect_options()` factory helper in `mariadb-wire-proto` and updated `mariadb-rpc` to use it for cross-package compatibility on current package/toolchain rules.

### Documentation

- Added package/deploy workflow documentation to `README.md`.
- Added consumer-facing integration documentation:
  - `docs/integration-guide.md`

## 2026-03-21

### Certification gate standardization

- Standardized the public certification surface to:
  - `just test`
  - `just stress`
  - `just perf`
- `just test` now composes:
  - plain run
  - `DRIFT_ASAN=1`
  - `DRIFT_MEMCHECK=1`
- Added RPC stress coverage in:
  - `tests/stress/rpc_stress_test.drift`
  - covers connection churn, pool-reuse cleanup, multi-result draining patterns, transaction-boundary cycling, and error/success contamination checks under concurrent virtual-thread load.
- Promoted perf from informational output to certification gate:
  - machine-keyed baselines under `perf/baselines/`
  - fail-closed behavior on unknown machines
  - wire-metric regression checks for bytes/packet counts

### Signed-package certification path

- Updated `just stress` and `just perf` to compile against deployed signed `.zdmp` packages rather than local source roots.
- Made `just stress` and `just perf` depend on `just deploy` so certification validates the publishable package surface, not only local source behavior.

### Toolchain-root contract

- Updated the public certification gates to require `DRIFT_TOOLCHAIN_ROOT`.
- Certification commands now resolve tooling exclusively from:
  - `$DRIFT_TOOLCHAIN_ROOT/bin/drift`
  - `$DRIFT_TOOLCHAIN_ROOT/bin/driftc`
- Kept lighter-weight source-level workflows separate as non-certification developer paths.

### Trust and deploy behavior

- Documented and retained the repo trust/deploy prerequisites needed for signed-package certification flows.
- Aligned package-consumption perf/stress workflows with exact-machine baseline enforcement using `/etc/machine-id`.

## 2026-04-02

### Artifact kind alignment

- Updated both published artifacts in `drift-manifest.json` from legacy `kind: package` to `kind: library` to match the current toolchain lane/model terminology.
- Bumped:
  - `mariadb-wire-proto` to `0.1.4`
  - `mariadb-rpc` to `0.1.4`
- Updated the co-artifact dependency in `mariadb-rpc` to `mariadb-wire-proto@0.1.4`.

## 2026-04-18

### Drift 0.28.x / ABI 10 toolchain readiness

- Reviewed the upcoming Drift 0.28.x release line, which introduces runtime ABI 10 and reworks `Arc<Interface>` so an interface face shares the control block and refcount of the originating `Arc<Concrete>`.
- New supported construction pattern: allocate the concrete service first, then derive the interface face from the same `Arc` allocation:
  - `conc.arc(Concrete(...)).as_interface<type Interface>()`
  - direct `conc.arc<type Interface>(...)` and `conc.arc(interface_value)` are now rejected with `E_ARC_OF_INTERFACE_DIRECT`.
- Audited the repo for affected call sites: no uses of `conc.arc`, `as_interface`, `ContextResolver`, or `LoggerConfigBuilder.context_resolver`. No source migration required for either co-artifact; ABI 10 promotion is a rebuild-only change.

### `std.log::config_builder` leak (regression in 0.28.0, fixed in 0.28.1)

- The `just test` memcheck gate failed against staged `drift-0.28.0+abi10` with a single 24-byte `definitely lost` block attributed to `std.log::config_builder` → `drift_alloc_array`.
- Built a minimal repro (`config_builder()` + `build()`, no sinks/levels/resolvers) and confirmed:
  - leaks under `drift-0.28.0+abi10` (24 bytes, 1 block)
  - clean under certified `drift-0.27.202+abi9` (same source, same valgrind invocation, 6/6 alloc/free)
  - clean under staged `drift-0.28.1+abi10` (6/6 alloc/free, matches ABI 9 baseline)
- Per repo defect policy, treated this as a `CORE_BUG` rather than masking it in the library. Repro bundle handed off at `/tmp/drift-log-builder-leak/` (sources, prebuilt binaries against all three toolchains, valgrind outputs, README).
- Fix landed in staged `drift-0.28.1+abi10` (git `2a5a735d`). 0.28.0 should be skipped for promotion; 0.28.1 is the candidate.

### Version bump

- Bumped both published artifacts in `drift/manifest.json` to mark ABI 10 readiness:
  - `mariadb-wire-proto` to `0.1.5`
  - `mariadb-rpc` to `0.1.5`
- Updated the co-artifact dependency in `mariadb-rpc` to `mariadb-wire-proto@0.1.5`.
