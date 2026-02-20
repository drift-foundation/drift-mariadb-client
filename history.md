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
- Updated `work-progress.md` checklist items as completed for:
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
- Updated `work-progress.md` (Phase 1.5) to mark live tx gate complete:
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
