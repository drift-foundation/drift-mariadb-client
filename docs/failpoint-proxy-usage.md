# mariadb-failpoint-proxy: app harness integration guide

How to consume the `mariadb-failpoint-proxy` binary from an application test
harness (e.g. bookkeeper/Microflows) to deterministically test the ambiguous
COMMIT-ack-loss boundary — `client sends COMMIT; MariaDB may durably commit;
client loses the acknowledgement`.

**Test-only.** This is a `kind:app` Drift artifact built and certified from
this repo, not part of production `mariadb-rpc`. It contains no production
code path, and production `mariadb-rpc` contains no failpoint-arming API —
see [No failpoint code in the app binary](#no-failpoint-code-in-the-app-binary)
below. Never point it at normal/shared production traffic — see
[Security](#security) for the one narrow exception (an isolated,
explicitly-run-booked remote smoke test). For the full design rationale and
wire-protocol spec, see
[`work/mariadb-rpc-failpoints/PLAN.md`](../work/mariadb-rpc-failpoints/PLAN.md).

## Topology

```text
your app  -- MariaDB protocol --> proxy -- MariaDB protocol --> real MariaDB
your test harness -- TCP control API --> proxy
```

The proxy sits transparently in the MariaDB wire path for exactly the one DB
domain you're testing. Everything else your app talks to (including any
other DB domain) stays direct.

## Starting the proxy

There are two distinct ways to get the binary, depending on who you are.

### Local dev build (unsigned, not certified)

For local iteration, or when your harness manages the whole lifecycle
itself (build, start, drive a client, stop) — this is what this repo's own
[S8](#s8-this-repos-own-ci-runs-the-real-binary) gate does:

```bash
just build-app mariadb-failpoint-proxy
# -> build/local-app/mariadb-failpoint-proxy
```

No deploy, no signing, no publishing — fine for iterating against this repo
directly, but this binary carries no trust chain.

### Consuming the certified/released binary (downstream harnesses, cert pipelines)

`mariadb-failpoint-proxy` is a signed `kind:app` artifact published the same
way as this repo's two `kind:package` artifacts (`mariadb-rpc` /
`mariadb-wire-proto`) — via `just deploy` (or your org's cert pipeline
invoking the same `drift deploy`), landing under
`${DRIFT_APP_PKG_ROOT:-build/deploy-app}/mariadb-failpoint-proxy/<version>/`
alongside its trust sidecars:

```
mariadb-failpoint-proxy                              # the signed executable
mariadb-failpoint-proxy.author-claim
mariadb-failpoint-proxy.author-profile
mariadb-failpoint-proxy.cert-claim.<kid>.json
mariadb-failpoint-proxy.provenance.zst
```

