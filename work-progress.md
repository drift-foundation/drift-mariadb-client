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
