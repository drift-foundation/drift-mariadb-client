# PLAN - ambiguous COMMIT testing via external MariaDB proxy

Status: DRAFT. Owner: K for implementation review. Planning/review only here. Started 2026-06-30. Revised 2026-06-30 after app-team and production-gate review.

## 1. Driver

PushCoin/bookkeeper needs a deterministic test hook for the dangerous database boundary:

1. client sends `COMMIT`;
2. MariaDB may durably commit;
3. client loses the acknowledgement;
4. connection is poisoned and discarded;
5. retry/recovery uses idempotency to determine whether the write landed.

The app team needs to test the one real bookkeeper binary, including smoke tests of a deployed app. A special test build with failpoint arming compiled into the application weakens that goal and leaves a production-risk surface inside the binary.

## 2. Revised design direction

Use an external MariaDB wire proxy for fault injection. Keep `mariadb-rpc` changes limited to production-safe error taxonomy/classification (the typed `RpcCommitError`).

Topology:

```text
bookkeeper -- MariaDB protocol --> proxy -- MariaDB protocol --> real MariaDB
test harness -- TCP control API --> proxy
```

Ownership (decided):

- the proxy is a product of this repo: a certified `kind:app` Drift artifact (`mariadb-failpoint-proxy`) with source at `failpoint-proxy/src/`, mirroring uflowsd. This repo owns the MariaDB wire-protocol details, so the wire-accurate framing/COMMIT-matching lives with the wire experts; `tools/wire_capture_proxy.py` is a reference/precedent only, not where the app lives (the Drift app is not under `tools/`, which holds Python helpers);
- control plane stays raw TCP / stdlib-only (JSON Lines), so this repo never takes a `drift-web` dependency;
- the app harness consumes the proxy as an external process — it slots into `run_ledger_stress.py` like uflowsd — but the proxy itself is owned and versioned here, not in app/workflows;
- because the proxy is ours, the nth-COMMIT discriminator H1 needs is an in-scope deliverable of this repo, not a cross-team ask. The only genuine cross-team dependency left is the S1–S3 `RpcCommitError` contract that bookkeeper branches on.

Consequences:

- bookkeeper uses the same binary and same DAO/pool/`rpc.commit()` path;
- app config changes only the DB endpoint to point at the proxy;
- fault arming is outside the app process;
- no failpoint arming API is compiled into `bookkeeper`;
- no failpoint arming API is exported by production `mariadb-rpc`;
- the proxy can be used in staging/smoke tests against a live deployed app by changing DB host/port;
- this repo can make the proxy exact and protocol-aware without adding any app/web dependencies.

App-team confirmation:

- DB endpoint swap is acceptable; bookkeeper already consumes DB host/port from generated config.
- TCP control API is acceptable, local-by-default with explicit remote opt-in.
- `max_conns = 1`, keepalive disabled, and finite/fail-fast acquire are acceptable for these tests.
- No known tested bookkeeper path holds two leases from the same DB pool at once; if this appears later, treat it as a useful app finding.
- Exact `COMMIT` matching is sufficient for v1 H2 (ledger); H1 (singular) needs nth-COMMIT matching because the singular pool emits an earlier `start` COMMIT before `complete`.
- App team recommended hosting the proxy in app/workflows; final decision is to keep it as this repo's product (see Ownership above), consumed by the app harness as an external process. Production `mariadb-rpc` remains taxonomy/classification only — no in-process arming.

## 3. Current repo facts

- `rpc.commit()` delegates to `wire.commit()` (`packages/mariadb-rpc/src/lib.drift:546-549`).
- `wire.commit()` is implemented as `_query_expect_ok(session, "COMMIT", 5000)` (`packages/mariadb-wire-proto/src/lib.drift:864-869`).
- `query()` encodes text protocol `COM_QUERY`, writes the command packet, then reads the first response packet (`packages/mariadb-wire-proto/src/lib.drift:686-704`).
- `_write_all` loops until the whole packet has been handed to the OS socket, or returns `wire-write-failed` before that boundary (`packages/mariadb-wire-proto/src/transport.drift:81-99`).
- Pool release discards unhealthy connections by checking `rpc.is_alive()` (`packages/mariadb-rpc/src/pool.drift:536-542`).
- A real transport/read failure after COMMIT causes the wire layer to mark the session dead (`packages/mariadb-wire-proto/src/lib.drift:702-704`), which makes `rpc.is_alive()` false and lets the pool discard the connection naturally.
- Proxy plaintext inspection depends on the current client negotiating no TLS and no compression. Verified current state: `REQUIRED_CAPS` forces protocol/auth/transaction flags only, and does not include `CLIENT_SSL` or `CLIENT_COMPRESS` (`packages/mariadb-wire-proto/src/capabilities.drift:30-32`). There is no TLS/compression implementation in `mariadb-wire-proto` today. If either is added later, this proxy design must be revisited.

Relevant files:

- `packages/mariadb-rpc/src/lib.drift`
- `packages/mariadb-rpc/src/pool.drift`
- `packages/mariadb-wire-proto/src/lib.drift`
- `packages/mariadb-wire-proto/src/transport.drift`
- existing proxy tooling reference: `tools/wire_capture_proxy.py`

