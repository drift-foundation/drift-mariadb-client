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
- [ ] Implement resultset decode (column count, column definitions, row values, terminator handling) in `packages/mariadb-wire-proto/src/decode/resultset.drift`.
- [ ] Add fixture-driven parser tests:
  - `packages/mariadb-wire-proto/tests/unit/ok_packet_test.drift`
  - `packages/mariadb-wire-proto/tests/unit/err_packet_test.drift`
  - `packages/mariadb-wire-proto/tests/unit/resultset_decode_test.drift`
  - `packages/mariadb-wire-proto/tests/fixtures/resultset/*.hex`

6. Wire error model
- [ ] Define stable wire-layer error tags and payloads in `packages/mariadb-wire-proto/src/errors.drift`.
- [ ] Ensure decode/auth code paths return structured errors (no ad-hoc strings) across:
  - `packages/mariadb-wire-proto/src/handshake/auth.drift`
  - `packages/mariadb-wire-proto/src/decode/ok_packet.drift`
  - `packages/mariadb-wire-proto/src/decode/err_packet.drift`
  - `packages/mariadb-wire-proto/src/decode/resultset.drift`
- [ ] Add unit tests for error mapping:
  - `packages/mariadb-wire-proto/tests/unit/error_tags_test.drift`

7. Real-DB smoke validation against local instance tooling
- [ ] Add smoke harness:
  - `packages/mariadb-wire-proto/tests/e2e/com_query_smoke_test.drift`
- [ ] Validate: connect/auth/query success and server-side SQL error decode.
- [ ] Keep this as controlled-config E2E only (no TLS, no pooling).

8. Phase 1 exit criteria
- [ ] Packet/handshake/OK/ERR/resultset unit tests green with fixed binary fixtures.
- [ ] E2E smoke green against local MariaDB instance.
- [ ] No RPC/SP call-surface code introduced in `mariadb-wire-proto`.

### Phase 2: RPC layer (`mariadb-rpc`)
- Stored procedure call builder.
- Arg encoding rules (MVP subset).
- Result mapping for common SP return patterns.
- Error tag normalization.

### Phase 3: Integration/hardening
- E2E with real MariaDB instance in controlled config.
- Negative tests: auth fail, malformed response, server error packets.
- Stress/concurrency smoke via virtual threads.

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
2. Supported argument types in MVP.
3. Transaction semantics in MVP (explicitly out or minimal support).
4. Connection lifecycle/pooling shape (single connection first vs pool-first).

## Status

- Planned and pinned at architecture level.
- Implementation not started.
