# drift-mariadb-client Integration Guide

How to consume `mariadb-rpc` (and optionally `mariadb-wire-proto`) from
another Drift project.

This guide covers the consumer path: obtaining artifacts, establishing
trust, compiling against published packages, and writing application
code. For developing or publishing the library itself, see the
[repository README](../README.md).

## Published packages

| Package | Module | Description |
|---|---|---|
| `mariadb-wire-proto` | `mariadb.wire.proto` | Low-level MariaDB wire protocol: packet codec, handshake, auth, command/response state machine |
| `mariadb-rpc` | `mariadb.rpc` | Streaming stored-procedure RPC layer on top of wire-proto |

`mariadb-rpc` depends on `mariadb-wire-proto` (resolved automatically).

### Which package to use

Most consumers should depend only on `mariadb-rpc`. It provides
connection config, stored procedure calls, streaming result consumption,
row getters, transaction control, and pool-safe reset — all without
exposing wire-level details.

Use `mariadb-wire-proto` directly only if you need raw packet-level
control (custom command sequences, non-stored-procedure flows, or
protocol instrumentation).

## Consumer prerequisites

- Current Drift toolchain — **driftc 0.33.57+ / ABI 18**. The trust model is
  trust-v1, but claim bodies are schema v2 and provenance is schema v4 as of
  0.33.57; toolchains older than 0.33.57 parse only v1 bodies and reject these
  artifacts.
- Published package artifacts under a package root: `.zdmp` plus the
  trust-v1 sidecars (`.author-claim`, `.cert-claim.<kid>.json`, and the
  `.provenance.zst` provenance bundle)
- A project trust store (`drift/trust.json`) granting both an `authors`
  kid and a `certifiers` kid for `mariadb.wire.proto.*` and `mariadb.rpc.*`

## Trust and signed package setup

mariadb-wire-proto and mariadb-rpc are consumed through Drift's trust-v1
package flow. Consumers need each package's artifact, its two role-tagged
sidecars, and a project trust store granting both roles for the package
namespaces.

### What Drift verifies

Drift distinguishes between two trust domains:

- **Bundled stdlib**: the deployed toolchain ships `std.zdmp` plus its v1
  sidecars and a `core_trust_v1.json` listing the Foundation kids in
  their respective roles. The compiler verifies stdlib against that core
  store.
- **User / third-party packages**: packages such as mariadb-rpc and
  mariadb-wire-proto are verified against the consumer's project trust
  store (`drift/trust.json`), layered on top of the core store. The
  consumer grants the publisher's author kid the `authors` role and the
  certifier's kid the `certifiers` role for the package namespaces.

### Package root layout

After deployment, the library root contains, per artifact and version:

```
<library-root>/
  mariadb-wire-proto/
    <version>/
      mariadb-wire-proto.zdmp
      mariadb-wire-proto.author-claim
      mariadb-wire-proto.cert-claim.<certifier-kid>.json
      mariadb-wire-proto.author-profile
  mariadb-rpc/
    <version>/
      mariadb-rpc.zdmp
      mariadb-rpc.author-claim
      mariadb-rpc.cert-claim.<certifier-kid>.json
      mariadb-rpc.author-profile
```

The `.author-claim` binds the package id, version, namespace, declared
deps, and source identity. The `.cert-claim.<kid>.json` binds the
artifact bytes, source identity, toolchain, cert suite, and resolved
dependency graph. `.author-profile` is a human-readable publisher
descriptor; it is not consulted at load time.

### Trust setup

Two options to populate the project trust store:

```bash
# A. Import the publisher's author kid from a published .author-claim:
drift trust import \
    --trust-store drift/trust.json \
    /path/to/mariadb-rpc/<version>/mariadb-rpc.author-claim

# B. Grant explicitly (use when you have the pubkey out of band):
drift trust add \
    --trust-store drift/trust.json \
    --namespace 'mariadb.rpc.*' \
    --pubkey-b64 <base64-32> \
    --kid ed25519:<kid> \
    --role author    # or 'certifier' / 'both'
```

Repeat for `mariadb.wire.proto.*`. Production trust stores SHOULD pass
`--role author` or `--role certifier` explicitly so role separation is
visible in the file; the verifier consults each role independently.

At load time the compiler checks: (1) the author-claim verifies under a
trusted `authors` kid for the package namespace; (2) the cert-claim
verifies under a trusted `certifiers` kid; (3) the .zdmp bytes hash to
the cert-claim's `artifact_sha256`; (4) the resolved dep graph matches
the cert-claim's recorded closure. Any mismatch is a hard rejection.

## Compilation

### Typical application

```bash
driftc --target-word-bits 64 \
    --package-root <library-root> \
    --dep mariadb-rpc@<version> \
    --entry main::main \
    -o my_app \
    my_app.drift
```

`--package-root` points to the library root (not the version directory).
`--dep` pins the exact version. The compiler resolves
`mariadb-wire-proto` transitively from the package metadata.

### Development bypass

To skip signature verification during local development:

```bash
driftc --target-word-bits 64 \
    --package-root <library-root> \
    --dep mariadb-rpc@<version> \
    --allow-unsigned-from <library-root> \
    --entry main::main \
    -o my_app \
    my_app.drift
```

Package metadata (dependencies, module declarations) is still read from
the `.zdmp` — `--allow-unsigned-from` only bypasses signature checks.