## 4. Transaction semantics

The ambiguous durable-write boundary for bookkeeper and coordinator helper paths is `COMMIT`, not `CALL`.

For manual transactions with autocommit disabled:

- lost ack after `CALL` but before `COMMIT` is not "may have committed";
- the transaction is still open;
- closing the connection rolls it back;
- that class is definite not-applied from the durable-write perspective.

The coordinator stored-procedure helper path also drains the SP result and explicitly calls `rpc.commit()`. So coordinator SP writes share the same ambiguous boundary: the helper's `rpc.commit()`.

Scenario-specific implications from the app team:

- **H2 - ledger COMMIT unknown:** route `bookkeeper_db` through the proxy. This is deterministic with exact-COMMIT matching because the ledger path emits one `bookkeeper_db` COMMIT per transfer. The ledger DAO already has a commit-unknown path; bookkeeper follow-up is to branch that helper on `commit()`'s `RpcCommitError.kind` once S1-S3 land.
- **H1 - `Singular.complete` unknown:** route `singular` through the proxy, but routing alone is not enough. The singular pool can emit multiple COMMITs for one operation (`start`, then `complete`, plus `resume` on reclaim). Exact "next COMMIT" would fire too early on `start`. H1 requires an nth-COMMIT discriminator, expected `nth = 2` for the normal start-then-complete path unless the harness proves a different ordinal for a specific scenario.

## 5. Production-safe mariadb-rpc work

This repo should not grow in-process failpoint arming. It should grow safe classification.

Prior gap (now fixed): `RpcConnection.commit()` collapsed every wire commit error into `rpc-wire-commit-failed` with the real wire tag stuffed in the message. The wire layer already had more shape: server commit rejection reaches `tx-command-server-err` from `_query_expect_ok` (`packages/mariadb-wire-proto/src/lib.drift:482-493`), while transport/read failure marks the session dead and returns the transport tag (`packages/mariadb-wire-proto/src/lib.drift:702-704`).

`commit()` now returns a commit-specific error so consumers branch on a typed `kind`, never a string:

```drift
pub variant RpcCommitErrorKind { AmbiguousWrite, NotSent, ServerRejected }

// pub struct (not pub error): a pub error's synthesized Diagnostic can't project
// a variant field, and commit() is nothrow + returns an explicit Result, so no
// Throw/Diagnostic synthesis is needed.
pub struct RpcCommitError { pub kind: RpcCommitErrorKind, pub cause_tag: String }

pub fn commit(self: &mut RpcConnection) nothrow -> core.Result<Void, RpcCommitError>
```

`kind` is the only field consumers branch on; `cause_tag` carries the original lower-level wire-proto machine tag (e.g. `wire-write-failed`, `wire-read-failed`, `tx-command-server-err`) for diagnostics only — no English prose, no duplicate classification string. The three classes:

| kind | Boundary | Meaning | Consumer handling |
|---|---|---|---|
| `AmbiguousWrite` | `COMMIT` fully sent, acknowledgement lost (or any unrecognized failure) | commit may or may not be durable | retriable/reconcile via idempotency key |
| `NotSent` | failure before the request was fully written (session not ready, or partial write) | definite not-applied | safe to retry as not-applied |
| `ServerRejected` | server `ErrPacket` received | definite server rejection | non-retriable |

The pure `classify_commit_wire_tag(wire_tag: &String) -> RpcCommitError` mapping is exported so tests/diagnostics can exercise it without a live wire session; `commit()` delegates to it. Unknown wire tags map to `AmbiguousWrite` on purpose (the only dangerous misclassification is calling an ambiguous commit "not applied" and re-driving a double mutation).

The proxy harness directly exercises the `AmbiguousWrite` class by forwarding zero server-response bytes after the full COMMIT request, so the client fails in `session_read_packet`. `ServerRejected` is a structurally different path (`tx-command-server-err` only after a decoded `ErrPacket`), so the proxy cannot accidentally turn the ambiguous case into a server rejection.

The proxy does not realistically exercise `NotSent` for COMMIT (an ~11-byte COMMIT is one small write, and the proxy sits on the response side). That class is covered by the `classify_commit_wire_tag` unit test (pre-write + `wire-write-failed` tags), not by a proxy mode.

This is production-safe: it classifies real failures correctly and provides no way to arm a failure.

## 6. Proxy fault semantics

Primary proxy failpoint: drop next COMMIT acknowledgement after forwarding the full client request.

Required behavior:

1. Proxy accepts client connection and connects to real MariaDB.
2. Proxy forwards handshake/auth and normal traffic transparently.
3. When armed, proxy reassembles MariaDB wire packets from the TCP stream before inspection. TCP segment boundaries are not packet boundaries.
4. Match only text protocol `COM_QUERY` whose SQL canonicalizes to exactly `COMMIT`.
5. Forward the complete client COMMIT packet to the real MariaDB server.
6. Stop forwarding the server response to the client for that command.
7. Close or reset the client-side connection so `mariadb-rpc` sees a transport/read failure.
8. Close the server-side connection as well. After the proxy swallows the server acknowledgement, that backend connection is orphaned/desynced from the client and must not be reused.
9. Mark the failpoint fired exactly once and auto-disarm.

