# Performance Baseline

Certification gate for wire-level performance regression. Part of the workspace certification contract (`just perf`).

## How it works

1. `just deploy` builds and signs both packages as `.zdmp` artifacts.
2. Perf scenarios are compiled against the deployed packages (not local source roots).
3. Each scenario runs through a wire-capture proxy that records bytes and packets.
4. Wire metrics are compared against a machine-pinned baseline.
5. If any metric regresses >5%, the gate fails.

## Compilation path

Scenarios compile via `--package-root build/deploy --dep mariadb-rpc@<version>`, consuming the signed `.zdmp` packages. This validates the same package consumption path that downstream consumers use.

## Machine identity

Baselines are keyed by `/etc/machine-id` (not hostname). This provides exact host pinning — hostname collisions or renames cannot blur the signal. The gate fails closed if no baseline exists for the current machine.

## Commands

```bash
just perf                  # run scenarios, gate against baseline
just perf-record-baseline  # record new baseline for this machine
```

## Preconditions

- `DRIFTC` points at the deployed compiler
- `drift` CLI on `PATH`
- `DRIFT_SIGN_KEY_FILE` set (required by `just deploy`)
- Local MariaDB instance up at `127.0.0.1:34114` with fixture schema loaded
- Local proxy port `127.0.0.1:34115` available
- Baseline recorded for this machine (`perf/baselines/<machine-id>.json`)

## Scenarios

- `rpc_single_result` — repeated `CALL sp_add(1, 2)` through `mariadb-rpc`
- `rpc_multi_result` — repeated `CALL sp_multi_rs()` through `mariadb-rpc`
- `rpc_error` — repeated `CALL sp_error()` through `mariadb-rpc`, expecting `ServerErr`

Each scenario runs 25 iterations with one connect/close pair.

## Gated metrics

| Metric | Signal | Gated? |
|--------|--------|--------|
| `bytes_written` | Wire payload size (client→server) | Yes (>5% = fail) |
| `bytes_read` | Wire payload size (server→client) | Yes (>5% = fail) |
| `packets_written` | Packet count (client→server) | Yes (>5% = fail) |
| `packets_read` | Packet count (server→client) | Yes (>5% = fail) |
| `elapsed_ms` | Wall-clock time | No (too noisy) |

## Result files

- Timestamped JSON: `perf/results/<timestamp>.json`
- Latest JSON copy: `perf/results/latest.json`
- Machine baselines: `perf/baselines/<machine-id>.json`
- Raw capture runs: `perf/captures/<scenario>/<run-id>/`

## Interpretation

Wire byte/packet counts are the stable signal for protocol changes. Wall-clock time will vary with system load.
