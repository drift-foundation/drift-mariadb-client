# Reduction package for K — sleep-after-query bug (Issue 3)

**Date:** 2026-05-15
**Toolchain:** `driftc 0.31.87 | abi 14 | git de5fbaca` (certified)
**Also reproduced on:** `driftc 0.31.88 | abi 14 | git 92e553a8` (staged) — sleep bug not in scope of 0.31.88 fix
**Note on the related Issue 1:** the chained method-call form `arc.get().field.method()` IS fixed in 0.31.88 (was still broken on 0.31.87). Separate from the sleep bug below.

---

## Narrowing results

All four scenarios run against fresh-state MariaDB on `127.0.0.1:34114`. Each is the **only** scenario in its binary (no prior open/close cycles that mask the bug). All deterministic.

| Scenario | Wire ops after `wire.connect` | `sleep(550ms)` elapsed |
|---|---|---|
| L0 baseline | (none) | 550ms ✓ |
| L1 net.connect | TCP only | 550ms ✓ |
| L2 net.connect + read | one socket read | 550ms ✓ |
| L2b net.connect + read + write | one read, one write | 550ms ✓ |
| L2w wire.connect | full handshake + auth, no query | 550ms ✓ |
| L2q wire.connect + 1 query | `SELECT 1` + drain | **0ms** ✗ |
| L3a wire.connect + SET NAMES | `SET NAMES utf8mb4` + drain | **0ms** ✗ |
| L3b wire.connect + set_autocommit | `wire.set_autocommit(false)` | **0ms** ✗ |
| L3c wire.connect + 2 queries | `SELECT 1` x 2 + drain each | **0ms** ✗ |

### What this tells you

- **Not** specific to SET-family. SELECT 1 alone (L2q) triggers.
- **Not** specific to multi-query path. A single query (L2q/L3a/L3b) triggers; multi-query (L3c) also triggers.
- **Not** the handshake. L2w (full handshake, no query) is fine.
- **Not** raw socket I/O. L1/L2/L2b are all fine.

**Trigger is the FIRST `COM_QUERY` round-trip after handshake.** That's the cleanest characterization: anything that goes through `wire.query` or `wire.set_autocommit` once is sufficient. The earlier hypothesis ("two consecutive queries" or "SET-family path") was wrong — it was masked by close-repair cycles in the layered binary.

## Cleanest fixture (5/5 deterministic)

`packages/mariadb-rpc/tests/spike/reduce_l2q_only_test.drift` — 55 lines, deps: only `mariadb.wire.proto` package.

```drift
module mariadb.rpc.tests.spike.reduce_l2q_only;

import std.core as core;
import std.concurrent as conc;
import std.time as time;
import std.format as format;
import std.console as console;
import mariadb.wire.proto as wire;

const HOST: String = "127.0.0.1";
const PORT: Int = 34114;
const USER: String = "root";
const PASSWORD: String = "rootpw";
const DB: String = "appdb";
const TIMEOUT_MS: Int = 1000;

fn main() nothrow -> Int {
    val opts = wire.create_connect_options(HOST, PORT, USER, PASSWORD, DB, wire.DEFAULT_CHARSET_ID, cast<Uint>(0), TIMEOUT_MS, TIMEOUT_MS);
    match wire.connect(&opts) {
        core.Result::Err(_) => { return 1; },
        core.Result::Ok(s) => {
            var session = move s;
            val sql = "SELECT 1";
            match wire.query(&mut session, &sql) {
                core.Result::Err(_) => { val _ = wire.close(&mut session); return 2; },
                core.Result::Ok(stv) => {
                    var stmt = move stv;
                    var done = false;
                    while not done {
                        match wire.next_event(&mut stmt) {
                            core.Result::Err(_) => { done = true; },
                            core.Result::Ok(ev) => {
                                match ev {
                                    wire.StatementEvent::Row(_) => {},
                                    wire.StatementEvent::ResultSetEnd => {},
                                    wire.StatementEvent::StatementEnd(_) => { done = true; },
                                    wire.StatementEvent::StatementErr(_) => { done = true; }
                                }
                            }
                        }
                    }
                }
            }
            console.println("query drained");
            val t = time.now_monotonic();
            val _ = conc.sleep(conc.Duration(millis = 550));
            console.println("sleep(550ms) elapsed=" + format.format_int(time.elapsed_ms(&t)));
            val _ = wire.close(&mut session);
            return 0;
        }
    }
}
```

### Expected stdout