## Minimal consumer example

```drift
module main;

import std.core as core;
import std.console as console;
import std.format as fmt;
import mariadb.rpc as rpc;

fn main() nothrow -> Int {
    // 1. Build connection config.
    var b = rpc.new_connection_config_builder();
    b.with_host("127.0.0.1");
    b.with_port(3306);
    b.with_user("appuser");
    b.with_password("secret");
    b.with_database("mydb");
    b.with_connect_timeout_ms(3000);
    b.with_read_timeout_ms(3000);
    b.with_write_timeout_ms(3000);
    b.with_autocommit(false);

    val cfg = match rpc.build_connection_config(move b) {
        core.Result::Ok(c) => { move c },
        core.Result::Err(e) => {
            console.println("config error: " + e.tag);
            return 1;
        }
    };

    // 2. Connect.
    var conn = match rpc.connect(move cfg) {
        core.Result::Ok(c) => { move c },
        core.Result::Err(e) => {
            console.println("connect error: " + e.tag);
            return 2;
        }
    };

    // 3. Call a stored procedure.
    var args: Array<rpc.RpcArg> = [];
    args.push(rpc.RpcArg::Int(42));

    var stmt = match conn.call(&"sp_get_user", &args) {
        core.Result::Ok(s) => { move s },
        core.Result::Err(e) => {
            console.println("call error: " + e.tag);
            return 3;
        }
    };

    // 4. Stream results.
    while true {
        match stmt.next_event() {
            core.Result::Err(e) => {
                console.println("event error: " + e.tag);
                return 4;
            },
            core.Result::Ok(ev) => {
                match ev {
                    rpc.RpcEvent::Row(row) => {
                        match row.get_string(0) {
                            core.Result::Ok(name) => {
                                console.println("user: " + name);
                            },
                            core.Result::Err(_) => {}
                        }
                    },
                    rpc.RpcEvent::ResultSetEnd => {},
                    rpc.RpcEvent::StatementEnd(_) => { break; },
                    rpc.RpcEvent::ServerErr(err) => {
                        console.println("server error: " + err.message);
                        break;
                    }
                }
            }
        }
    }

    // 5. Reset for pool reuse (or close).
    match conn.reset_for_pool_reuse() {
        core.Result::Ok(_) => {},
        core.Result::Err(_) => { return 5; }
    }

    match conn.close() {
        core.Result::Ok(_) => {},
        core.Result::Err(_) => { return 6; }
    }

    return 0;
}
```

## Operational notes

### MariaDB server

The library targets MariaDB 11.4+. It uses `mysql_native_password` auth
(SHA1-based). TLS is not supported in the current version — connections
are plaintext.

For local development, the library repository includes Docker Compose
tooling for spinning up test instances, but consumers are free to point
at any accessible MariaDB server.

### Streaming and drain semantics

Results are consumed incrementally via `stmt.next_event()`. There is no
buffer-all API. This keeps memory bounded for large resultsets.

If you don't need all results, drain explicitly:

- `stmt.skip_result()` — skip the current resultset, stop at boundary
- `stmt.skip_remaining()` — drain everything to the terminal event

A statement must reach a terminal event (`StatementEnd` or `ServerErr`)
before the connection can issue another call. See the
[single active statement rule](effective-mariadb-rpc.md#single-active-statement-rule).

### Connection reuse

Before returning a connection to a pool:

1. Ensure the active statement is fully consumed or skipped.
2. Call `conn.reset_for_pool_reuse()`.

This sends `COM_RESET_CONNECTION` (single round-trip) or falls back to
`ROLLBACK` + `SET autocommit=1` + `PING`. It verifies session state is
clean before declaring the connection reusable.

### Error model

Two error channels:

- **`RpcError`** (via `Result::Err`) — transport or protocol failure.
  The connection may be dead.
- **`RpcEvent::ServerErr`** (via the event stream) — server SQL error
  (missing procedure, constraint violation, etc.). The connection
  remains alive; only the statement is terminal.

Handle both explicitly. For the full error tag reference, see
[effective-mariadb-rpc.md](effective-mariadb-rpc.md#error-model).

## API reference

- [Effective mariadb-rpc usage](effective-mariadb-rpc.md) — config
  builder, statement model, event types, row getters, drain semantics,
  transaction control, error tags
- [Effective mariadb-wire-proto usage](effective-mariadb-wire-proto.md)
  — low-level wire protocol details (for advanced consumers)

## Troubleshooting

| Phase | Symptom |
|---|---|
| **Trust** | `drift trust import` rejects the `.author-claim`, or the project `drift/trust.json` lacks the required role grant |
| **Verification** | Author-claim or cert-claim signature rejected, untrusted kid for the required role, artifact_sha256 mismatch, missing `.author-claim` / `.cert-claim.<kid>.json` sidecar |
| **Package load** | Missing transitive dep (`mariadb-wire-proto` not under package root) |
| **Checker** | Type errors referencing package types |
| **Link-time** | Undefined symbols (check runtime library availability) |
| **Runtime** | Connection refused (MariaDB not running), auth failure, timeout |

When reporting an issue, include:

- Exact compiler version (`driftc --version`)
- Exact `--dep` pins used
- Trust store mode (author-profile via `drift trust`, or `--allow-unsigned-from`)
- MariaDB server version
- Minimal reproduction