Do not implement this as "drop next packet" or "drop next response" without COMMIT matching. `SET autocommit`, `ROLLBACK`, `SELECT`, keepalive `PING`, and stored-procedure `CALL` must not consume the armed COMMIT failpoint.

Default failure mode: abrupt close after the full COMMIT request is forwarded. FIN or RST both model reset-style ambiguous ack loss because the client observes a read-side transport failure. A future `drop-and-hold` mode can model timeout-style ack loss by leaving the client waiting until its commit I/O timeout elapses; defer that mode until a test specifically needs it.

After firing, the proxy must remain healthy for fresh client connections. With `times = 1`, the retry/reconcile path's second COMMIT must pass through normally; otherwise the proxy would consume the recovery COMMIT and invalidate the exactly-once assertion.

## 7. Raw TCP control API

Use a separate raw TCP control listener. The user accepts the operational risk; keep the risk visible in docs. Keep this deliberately below REST/web so the proxy can live in this repo without depending on `drift-web`.

Example ports:

- data listener: `127.0.0.1:43306` or staging-visible equivalent;
- control listener: `127.0.0.1:43307` by default, optionally bindable to a configured interface for remote smoke tests.

Recommended minimal protocol: newline-delimited JSON request/response over TCP. One JSON object per line. Each request includes an `op`.

Arm:

```json
{
  "op": "arm",
  "label": "ledger-commit-unknown-1",
  "domain": "bookkeeper_db",
  "match": {
    "command": "COM_QUERY",
    "sql": "COMMIT",
    "nth": 1
  },
  "action": "drop_server_response_after_forward",
  "times": 1
}
```

Response:

```json
{
  "ok": true,
  "id": 1,
  "label": "ledger-commit-unknown-1",
  "armed": true
}
```

Status:

```json
{
  "op": "status",
  "id": 1
}
```

Response:

```json
{
  "ok": true,
  "id": 1,
  "label": "ledger-commit-unknown-1",
  "domain": "bookkeeper_db",
  "armed": false,
  "fired": true,
  "fire_count": 1,
  "data_listener": "127.0.0.1:43306",
  "matched_client": "127.0.0.1:49218",
  "matched_command": "COM_QUERY",
  "matched_sql": "COMMIT",
  "matched_nth": 1,
  "bytes_forwarded_to_server": 11,
  "server_response_bytes_dropped": -1
}
```

`server_response_bytes_dropped` is `-1` by design, not a placeholder: the proxy never reads the swallowed server response (reading it would itself risk forwarding part of it before closing), so an actual dropped-byte count is unmeasurable — `-1` is the documented "not measured" sentinel (see §11 S5 implementation notes).

Useful operations:

- `arm` arms one failpoint.
- `status` inspects one failpoint.
- `list` lists active/recent failpoints.
- `assert_all_fired` fails if any armed failpoint did not fire.
- `clear` clears all armed/recent failpoints for test isolation.
- `health` reports proxy readiness.

Controls:

- `label` required.
- optional `domain` label (e.g. `bookkeeper_db`, `singular`) is echoed in arm/status so a fired event names which DB domain it hit; with one proxy/listener per domain, the `data_listener` already disambiguates, but the explicit label keeps assertions readable.
- `match.nth` defaults to `1`. **Counting origin is arm-time, not connection-open:** the counter starts at the `arm` ack and counts only matching COMMITs seen thereafter on the proxy data listener; only the nth match fires, and the first n-1 matches pass through normally while incrementing the counter. Arm-time origin is what makes it deterministic — under `max_conns = 1` the data connection often pre-exists (warmup / a prior op), so counting from connection-open would be off by the earlier traffic. Required for H1, where the singular pool commits `start` before `complete` (`nth = 2`); H2 uses the default `nth = 1`.
- `times` must default to `1`.
- one-shot failpoints auto-disarm.
- stale armed failpoints must make tests fail loudly through the `assert_all_fired` op.
- optional auth token is acceptable, but not a substitute for network isolation.

Protocol rules (decided):

- **Framing: one minified JSON object per line, UTF-8, `\n`-terminated, both directions.** Objects MUST be minified — no pretty-printing. A pretty-printer emits real newlines mid-object and silently desyncs the line framing; this is the one rule a "helpful" client library will break by default.
- **Synchronous request/response.** Client writes one request line and reads exactly one response line before sending the next. Ordering gives implicit correlation; an optional client-supplied `req_id` may be echoed in the response for explicit correlation.
- **The `arm` response is the happens-before barrier.** Arming travels on the control connection; the COMMIT travels on the data connection. The harness MUST read the `{"armed":true}` reply before it triggers the API call that commits, so the failpoint is guaranteed live before the data-plane COMMIT is processed. Fire-and-forget arming is a flaky test, not a valid sequence. (See §8 step ordering.)
- **Every response carries `ok: true|false`.** Errors are also one line, with a stable machine-readable code (branch on `error`, never on `message`):