**Pass (fix lands):**
```
query drained
sleep(550ms) elapsed=550   (give or take a few ms)
```

**Fail (current 0.31.87 / 0.31.88 behavior):**
```
query drained
sleep(550ms) elapsed=0
```

## How to run

The fixture depends on the `mariadb-wire-proto` package in the `drift-mariadb-client` repo (TCP + handshake + COM_QUERY codec). Easiest is to run it via the repo's existing test harness:

```bash
cd ~/src/drift-mariadb-client
DRIFT_TOOLCHAIN_ROOT=$HOME/opt/drift/certified/current/toolchain \
  just rpc-check-unit packages/mariadb-rpc/tests/spike/reduce_l2q_only_test.drift
```

If you'd rather invoke driftc directly:

```bash
DR=$HOME/opt/drift/certified/current/toolchain
$DR/bin/driftc \
  --manifest ~/src/drift-mariadb-client/drift/manifest.json \
  --stdlib-root $DR/lib/stdlib \
  --entry mariadb.rpc.tests.spike.reduce_l2q_only::main \
  --target-word-bits 64 \
  -o /tmp/reduce_l2q_only \
  packages/mariadb-rpc/tests/spike/reduce_l2q_only_test.drift
/tmp/reduce_l2q_only
```

(Adjust paths; harness command line is what we actually use day-to-day.)

## MariaDB server setup

The fixture talks to a local MariaDB on `127.0.0.1:34114` as `root/rootpw` against database `appdb`. We use docker-compose:

```bash
cd ~/src/drift-mariadb-client
just db-create mdb114-a    # spins up mariadb:11.4 on port 34114
just db-up mdb114-a
just db-load-schema mdb114-a   # loads tests/fixtures/appdb_schema.sql
```

The schema doesn't matter for this bug — `SELECT 1` doesn't reference any table. Any MariaDB 11.4+ on `127.0.0.1:34114` with a root login works. If your local setup uses different creds, edit the consts at the top of the fixture.

If you want a smaller server to avoid mariadb entirely, the trigger is **purely on the client side** (stale reactor state after `wire.query`'s read/write cycles). A minimal mock would need to: accept TCP, send a valid MariaDB initial handshake hello packet, accept the auth response (any reply that satisfies native-password auth), accept a COM_QUERY frame, and reply with a column-count + column-def + EOF + row + EOF sequence. We can put that mock in your tree if helpful — say the word.

## The "close repairs it" data — still the load-bearing signal

`packages/mariadb-rpc/tests/spike/reduce_sleep_l3_only_test.drift`:

```
baseline sleep(50ms) elapsed=50            <- before any I/O, sleep works
connected
[after connect] sleep(550ms) elapsed=0     <- bug
[after close]   sleep(550ms) elapsed=550   <- close() repaired it
[after close x2] sleep(550ms) elapsed=551  <- stays repaired
```

Combined with the narrowing above: the trigger is the first COM_QUERY's read/write cycle on the live socket, and the state is **held by the live fd** in the reactor. `rpc.close` (which calls `stream.close()`, removing the fd from the epoll set) clears it.

## Where to instrument (per your sharpened hypothesis)

You called out:
- `pending_read` / `pending_write` on the watch
- `read_vt` / `write_vt` waiter slots — are they cleared after `wire.next_event`'s drain returns?
- `park_token` on the main VT
- whether `epoll_wait` reports readiness on the connection fd with no VT actually waiting
- whether a pending readiness edge is being replayed against the main VT during `conc.sleep`

Given the trigger is "any one COM_QUERY round-trip," I'd start with the **last `wire.next_event` call's return path** — the one that delivers `StatementEvent::StatementEnd` and unwinds the wait/watch state for the final OK packet read. If a waiter slot is left populated after that drain (because the StatementEnd packet was already buffered, so the reactor's wait state wasn't "cleared by the unpark path" — only by a "we already had the data" short-circuit), the next `conc.sleep`'s `park_until` would see the slot as already-woken.

## All three fixture files in repo

| Path | Lines | Purpose |
|---|---|---|
| `packages/mariadb-rpc/tests/spike/reduce_l2q_only_test.drift` | 55 | Minimal trigger, 5/5 deterministic |
| `packages/mariadb-rpc/tests/spike/reduce_sleep_l3_only_test.drift` | 60 | Shows close-repairs-it signal |
| `packages/mariadb-rpc/tests/spike/reduce_sleep_after_connect_test.drift` | 331 | Full L0→L3 + narrowing matrix in one binary |

---

— SL
