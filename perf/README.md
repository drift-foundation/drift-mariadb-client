# Performance Baseline

This directory holds the lightweight local performance workflow for protocol/RPC regression spotting.

## Goals

- Keep a small, repeatable baseline for common stored-procedure usage.
- Track wire payload metrics separately from elapsed time.
- Make before/after comparisons easy when changing wire or RPC behavior.

## Run

```bash
just perf
```

Preconditions:

- `DRIFTC` points at the deployed compiler.
- Local MariaDB instance is up at `127.0.0.1:34114`.
- Fixture schema is loaded (`just db-load-schema mdb114-a`).

## Scenarios

- `rpc_single_result`
  - one connection, repeated `CALL sp_add(1, 2)` through `mariadb-rpc`
- `rpc_multi_result`
  - one connection, repeated `CALL sp_multi_rs()` through `mariadb-rpc`
- `rpc_error`
  - one connection, repeated `CALL sp_error()` through `mariadb-rpc`, expecting `ServerErr`

Each scenario currently runs 25 iterations and includes one connect/close pair.

## Result files

- Timestamped JSON: `perf/results/<timestamp>.json`
- Latest JSON copy: `perf/results/latest.json`
- Raw capture runs: `perf/captures/<scenario>/<run-id>/`

## Result shape

```json
{
  "generated_at": "2026-03-10T23:15:00Z",
  "toolchain": {
    "driftc": "/home/sl/opt/drift/current/bin/driftc",
    "version": "driftc 0.27.24-dev | abi 5 | ..."
  },
  "target": {
    "host": "127.0.0.1",
    "port": 34114,
    "proxy_port": 34115
  },
  "scenarios": [
    {
      "name": "rpc_single_result",
      "iterations": 25,
      "elapsed_ms": 18,
      "bytes_written": 1100,
      "bytes_read": 1400,
      "packets_written": 30,
      "packets_read": 34,
      "capture_duration_ms": 17,
      "capture_run_dir": "perf/captures/rpc_single_result/20260310-231500-12345"
    }
  ]
}
```

## Interpretation

- Use these numbers for regression spotting, not absolute performance claims.
- Compare runs on the same machine and local DB setup.
- Wall-clock time will vary; wire byte/packet counts are usually the most stable signal for protocol changes.