```json
{"ok": false, "error": "unknown-op", "message": "unsupported op: foo"}
```

  Closed error-code set for v1: `bad-json`, `too-long`, `unknown-op`, `unknown-id`, `bad-args`, `not-armed`.
- **Robust reader.** Accumulate until `\n`; bound max line length (cap ~1 MB) and reply `{"ok":false,"error":"too-long"}` past it. On a malformed line, reply `bad-json` and keep the connection open rather than desyncing the session.
- **`id` is the key, `label` is a human tag.** Each `arm` returns a fresh `id`; `status`/`clear` key off `id`. Duplicate labels are allowed but discouraged.
- **`clear` is a full reset** — wipes both armed and recent-fired records, for test isolation between cases.
- **`assert_all_fired`** returns `ok:false` with the list of un-fired labels if any armed failpoint never fired, and includes `armed_count` so a test that forgot to arm fails loudly on `armed_count == 0` instead of passing on an empty set.
- **`health`** means the data listener is bound (ideally backend reachable), not merely that the process is up — §8 step 3 gates on it.
- **Concurrency.** Control ops mutate shared proxy registry state under a single lock; multiple control connections are allowed but each op is atomic.
- **Stateless connections.** Reconnecting to the control port is fine; no per-connection session state. No streaming responses in v1. A thin CLI can wrap this later; tests can use raw sockets / `nc` / `socat` directly.

## 8. Real-time test flow

Typical bookkeeper test:

1. Start proxy with data listener pointing at real MariaDB.
2. Configure the specific bookkeeper DB domain under test to use the proxy data listener.
3. Wait for the proxy `health` op to report ready.
4. Drive bookkeeper to the point immediately before the API call that will commit.
5. Test harness arms the failpoint over the raw TCP control API and reads the `{"armed":true}` reply before proceeding — this ack is the happens-before barrier guaranteeing the failpoint is live before the data-plane COMMIT.
6. Only after the arm ack: test harness calls the bookkeeper API.
7. Proxy forwards full `COMMIT` to MariaDB and drops/closes the client response path.
8. bookkeeper sees an ambiguous commit transport error from `mariadb-rpc`.
9. Test harness asserts proxy failpoint fired exactly once.
10. Test harness verifies retry/reconcile behavior and exactly-once ledger result.

For live deployed-app smoke tests, steps 2 and 5 are the key knobs: the deployed app points to the proxy DB endpoint, and the harness controls the proxy over raw TCP.

Bookkeeper has at least two DB domains: `singular` and `bookkeeper_db`. The harness must be able to route each domain independently so `times = 1` is consumed by the intended COMMIT:

- ledger COMMIT unknown: route `bookkeeper_db` through the proxy and keep `singular` direct;
- `Singular.complete` COMMIT unknown: route `singular` through the proxy and keep `bookkeeper_db` direct; arm with nth-COMMIT targeting so the failpoint fires on `complete`, not the earlier `start` commit.

Do not put both domains behind the same armed one-shot proxy unless the test also has a stronger discriminator. Otherwise the wrong domain can consume the armed COMMIT.

## 9. Proxy matching details

Minimum match:

- reassemble framed MariaDB packets first: 4-byte packet header (`payload_len` 3 bytes little-endian + sequence id 1 byte), then payload;
- match on the framed client command payload, not raw TCP chunks;
- command byte `0x03` (`COM_QUERY`) at payload byte 0;
- SQL bytes after the command byte decode as text;
- trim ASCII whitespace and optional trailing semicolon;
- case-insensitive exact match to `COMMIT`.

Avoid broad substring matching. Do not match `COMMIT WORK` unless intentionally added and tested. Do not match multi-statement text unless explicitly supported later.

Optional future discriminators:

- client remote address;
- connection ordinal;
- DB domain, if the proxy or harness can supply it out-of-band;
- SQL comment marker if a caller can safely add one;
- transaction label if exposed outside the SQL path.

Start with label + one-shot + exact COMMIT matching for H2. Add `match.nth` before H1, because H1 is not deterministic with exact next-COMMIT alone.

## 10. Pool/test configuration

For deterministic tests, app-side DB pool should use:

- real `pool.ConnectionPool`;
- `max_conns = 1`;
- `keepalive_interval_ms = 0` unless specifically testing keepalive;
- finite acquire timeout or fail-fast nested-acquire behavior where available.

The proxy design no longer needs an in-process pool failpoint API, but `max_conns = 1` is still useful so the next COMMIT is deterministic.

App-team confirmation: no known tested bookkeeper path holds two leases from the same DB pool at once. The Phase 7 ledger path takes one `bookkeeper_db` lease, commits/returns, then calls Singular through its separate gateway/domain. If `max_conns = 1` exposes a nested acquire later, treat it as a useful app finding.

Domain routing requirement:

- each DB domain needs its own host/port config knob;
- only the domain under test should route through the armed proxy;
- other DB domains should remain direct, or use an unarmed proxy if packet capture is separately desired.

Boundary-fault tests H1/H2 should run as sequential single-operation tests in their own config with `max_conns = 1`. They should not be run in the same config as the concurrent matrix or concurrent redispatch case D, which keep the normal multi-connection settings.

## 11. Step plan

