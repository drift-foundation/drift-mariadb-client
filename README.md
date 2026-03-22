# mariadb-client

Drift user-land MariaDB client library.

Published as signed packages for normal third-party consumption. Not part of Drift stdlib. For integration into your project, see `docs/integration-guide.md`.

## Packages

1. `packages/mariadb-wire-proto`
- Low-level MariaDB wire protocol implementation.
- Packet codec, handshake/auth, command/response state machine.

2. `packages/mariadb-rpc`
- Stored-procedure-oriented client API on top of `mariadb-wire-proto`.
- Drift-friendly call and result mapping for app code.

## Scope (MVP)

- MariaDB server versions controlled by project.
- Basic auth mode(s) only.
- TLS disabled for MVP.
- Stored procedure workflow first (via `COM_QUERY` path).

## Documentation

- **Integration guide** (consuming this library from another project): `docs/integration-guide.md`
- RPC usage guide: `docs/effective-mariadb-rpc.md`
- Wire-proto usage guide: `docs/effective-mariadb-wire-proto.md`

## Protocol References

- MariaDB Client/Server Protocol (overview): https://mariadb.com/docs/server/reference/clientserver-protocol
- Packet format: https://mariadb.com/docs/server/reference/clientserver-protocol/0-packet
- Connection/handshake phase: https://mariadb.com/docs/server/reference/clientserver-protocol/1-connecting/connection
- MariaDB vs MySQL protocol differences: https://mariadb.com/docs/server/reference/clientserver-protocol/mariadb-protocol-differences-with-mysql
- Accessed: 2026-02-17

## Dependencies

- `bash`
- `just`
- `docker` with Compose support (`docker compose` or `docker-compose`)
- `mariadb` CLI client (used for schema loading, manual queries, and capture workflows)
- `drift` toolchain (`driftc` compiler + `drift` CLI for prepare/deploy)

### New machine check

Before bringing up a local MariaDB instance, verify both Docker and Compose are available:

- `docker --version`
- `docker compose version`

If `docker` exists but `docker compose` does not, install the Compose plugin before running `just db-up ...`.

Also verify your user can talk to the Docker daemon without `sudo`:

- `docker ps`

If Docker is installed but you get a permission error for `/var/run/docker.sock`, add your user to the `docker` group and start a new shell session:

- `sudo usermod -aG docker "$USER"`
- `newgrp docker`

