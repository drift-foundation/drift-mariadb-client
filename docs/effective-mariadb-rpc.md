# Effective MariaDB RPC Usage

Audience: application developers using `mariadb-rpc`.

Status: living guide. Update as API stabilizes.

## Goals

- Provide practical guidance for calling stored procedures safely and efficiently.
- Keep wire-level mechanics out of app code.

## Core principles

- Prefer RPC API over wire API for normal application development.
- Treat stored procedure calls as streamed responses, not pre-buffered blobs.
- Keep transaction scope small and explicit.
- Return connections to pool only after reset/sanitization.
- Use `call(...)` as the statement entrypoint; consume via statement events.

## Statement model (streaming-first)

- `conn.call(...)` starts one statement and returns `RpcStatement`.
- Consume results incrementally with:
  - `stmt.next_event()`
  - `stmt.skip_result()`
  - `stmt.skip_remaining()`
- There is no `query_all`/buffer-all API in MVP.

## Single active statement rule

- One connection supports one active statement at a time.
- Practical implication:
  - finish a statement (consume/skip) before starting the next call.
- This is pool-friendly and avoids overlapping response streams on one socket.

## Drain semantics and pool safety

- Preferred explicit drain when you do not need all results:
  - `stmt.skip_result()` to jump to the next resultset.
  - `stmt.skip_remaining()` to finish the statement.
- If a statement is dropped before terminal event, wire-layer destruction drains remaining packets.
- Before returning a connection to pool, call:
  - `conn.reset_for_pool_reuse()`
- `reset_for_pool_reuse()` normalizes session state for next borrower:
  - rolls back open transaction if needed
  - restores `autocommit=1` if needed
  - verifies reusable state.

## Transactions

- For explicit transaction flow:
  - `conn.set_autocommit(false)`
  - one or more `conn.call(...)`
  - `conn.commit()` or `conn.rollback()`
- Keep transactions short when calls can produce large resultsets; stream/skip aggressively.

## Error model

- Transport/protocol failures are outer `RpcError`.
- Server SQL error packets are surfaced as `RpcEvent::ServerErr`.
- Handle both layers explicitly.

## Recommended usage pattern

1. Borrow connection.
2. Call procedure.
3. Stream events, processing rows you need.
4. Skip unneeded remainder (`skip_result`/`skip_remaining`).
5. Commit or rollback.
6. `reset_for_pool_reuse()`.
7. Return connection.

## Operational guidance

- Timeout strategy defaults.
- Retry guidance (what is safe to retry and when).
- Pool sizing and backpressure recommendations.
- Observability/metrics suggestions.

## Anti-patterns to avoid

- Implicitly relying on connection close for cleanup in pooled environments.
- Issuing large result-producing calls in latency-sensitive transactions.
- Assuming statement errors close the session.

## TODO: examples to add

- `call` with args and streamed row handling.
- Multi-resultset selective consume (`skip_result` then read next resultset).
- Partial consume then `reset_for_pool_reuse` path.
- Streaming-to-file example at RPC layer.