- [x] S1. Production-safe RPC taxonomy: `commit()` no longer flattens all failures into one tag. Delegates to a pure `classify_commit_wire_tag()` that returns an `RpcCommitError` whose `kind` is `NotSent` for pre-write/never-sent failures (`wire-write-failed`, `session-closed`, `session-not-reusable`, `active-statement-present`), `ServerRejected` for a decoded server ERR (`tx-command-server-err`), or `AmbiguousWrite` for all read-side/unknown failures (conservative default — unknown collapses to ambiguous so a lost ack is never misread as not-applied). `cause_tag` preserves the original wire tag for diagnostics. (`packages/mariadb-rpc/src/lib.drift`)
- [x] S2. Typed commit-error contract (frozen): `commit()` returns `core.Result<Void, RpcCommitError>` where `RpcCommitError { kind: RpcCommitErrorKind, cause_tag: String }` and `RpcCommitErrorKind { AmbiguousWrite, NotSent, ServerRejected }`. Consumers branch on `kind` only. Pure `classify_commit_wire_tag` exported for tests. (`packages/mariadb-rpc/src/lib.drift`)
- [x] S3. Unit regression exercises the real wire-tag→`RpcCommitError.kind` mapping via the exported `classify_commit_wire_tag`: read-side failures (`wire-read-eof`/`-failed`/`-header-decode-failed`/`-sequence-mismatch`) → `AmbiguousWrite`, unknown tag → `AmbiguousWrite`, pre-write + `wire-write-failed` → `NotSent`, `tx-command-server-err` → `ServerRejected`, plus `cause_tag` preservation. (`packages/mariadb-rpc/tests/unit/commit_error_taxonomy_test.drift`, passing.) S7 additionally exercises the same mapping through a real socket end-to-end.
- [~] S4. Implement the MariaDB wire proxy as a `kind:app` Drift artifact in this repo, mirroring uflowsd's layout: source at `failpoint-proxy/src/`, namespace `mariadb.failpoint.proxy`, app `mariadb-failpoint-proxy`, entry `mariadb.failpoint.proxy::service_main`, declared in `drift/manifest.json`. Built/certified here and consumed by the app harness as an external process (like uflowsd), not under `tools/` (which is Python helpers). Transparent forwarding with framed packet-level COM_QUERY inspection; framing inlined (trivial 4-byte MariaDB header), so no `mariadb-wire-proto` dep. Control plane raw TCP / JSON Lines, no `drift-web`.
  - DONE (scaffold): `failpoint-proxy/src/main.drift` + manifest entry compile; `tools/emit_test_plan.py` artifact inference extended for `failpoint-proxy/`; stdlib confirmed (`std.net` listen/accept/connect, `std.json`, `std.concurrent` spawn).
  - DONE (slice 1 — transparent forwarding + framing): `service_main` arg parsing (`--data-host/-port`, `--backend-host/-port`, `--control-host/-port` placeholder); data listener + accept loop spawning one detached handler vthread per client; per-client backend connect; bidirectional forwarding (server→client on a spawned vthread, client→server inline) over `conc.Arc<net.TcpStream>` shared both ways. Client→server path feeds `framing.drift`, a MariaDB packet reassembler that observes exact `COMMIT` (`COM_QUERY` 0x03 + trimmed/`;`-optional/case-insensitive "COMMIT") across split/coalesced reads — no raw-chunk matching, no stream rewriting, TLS/compression-plaintext assumption documented. No fault action yet (observation logs only). Compiles + links clean; `failpoint-proxy/tests/unit/framing_test.drift` passes (whole/split/coalesced/variant/non-match/oversized-realign).
  - Review fixes applied: (1) teardown is symmetric — whichever direction ends first calls `_close_pair` to close both fds, so a backend-EOF-while-client-idle no longer hangs the handler; (2) framer is a streaming state machine that skips an oversized packet's payload byte-exactly (`skip` counter, cap `MAX_OBSERVE_PAYLOAD`), so it can never desync/false-match — covered by `scenario_oversized_realign` (COMMIT bytes embedded in a skipped payload are not matched; a real COMMIT after realigns); (3) all `--*-port` and `--*-host` flags fail fast (exit 2) on a missing value (flag as last token or followed by another `--flag`) or, for ports, a non-integer / out-of-1..65535 value — via a shared `_lookup_arg` (Absent/MissingValue/Found), instead of silently defaulting.
  - `just test` gate: green (205 ok / 0 failed) validated the `RpcCommitError` pivot end-to-end (unit + live e2e + ASAN + memcheck), including `commit_error_taxonomy_test`.
  - Follow-up done: `failpoint-proxy/tests/unit` wired into `tools/emit_test_plan.py` `UNIT_ROOTS`, so `framing_test` now runs in the normal gate (base/asan/memcheck; plan is now 210 jobs) — no manual side path.
  - Non-deploy local build path added: `just build-app <APP>` (→ `tools/emit_test_plan.py app --app <APP>`) compiles a `kind:app` from local source using the manifest's modules + `entry_point`, emitting `build/local-app/<APP>` — no deploy/sign/publish.
  - DONE (slice 1 live smoke — the remaining DoD): built the proxy binary with `just build-app` (no deploy/sign/publish), ran it 43306→mdb114-a:34114, and drove the real mariadb-rpc client through it via `packages/mariadb-rpc/tests/e2e/live_proxy_passthrough_smoke_test.drift` (manual; not in the curated gate list). connect/auth/query(`sp_add`→3)/`COMMIT` all pass through; the proxy emits `ev:"commit_observed"` (exact-COMMIT framing confirmed on a real client's wire bytes). Ran twice → next-client recovery proven. A raw `mysql` client through the proxy also works. Review-focus items all addressed: socket lifecycle + symmetric teardown, half-close/first-closer, double-close backstop, next-client recovery.
  - Structured logging (stdlib `std.log`, JSON Lines, house string-valued attrs; low-noise, never logs SQL/auth/packets): slice-1 events `proxy_start`, `client_accept` (conn_id + `client` peer address via `TcpStream.peer_addr()`, added in staged 0.33.67/abi19), `backend_connect` (conn_id, backend, ok), `commit_observed` (conn_id, ordinal), `conn_close` (conn_id, first, reason ∈ eof|read-error|write-error, bytes_c2s, bytes_s2c, commits). Pumps now return a `PumpResult{reason,bytes,commits}`. Verified live: all five events emit as JSON Lines and the smoke passes. S5 reuses the same style for the fault/control-plane events: `failpoint_arm`, `failpoint_fire`, `failpoint_clear`, `control_op`, `control_close` (see S5 below — there is no separate "server response dropped" event; the swallowed-response byte count is intentionally unmeasured, see `server_response_bytes_dropped`). Note: attrs are string-valued (e.g. `"data_port":"43306"`) per the runner's house idiom, not typed JSON numbers/bools.
  - Runtime note: `main()` is the root VT (`drift_run_main_on_vt` spawns it; the OS thread just joins), so socket I/O from `service_main` parks on the epoll reactor like any VT. The slice-1 detached-spawn bug (dropping a `VirtualThread` handle abandoned a not-yet-run task) was a real toolchain issue on ≤0.33.64/abi18 — now FIXED in staged 0.33.67/abi19 (drop abandons only the result handle, not submitted work). So the proxy uses genuine detached **spawn-per-client concurrency** (each handler on its own vthread + cloned Logger via `logger.derive().build()`). Verified concurrent: two 1s clients finish in ~1s (not ~2s serial), handlers on distinct vtids. No one-at-a-time limitation.
  - Toolchain migration to staged `drift-0.33.67+abi19` (cert in progress): existing code compiles clean on abi19; both prior blockers resolved there (spawn-drop + `peer_addr`). New abi19 requirement (undocumented): `--entry` targets must be `pub` — migrated all 51 test `fn main` → `pub fn main`, the app `service_main` → `pub`, and `tools/emit_test_plan.py` entry-detection regex to accept `pub`. `pub fn main` is forward-safe (compiles on abi18 too). Flagged the pub-entry change back to the toolchain team.
  - Concurrent-client support is DONE (detached spawn-per-client on abi19; `TOOLCHAIN-spawn-drop-question.md` resolved). The failpoint registry, nth-from-arm COMMIT matching, JSON Lines control plane, and fault action that used to be tracked here as slices 2-3 TODO are DONE — see S5 below; remaining work is S7/S8.
- [x] S5. Newline-delimited JSON control API implemented (`failpoint-proxy/src/control.drift`, module `mariadb.failpoint.proxy.control`), including S6's observability ops (delivered together — one JSON dispatch table, not worth splitting): `arm` (label + optional domain + `match.command`/`match.sql`/`match.nth` + fixed `action`/`times=1`, validated against the only supported shape — `COM_QUERY`/`COMMIT`/`drop_server_response_after_forward`), `status`, `list`, `assert_all_fired`, `clear` (full registry reset), `health`. Registry is `conc.Arc<control.Registry>` (`Mutex<RegistrySlot>` of `Array<FailpointEntry>`, linear-scan — matches this repo's existing `Array`/`Mutex` house style, no `HashMap` precedent). `match.nth` counts matching COMMITs from arm-time, per-entry (`seen_since_arm`), exactly as specified. `handle_line(reg, line) -> String` is the pure JSON-in/JSON-out core (no sockets, no logging) — unit-tested directly in `failpoint-proxy/tests/unit/control_test.drift` (arm/status/nth=1/nth=2/assert_all_fired/clear/list/health/bad-json/unknown-op/unknown-id/bad-args), the same pattern as `classify_commit_wire_tag` and the packet framer. The control TCP listener (`run_control_loop` + per-connection line reader, byte-accumulate-until-`\n`, `too-long` closes the connection since a boundary-less buffer can't safely resync) runs as a second detached accept loop in `service_main`, independent of the data listener.
  Fault action wired into the data path: `_pump_client_to_server` (`main.drift`) calls `control.observe_commit` once per observed exact-COMMIT; a fire sets a per-connection `conc.Arc<AtomicBool>` swallow flag *before* forwarding the triggering COMMIT (so the backend cannot have produced a response yet when the flag becomes visible — race-free by construction, not by timing luck), then forwards **only through the end of the triggering COMMIT packet** (see the offset-exactness fix below — not the whole socket read), then closes both sockets itself and returns `"failpoint-fired"`. `_pump_passthrough` (server→client) checks the same flag right after each read and drops anything read once it's set. Logs `failpoint_fire` (conn_id/id/label/domain/matched_nth) on fire; control requests log named `failpoint_arm`/`failpoint_clear` events (conn_id/ok/id/label), everything else a generic `control_op` (conn_id/op).
  Two documented v1 approximations (diagnostics only, not gated by any DoD item): `bytes_forwarded_to_server` is hardcoded `11` (exact size of a bare `COMMIT` packet, which is what `wire.commit()` always emits — not a generic measurement); `server_response_bytes_dropped` is `-1` ("not measured" sentinel — the proxy deliberately never reads the swallowed response, since reading it would itself risk forwarding part of it).
  Verified live end-to-end against `mdb114-a` (not yet in the automated gate — see S8): armed via raw TCP control (`{"op":"arm",...}`), drove `packages/mariadb-rpc/tests/e2e/live_proxy_passthrough_smoke_test.drift`'s real `mariadb-rpc` client through the proxy, `conn.commit()` returned `Err` (exit 17) exactly as expected; proxy logs showed `commit_observed` → `failpoint_fire` → `conn_close{reason:"read-error"}`; `status` showed `fired:true, armed:false, fire_count:1, matched_client:<real addr>`; `assert_all_fired` flipped to `ok:true`; re-running the same smoke with nothing armed passed cleanly (exit 0), proving the one-shot doesn't wedge the proxy or consume an unrelated future COMMIT.
  Review round fixed four issues found against this slice: (1) `control.drift` was missing from `drift/manifest.json`'s `modules` list — local/test builds masked this because the test harness source-walks the whole `failpoint-proxy/src/` directory, but `drift deploy`/`drift author` read the manifest list literally; added the module and re-minted `drift/mariadb-failpoint-proxy.author-claim`. (2) `assert_all_fired` returned `ok:true` on an empty registry (nothing ever armed, e.g. right after `clear`) — contradicted the plan's own "forgot-to-arm fails loudly" requirement; fixed to `ok:false, error:"not-armed"` whenever `armed_count == 0` (this is what the closed error-code set's `not-armed` tag was for). (3) Protocol-exactness gap: the fault action forwarded the ENTIRE socket read chunk before closing, not just the bytes through the triggering COMMIT packet — a coalesced `[COMMIT][next packet]` read (pipelining, or kernel coalescing; not expected from the synchronous `mariadb-rpc` client today, but a real gap for a shared proxy tool) would have forwarded the trailing bytes too. Fixed properly rather than documented away: `framing.drift` gained `feed_and_locate_commits` (returns `CommitFeedResult{commits, commit_ends}` — the chunk-relative end offset of each matched COMMIT, in order; `feed_and_count_commits` is now a thin wrapper over it), and `_pump_client_to_server` forwards only through the firing commit's `commit_ends[k]` offset, discarding anything after it in the same read. New `framing_test.drift` scenario `scenario_locate_coalesced_commit_then_other` pins a coalesced `[COMMIT][SELECT]` read and asserts the returned boundary is exactly `commit.len`. (4) Control-plane logging was thinner than this section's own language (generic `control_op` only) — added named `failpoint_arm`/`failpoint_clear` events.
  Also discovered (unrelated to the above, found while re-verifying `just deploy` after the manifest fix): `drift deploy`'s baseline app smoke runs `<bin> --help` and requires a return within 30s (any exit code; only a hang or signal-death fails it) — the proxy is a daemon with no such fast-path, so it hung and `just deploy` (and therefore `just stress`/`just perf`, both `: deploy`) failed. Pre-existing since S4 (unrelated to control.drift — nobody had run real `deploy` against this artifact before; S4 deliberately used `just build-app` to avoid it). Fixed with a minimal `--help`/`-h` handler in `service_main` that prints usage and returns 0 before binding any listener. `just deploy` now publishes all 3 artifacts cleanly.
- [ ] S7. Add an e2e test using real pool `max_conns = 1`: arm proxy, trigger COMMIT, assert proxy fired, assert app/client sees ambiguous write error, assert next acquire reconnects.
- [ ] S8. Add automated proxy-process gate coverage so `just test` exercises the actual `mariadb-failpoint-proxy` binary, not only pure framing/control logic. Minimum v1: build local app, start proxy against `mdb114-a`, run the passthrough live client through it, assert JSONL lifecycle events including `commit_observed`, and tear down reliably. Later, after S7, add the armed COMMIT-loss proxy-process gate case. See `work/mariadb-rpc-failpoints/PROXY-GATE-HARNESS.md`.
- [ ] S9. Document app usage: selected DB domain endpoint points at proxy, non-target domains stay direct, control API over raw TCP, no failpoint code in app binary.
- [ ] S10. Share with app team and K before implementation.

S1-S3 are independently shippable and production-safe. They improve behavior for real commit transport failures even before the proxy exists. The proxy e2e then validates exactly the classification path S1-S3 add.

## 12. Consumer follow-up

Known app-side issue from the app team draft:

- `conn.call(...)` and `rpc.skip_remaining(...)` errors currently map to retriable backend-unavailable style errors.
- `rpc.commit(...)` errors currently map to `BackendRejected` in relevant coordinator helper paths.
- Lost ack on COMMIT must not be classified as server rejection.

Once this repo exposes the typed commit error, consumer commit helpers should branch on `RpcCommitError.kind`:

- `AmbiguousWrite` -> retriable/reconcile path;
- `ServerRejected` -> backend rejected;
- `NotSent` -> retry as not-applied, per caller semantics.

H2 is not blocked on new bookkeeper business behavior: the ledger DAO already has a commit-unknown path and deliberately does not roll back after that classification. The expected bookkeeper follow-up is narrower: branch the relevant commit helper on `commit()`'s new `RpcCommitError.kind` (`AmbiguousWrite` → reconcile, `ServerRejected` → backend-rejected, `NotSent` → retry as not-applied) once available.

## 13. Security and operations note

The raw TCP control API can arm destructive fault injection. It must be treated as test infrastructure.

Default recommendations:

- bind control to loopback by default;
- require explicit config to bind wider than loopback;
- log every arm/fire/clear with label and remote control address;
- support an auth token if practical;
- never run this proxy in front of production traffic unless it is an intentional smoke-test environment with an explicit runbook.

The core safety property is not that TCP control is impossible to misuse. It is that neither `bookkeeper` nor production `mariadb-rpc` contains an arming API.

## 14. Open questions

- Can the proxy reuse framing logic or fixture knowledge from `tools/wire_capture_proxy.py`, or should it be implemented separately?
- Should the proxy close client-side with FIN or RST by default? Recommendation: abrupt close; make mode configurable only if tests need to distinguish FIN/RST.
- Should timeout-style ack loss (`drop-and-hold` until client timeout) be added after the close-mode MVP?
- If both DB domains ever need to be behind proxies at once, should the harness run one proxy per domain or add an explicit domain discriminator?
- For H1, what exact `nth` value should be armed in each Singular scenario (`start` -> `complete`, reclaim/resume path)? Default assumption is `nth = 2` for normal `complete`, but the harness should prove it with proxy observability.

Cross-team item with bookkeeper (only one remaining):

- Pre-cert H2 end-to-end: bookkeeper wants to build against `mariadb-rpc`'s new commit-error contract before a cert cut, to exercise H2 end-to-end. Acceptable on the condition that the contract — `commit() -> core.Result<Void, RpcCommitError>` with `RpcCommitError { kind: RpcCommitErrorKind, cause_tag: String }` and `RpcCommitErrorKind { AmbiguousWrite, NotSent, ServerRejected }` — is frozen as of S1/S2, so the consumer branch does not break at cert. The mariadb-side reply should confirm the contract is pinned.

Resolved:

- Proxy ownership: decided — the proxy is this repo's product as a certified `kind:app` at `failpoint-proxy/src/` (not under `tools/`; `tools/wire_capture_proxy.py` is precedent only), raw TCP / JSON Lines control, no `drift-web`. App team recommended app/workflows; we keep it here and the app harness consumes the certified binary as an external process. Because it is ours, nth-COMMIT is an in-scope deliverable, not a cross-team ask.
- Remote raw TCP control: local-by-default with explicit opt-in for remote smoke environments. Loopback + SSH/port-forwarding is the default; remote bind is opt-in only.
- `COMMIT WORK` and other COMMIT spellings: out of scope for v1. Exact `COMMIT` text matching is sufficient because current `mariadb-rpc` emits exact `COMMIT`. Revisit only if the emitted text changes.
- Exact next-COMMIT matching is sufficient for H2 ledger tests. H1 needs nth-COMMIT matching because the singular domain can emit an earlier `start` COMMIT before `complete`.

## 15. Definition of done

- Production `mariadb-rpc` has no failpoint arming API.
- `mariadb-rpc` exposes production-safe ambiguous commit classification.
- Proxy can arm one-shot exact-COMMIT ack loss over raw TCP control.
- Proxy reassembles MariaDB wire packets before matching; it does not match raw TCP chunk bytes.
- Proxy forwards the full COMMIT request before dropping/closing the client response path.
- Proxy closes both client-side and server-side sockets after firing.
- Proxy accepts a fresh client connection after firing, and the one-shot failpoint does not consume the retry/reconcile COMMIT.
- Proxy supports nth-COMMIT matching before H1 is considered covered; H2 can run with the default `nth = 1`.
- Caller receives a classifiable ambiguous write transport error.
- Connection is poisoned/dead and the pool discards it.
- Next acquire reconnects cleanly.
- Tests can assert the proxy failpoint fired exactly once and fail loudly if armed but never fired.
- App team can run the same bookkeeper binary against the proxy by changing DB endpoint config.
- App harness can route `singular` and `bookkeeper_db` independently so a one-shot COMMIT failpoint fires in the intended DB domain.
- H1/H2 boundary tests run sequentially under their single-connection config, separate from concurrent stress/redispatch cases that use normal multi-connection config.
