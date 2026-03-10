# TODO

Current state:

- Wire-proto cleanup items `#1`, `#9`, `#11`, `#13`, `#14`, `#17`, and `#19` are complete; see [history.md](/home/sl/src/drift-mariadb-client/history.md) entries on 2026-02-23 and 2026-02-24.
- RPC Phase 2 contract/design work is complete; the final contract is captured in [work/rpc-api-finalize/plan.md](/home/sl/src/drift-mariadb-client/work/rpc-api-finalize/plan.md), with implementation/tests/docs in repo.

## Wire protocol remaining
- None currently tracked here.

## Phase 2: RPC layer (`mariadb-rpc`)

- [x] Wire-level metadata suppression via `MARIADB_CLIENT_CACHE_METADATA` — negotiated at connect, per-resultset fallback in column-count packet. Eliminates column-definition packets and EOF terminator when server omits metadata. Live-validated against MariaDB 11.4.
- [ ] RPC-level metadata caching — cache column defs by SP name so repeated calls skip name resolution. Separate from wire suppression; depends on column-def availability for name-based row access.

## Phase 3: Integration/hardening

- [ ] Negative tests: auth fail, malformed response, server error packets
- [ ] Stress/concurrency smoke via virtual threads (baseline exists: 32 workers x 100 queries)
- [ ] Expand fixture corpus: capture -> packetize -> deterministic replay tests
