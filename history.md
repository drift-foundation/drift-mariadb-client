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
