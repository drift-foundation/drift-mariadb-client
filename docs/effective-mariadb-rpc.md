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

## Recommended usage patterns (placeholder)

1. Connection lifecycle
- Acquire from pool.
- Execute one logical unit of work.
- Ensure connection is clean before returning to pool.

2. Stored procedure calls
- Prefer explicit procedure contracts.
- Consume or explicitly skip unneeded results.

3. Transactions
- Use explicit begin/commit/rollback flow exposed by RPC layer.
- Avoid long-running transaction calls that return large resultsets.

4. Errors
- Distinguish transport/protocol failures from server SQL errors.
- Handle deterministic error tags from RPC layer.

## Operational guidance to fill

- Timeout strategy defaults.
- Retry guidance (what is safe to retry and when).
- Pool sizing and backpressure recommendations.
- Observability/metrics suggestions.

## Anti-patterns to avoid

- Implicitly relying on connection close for cleanup in pooled environments.
- Issuing large result-producing calls in latency-sensitive transactions.
- Assuming statement errors close the session.

## TODO: examples to add

- Simple `call` success path.
- Error-handling path.
- Transaction path with rollback-on-error.
- Streaming-to-file example at RPC layer.