**A downstream app harness that isn't building this repo from source should
consume this published artifact, not a locally rebuilt copy** — same trust
chain (author-claim + cert-claim + provenance) as the packages it's testing
against, not an out-of-band binary. This is the path that matters for cert:
it's the same certified binary this repo's own [S8](#s8-this-repos-own-ci-runs-the-real-binary)
gate runs, not a rebuild-from-source that could silently drift from it.

### Running it

Identical either way — three listeners configured explicitly:

```bash
<path-to-binary>/mariadb-failpoint-proxy \
  --data-host 127.0.0.1   --data-port 43306 \
  --backend-host 127.0.0.1 --backend-port 3306 \
  --control-host 127.0.0.1 --control-port 43307
```

| Flag | Default | Meaning |
|---|---|---|
| `--data-host` / `--data-port` | `127.0.0.1` / `43306` | Where your app connects — this is what you point your app's DB config at for the domain under test |
| `--backend-host` / `--backend-port` | `127.0.0.1` / `3306` | The real MariaDB the proxy forwards to |
| `--control-host` / `--control-port` | `127.0.0.1` / `43307` | Where your test harness arms/inspects failpoints (raw TCP, JSON Lines — see below) |

`--help`/`-h` prints usage and exits immediately; otherwise the process runs
until killed (plain `SIGTERM` is sufficient — there is no graceful-shutdown
flag). Structured JSON Lines events go to stderr (`proxy_start`,
`client_accept`, `backend_connect`, `commit_observed`, `failpoint_arm`,
`failpoint_fire`, `failpoint_clear`, `conn_close`, ...) — low-noise, and
never SQL text, auth bytes, or packet payloads.

Bind both listeners to loopback unless you specifically need a remote test
environment — see [Security](#security) below.

## Routing: only the domain under test goes through the proxy

Point **only the one DB domain you're testing** at the proxy's data
listener. Every other domain your app uses stays configured with its real,
direct host/port. Don't put multiple domains behind one proxy instance
unless you have a stronger discriminator than "next COMMIT" — the wrong
domain could consume a one-shot armed failpoint meant for another.

Two worked examples from this repo's own design work:

**H2 — ledger COMMIT unknown.** The ledger path emits exactly one COMMIT per
transfer on the `bookkeeper_db` domain, so exact-next-COMMIT matching
(`match.nth = 1`, the default) is deterministic:

- `bookkeeper_db` -> proxy (`--data-port 43306`, forwarding to the real
  `bookkeeper_db` host/port)
- `singular` -> direct (real host/port, no proxy)

Proven live in this repo against a real subprocess:
`packages/mariadb-rpc/tests/e2e/live_proxy_pool_commit_ambiguous_test.drift`
(also run automatically by [S8](#s8-this-repos-own-ci-runs-the-real-binary), case 2).

**H1 — `Singular.complete` COMMIT unknown.** The singular connection pool
emits an earlier `start` COMMIT before the `complete` COMMIT you actually
want to hit, so routing alone isn't enough — arm with `match.nth = 2` for
this exact start-then-complete shape (a different scenario on your side
could have a different ordinal — prove it with your own scenario if it's
not this one, see `PLAN.md` §9):

- `singular` -> proxy
- `bookkeeper_db` -> direct

This is the riskiest day-1 behavior downstream teams depend on, so it's not
left as only unit-tested (`control_test.drift`'s `scenario_fire_nth2`) —
proven live against a real subprocess, two sequential COMMITs on the SAME
connection (matching the actual start-then-complete shape): the first passes
through untouched, the second returns `AmbiguousWrite`, and `status` confirms
`matched_nth == 2` (not just "it fired," the actual ordinal that fired). See
`packages/mariadb-rpc/tests/e2e/live_proxy_nth_commit_ambiguous_test.drift`
(also run automatically by S8, case 3).

Run H1/H2-style boundary tests as sequential single-operation cases in their
own config with `max_conns = 1` (see below) — not mixed into a concurrent
multi-connection test matrix, where a second connection's COMMIT could steal
the armed one-shot.

**Domain isolation is also proven live**, not just asserted: a connection
that never touches the proxy's data listener cannot affect (or be affected
by) the failpoint registry, while one that does touch it does — see
`packages/mariadb-rpc/tests/e2e/live_proxy_domain_isolation_test.drift` (S8
case 4). This repo has one dev DB, so it proves the claim with a
direct-vs-proxied connection to the same backend rather than simulating
bookkeeper's actual two domains — standing up that two-domain simulation is
app/workflows-owned, not this repo's.

## Control plane: raw TCP, JSON Lines

One minified JSON object per line, both directions, over a plain TCP
connection to the control listener. Synchronous request/response: write one
line, read exactly one line back before sending the next. The response is
the happens-before barrier — read the `arm` ack **before** triggering
whatever will COMMIT, or the failpoint might not be live yet when the
data-plane traffic arrives.

Every response carries `"ok": true|false`; on `false` there's a stable
machine-readable `"error"` code, never a string to pattern-match on
`"message"`.

### `health`

Readiness check — `ready:true` means the data listener is bound (not merely
that the process exists). Poll this instead of a raw connect-and-hope when
waiting for the proxy to come up.

```json
--> {"op":"health"}
<-- {"ok":true,"ready":true,"data_listener":"127.0.0.1:43306"}
```

### `clear`

Full registry reset (armed + fired history) — call this at the start of
each test case for isolation, regardless of what a previous run left behind.

```json
--> {"op":"clear"}
<-- {"ok":true}
```

### `arm`

One-shot exact-`COMMIT` ack-loss. `label` is required; `domain` is an
optional human tag echoed back in `status` (doesn't affect matching —
routing is what actually separates domains, see above); `match.nth` defaults
to `1` and counts matching COMMITs from **this ack**, not from
connection-open (so pool warmup traffic before you arm doesn't throw off the
count). `action`/`times` are effectively fixed in v1 —
`drop_server_response_after_forward` / `1` — include them for clarity or
omit them; either way only that shape is currently supported.

```json
--> {"op":"arm","label":"ledger-commit-unknown-1","domain":"bookkeeper_db","match":{"command":"COM_QUERY","sql":"COMMIT","nth":1}}
<-- {"ok":true,"id":1,"label":"ledger-commit-unknown-1","armed":true}
```

### `assert_all_fired`

Fails loudly — `ok:false` — both when an armed failpoint never fired AND
when *nothing was ever armed* (`armed_count:0`, `error:"not-armed"`): a test
that forgot to call `arm` fails here instead of vacuously passing on an
empty set.

```json
--> {"op":"assert_all_fired"}
<-- {"ok":true,"armed_count":1,"not_fired":[]}
```

```json
--> {"op":"assert_all_fired"}
<-- {"ok":false,"error":"not-armed","message":"no failpoint was ever armed","armed_count":0,"not_fired":[]}
```

### `status`

Full detail for one failpoint by `id` (returned from `arm`). `unknown-id` if
it doesn't exist (e.g. after `clear`). `bytes_forwarded_to_server` is exact
for a bare `COMMIT` (`wire.commit()` always emits exactly that text);
`server_response_bytes_dropped` is always `-1` — the proxy deliberately
never reads the swallowed response (reading it would itself risk forwarding
part of it), so that count is intentionally unmeasured, not a real
measurement.

```json
--> {"op":"status","id":1}
<-- {"ok":true,"id":1,"label":"ledger-commit-unknown-1","domain":"bookkeeper_db","armed":false,"fired":true,"fire_count":1,"data_listener":"127.0.0.1:43306","matched_client":"127.0.0.1:49218","matched_command":"COM_QUERY","matched_sql":"COMMIT","matched_nth":1,"bytes_forwarded_to_server":11,"server_response_bytes_dropped":-1}
```

There's also a `list` op (same object shape as `status`, as a `"failpoints"`
array covering every armed/fired entry) if you need to inspect the whole
registry rather than one id at a time.

## Pool / test configuration

For a deterministic boundary test, configure your app's real
`pool.ConnectionPool` (not a bare connection) for the domain under test:

- `max_conns = 1` — so "the next COMMIT" is unambiguous;
- `keepalive_interval_ms = 0` — no background keepalive traffic to
  accidentally consume the armed COMMIT;
- a **finite** `acquire_timeout` — the config default is "block forever";
  set an explicit bound so a regression in your own release/discard path
  fails the test loudly instead of hanging it (this repo's own S7 test does
  exactly this: `pc.acquire_timeout = Optional::Some(conc.Duration(millis = ...))`).

No known tested bookkeeper path holds two leases from the same pool at once;
if `max_conns = 1` ever exposes that, treat it as a useful finding about
your own app, not a proxy bug.

## No failpoint code in the app binary

The core safety property isn't that the raw TCP control API is impossible to
misuse — it's that **neither your app binary nor production `mariadb-rpc`
contains an arming API**. Production `mariadb-rpc` only exposes
classification: `commit()` returns a typed
`core.Result<Void, RpcCommitError>` where `RpcCommitError.kind` is
`AmbiguousWrite` / `NotSent` / `ServerRejected`. Your app branches on that
`kind` — reconcile on `AmbiguousWrite`, retry-as-not-applied on `NotSent`,
non-retriable on `ServerRejected` — with zero code path that can arm a
failure. Fault injection lives entirely outside your app process, in this
separate, test-only binary.

## Security

The control API can arm destructive fault injection and must be treated as
test infrastructure: bind both listeners to loopback by default, only widen
if you have an explicit remote-smoke runbook. Never put this in front of
normal/shared production traffic — a deployed-app remote smoke test is fine,
but only as an intentional, isolated environment with an explicit runbook:
restricted bind (not open to arbitrary hosts), routing scoped to that one
smoke run, and nobody else's traffic sharing the same backend at the same
time.

## S8: this repo's own CI runs the real binary

`just test` in this repo builds `mariadb-failpoint-proxy` from local source,
starts it as a real subprocess against a dev MariaDB, drives real client
tests through it, and asserts the proxy's own JSON Lines log shows the
expected lifecycle (including an armed-COMMIT-loss case asserting
`failpoint_fire` actually appears) — not just in-process unit logic. So the
guarantees above are exercised automatically on the actual certified binary,
not only proven by hand. See `tools/proxy_gate_smoke.py` and
`work/mariadb-rpc-failpoints/PROXY-GATE-HARNESS.md` if you want the details.