### Environment

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DRIFTC` | yes | — | Path to `driftc` compiler |
| `DRIFT_PKG_ROOT` | no | `build/deploy` | Package library root for `just prepare` / `just deploy` |
| `DRIFT_SIGN_KEY_FILE` | for deploy | — | Ed25519 signing key file |

### Package Lifecycle

This project uses `drift-manifest.json` to define two co-artifacts (`mariadb-wire-proto` and `mariadb-rpc`) with versioning, dependency resolution, and signed artifact publishing.

- `just prepare` — resolve dependencies and write `drift-lock.json`
- `just deploy` — build, sign, and publish both packages to `DRIFT_PKG_ROOT`

### Certification Gates

The workspace orchestrator treats three `just` commands as the repo's public certification interface. Each must return clean pass/fail via exit code. A repo is certification-ready only when all three gates pass.

| Gate | What it validates | Compilation path |
|------|-------------------|-----------------|
| `just test` | Correctness and memory safety | Local source roots (fast dev loop) |
| `just stress` | Protocol contamination under concurrency | Deployed signed `.zdmp` packages |
| `just perf` | Wire-level performance regression | Deployed signed `.zdmp` packages |

**`just stress` and `just perf` depend on `just deploy`.** They build, sign, and publish both packages before compiling test/perf scenarios against the deployed `.zdmp` artifacts. This is intentional — it exercises the real signed-package consumption path that downstream consumers use, including `.zdmp` metadata parsing, namespace trust verification, and transitive dependency resolution. A failure during the deploy/package-production phase is a certification failure.

#### `just test` — correctness and safety

Runs the full test suite (unit + live/e2e) under three safety modes sequentially:

1. **Plain** — baseline correctness
2. **ASAN** (`DRIFT_ASAN=1`) — address sanitizer
3. **Memcheck** (`DRIFT_MEMCHECK=1`) — valgrind memory checking

Compiles from local source roots via the manifest. No deploy step required.

Preconditions:
- `DRIFTC` is set
- Local MariaDB instance running at `127.0.0.1:34114` with fixture schema loaded (`just db-load-schema mdb114-a`)

#### `just stress` — protocol contamination stress

Runs 5 concurrent stress scenarios (16 workers x 50 iterations each) against a live MariaDB instance through the real `mariadb-rpc` API:

1. Connection churn (rapid connect/call/close)
2. Pool-reuse contamination (partial consume + reset + state verification)
3. Interleaved multi-resultset (mixed consume/skip patterns)
4. Transaction boundary stress (commit/rollback + reset + state verification)
5. Error path cycling (alternating success/ServerErr + connection reuse)

Compiles from deployed `.zdmp` packages via `--package-root` + `--dep`.

Preconditions:
- Same as `just test`
- `DRIFT_SIGN_KEY_FILE` is set (required by `just deploy`)
- `drift` CLI on `PATH` (for deploy)

#### `just perf` — performance regression gate

Runs RPC scenarios through a wire-capture proxy, measures bytes/packets on the wire, and compares against a machine-pinned baseline. Fails if any wire metric regresses more than 5% from baseline. Fails closed if no baseline exists for this machine.

Machine identity: baselines are keyed by `/etc/machine-id` for exact host pinning.

- `just perf` — run scenarios and gate against baseline
- `just perf-record-baseline` — snapshot current results as the baseline for this machine

Preconditions:
- Same as `just stress`
- Local proxy port `127.0.0.1:34115` available
- Baseline recorded for this machine (`perf/baselines/<machine-id>.json`)

See `perf/README.md` for result format and scenario details.

### Trust and Deploy Prerequisites

`just stress` and `just perf` compile test code against the deployed signed packages, not local source trees. This requires:

1. **Signing key** — `DRIFT_SIGN_KEY_FILE` must point to an Ed25519 key file
2. **Deploy tooling** — `drift` CLI must be on `PATH`
3. **Trust store** — `drift/trust.json` must exist with namespace claims for `mariadb.rpc.*` and `mariadb.wire.proto.*`

The trust store (`drift/trust.json`) is checked into the repo. It maps the project's signing key to its namespace claims so that `driftc` accepts the locally-deployed packages. This is the same trust mechanism that downstream consumers use — the only difference is that consumers import trust from the published `.author-profile` instead of using a pre-populated trust store.

If the trust store is missing or the signing key changes, `just stress` and `just perf` will fail at compile time with a namespace trust error. This is intentional — it validates the trust chain end-to-end.

### Build Support Flags

- `DRIFT_ASAN=1` — address sanitizer (sets `ASAN_OPTIONS=detect_leaks=0:halt_on_error=1`). Incompatible with `DRIFT_MEMCHECK`/`DRIFT_MASSIF`.
- `DRIFT_MEMCHECK=1` — runs binaries under `valgrind --tool=memcheck`
- `DRIFT_MASSIF=1` — runs binaries under `valgrind --tool=massif`
- `DRIFT_ALLOC_TRACK=1` — enables allocator tracking instrumentation
- `DRIFT_OPTIMIZED=1` — passes `--optimized` to driftc

### Dev Workflows (not certification gates)

These are lighter-weight, source-level workflows for the inner dev loop. They compile from local source roots (no deploy required) and are not part of the orchestrator's certification interface.

#### Individual test recipes

All dev test recipes use `--manifest drift-manifest.json --artifact <name>` to derive source roots from the manifest. Co-artifact dependencies compile against local source trees.

- `just test-unit` — unit tests only (no DB): `wire-check` + `rpc-check`
- `just test-live` — live/e2e tests (needs DB)
- `just wire-check` / `just wire-check-unit <file>` — wire-proto unit tests
- `just rpc-check` / `just rpc-check-unit <file>` — RPC unit tests
- `just wire-compile-check [file]` — compile-only check

### Wire Capture Proxy (Fixture Generation)

- `just wire-capture <scenario> <listen_port> <target_port> [target_host]`
  - Starts a one-shot TCP MITM proxy and records both directions as raw `.bin` chunks.
  - Output root: `tests/fixtures/scenarios/bin/<scenario>/<run-id>/`
  - Files written per run:
    - `manifest.json`
    - `events.jsonl` (ordered chunk metadata)
    - `0000_c2s.bin`, `0001_s2c.bin`, ...
    - `summary.json`
    - `scenario.sql` (SQL transcript when extracted; see packetized step below)

Example against local DB instance `mdb114-a`:

1. Start capture proxy:
   - `just wire-capture handshake_mdb114a 34115 34114`
2. Point your client to `127.0.0.1:34115` (proxy), not directly to `34114`.
3. Run the handshake/query scenario once; proxy exits when connection closes.
4. Inspect captured binaries under:
   - `tests/fixtures/scenarios/bin/handshake_mdb114a/<run-id>/`

Important:
- For fixture extraction/replay parsing, capture plaintext protocol sessions (disable TLS in the client).
- Example for mariadb CLI:
  - `mariadb --ssl=OFF -h 127.0.0.1 -P 34115 -u root -prootpw -e "SELECT VERSION();"`

### Packetized Replay Fixtures

Use capture runs to build deterministic packet fixtures (no live TCP needed in tests):

1. List captured runs:
   - `just wire-capture-list`
2. Extract one run into packetized artifacts:
   - `just wire-fixture-extract <scenario> <run-id>`
3. Output is written to:
   - `tests/fixtures/packetized/<scenario>/<run-id>/`
4. Key files:
   - `c2s_packets.json`
   - `s2c_packets.json`
   - `c2s_stream.bin`
   - `s2c_stream.bin`
   - `manifest.json`
   - `scenario.sql` (reconstructed SQL transcript from COM_QUERY packets)
5. `wire-fixture-extract` also writes matching `scenario.sql` into:
   - `tests/fixtures/scenarios/bin/<scenario>/<run-id>/scenario.sql`

Notes:
- Packetization reconstructs MariaDB packet boundaries from the TCP stream (`3-byte length + 1-byte sequence + payload`).
- These packetized files are intended for deterministic replay tests without running a live DB instance.

## Local MariaDB Dev Instances

### Layout (generated, not checked in)

- `tmp_db_instances/<instance>/runtime/data`
- `tmp_db_instances/<instance>/runtime/log`
- `tmp_db_instances/<instance>/runtime/tmp`
- `tmp_db_instances/<instance>/runtime/run.env`
- `tmp_db_instances/<instance>/config/compose.yaml`
- `tmp_db_instances/<instance>/config/conf.d/my.cnf`
- `tmp_db_instances/<instance>/config/init/`

### Naming and auto-port scheme

- Use instance names like `mdb114-a`, `mdb114-b`, `mdb114-c`.
- Port formula: `34000 + version + (slot_index - 1) * 5`.
- Examples:
- `mdb114-a` -> `34114`
- `mdb114-b` -> `34119`
- `mdb114-c` -> `34124`

### Commands (`just db-*`)

- `just db-create mdb114-a`
- `just db-up mdb114-a`
- `just db-ps mdb114-a`
- `just db-logs mdb114-a`
- `just db-sql mdb114-a "SELECT 1;"`
- `just db-load-schema mdb114-a`
- `just db-down mdb114-a`
- `just db-rm mdb114-a`
- Override host port and image:
- `just db-create mdb114-b 34080 mariadb:11.4`

Recommended first-run sequence on a new machine:

1. `just db-create mdb114-a`
2. `docker compose version`
3. `docker ps`
4. `mariadb --version`
5. `just db-up mdb114-a`
6. `just db-load-schema mdb114-a`

### Notes

- Data and config both live under `tmp_db_instances/<instance>/` (`runtime/` and `config/`).
- You can run multiple instances concurrently by using distinct instance names.
- `tmp_db_instances/` must stay git-ignored.

### Safety

- Instance names are strictly validated (`^mdb[0-9]+-[a-z]$`).
- `run.env` is never `source`d; expected keys are parsed explicitly.
- Docker compose is invoked with argv arrays (no fragile command-string splitting).

## Repository layout

```text
drift-manifest.json                  # package manifest (artifacts, versions, deps)
drift-lock.json                      # resolved dependency lock (generated by drift prepare)
drift/trust.json                     # project-local trust store (namespace claims for signing key)
the-drift-foundation.author-profile  # publisher signing identity
packages/
  mariadb-wire-proto/                # wire protocol package
  mariadb-rpc/                       # RPC layer package
build/deploy/                        # deploy output (gitignored)
tests/
  stress/                            # stress test scenarios (certification gate)
perf/
  scenarios/                         # perf benchmark scenarios
  baselines/                         # machine-keyed perf baselines (by /etc/machine-id)
  results/                           # timestamped perf run results
docs/
tools/
```

## Development policy

- Track Drift toolchain `main` (see `AGENTS.md`).
- Regression-first for core defects.
- No workaround-only masking for protocol/concurrency/lifetime bugs.
