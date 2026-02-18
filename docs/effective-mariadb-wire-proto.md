# Effective MariaDB Wire Protocol Usage

Audience: advanced users working directly with `mariadb-wire-proto`.

Status: living guide. Update as API stabilizes.

## Scope

- Low-level protocol session usage.
- Streaming statement/result consumption.
- Pool-safe drain/reset semantics.

## Core mental model

- One TCP stream carries sequential packets for all statements on a session.
- You cannot safely issue/interpret next command responses until current statement is fully drained.
- Streaming is the default; no eager full-result aggregation in core API.

## API design intent (pinned)

- Session-level operations:
  - `connect`, `close`
  - `query`
  - `set_autocommit`, `commit`, `rollback`
  - `reset_for_pool_reuse`
- Statement-level operations (planned):
  - `next_event` (row/resultset boundary/statement terminal events)
  - `skip_result`
  - `skip_remaining`

## Safe-default behavior (pinned)

- `commit`/`rollback` auto-drain pending statement responses before issuing tx command.
- If drain fails or times out:
  - mark session non-reusable
  - return deterministic error

## Performance and memory guidance

- Stream rows as consumed by caller.
- Internal read-ahead may exist, but must stay bounded.
- Do not materialize full resultsets by default.

## Transaction + resultset implications

- Large in-transaction resultsets can delay commit/rollback due to required drain.
- This can extend lock/resource hold duration on server side.
- Prefer small tx result payloads or explicit skip paths when possible.

## Pooling guidance

- Before returning a session to pool:
  - ensure active statement is fully drained (or explicitly skipped)
  - ensure transaction/autocommit state is normalized
- On any drain/reset failure, discard session from pool.

## TODO: examples to add

- Statement event loop example.
- Skip-unneeded-resultset example.
- Commit after partially consumed statement (safe auto-drain).
- Pool reset sequence example.
