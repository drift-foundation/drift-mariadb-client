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

Use an external MariaDB wire proxy for fault injection. Keep `mariadb-rpc` changes limited to production-safe error taxonomy and classifiers.

Topology:

```text
bookkeeper -- MariaDB protocol --> proxy -- MariaDB protocol --> real MariaDB
test harness -- TCP control API --> proxy
```

Preferred ownership:

- implement the proxy in this repo as `tools/` test infrastructure, because this repo owns the MariaDB wire protocol details and already has capture-proxy precedent;
- keep the proxy control plane raw TCP / stdlib-only so this repo does not take a `drift-web` dependency;
- move the proxy to app/workflows only if the control plane becomes REST/web and needs `drift-web` or other app-stack dependencies.

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
- Exact `COMMIT` matching is sufficient for v1.
- Proxy ownership in this repo is preferred if the control plane is raw TCP and does not add `drift-web`; otherwise it belongs in app/workflows. Production `mariadb-rpc` still remains taxonomy/classification only.

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

## 5. Production-safe mariadb-rpc work

This repo should not grow in-process failpoint arming. It should grow safe classification.

Current gap: `RpcConnection.commit()` collapses every wire commit error into `rpc-wire-commit-failed` and puts the real wire tag in the message (`packages/mariadb-rpc/src/lib.drift:546-550`). The wire layer already has more shape: server commit rejection reaches `tx-command-server-err` from `_query_expect_ok` (`packages/mariadb-wire-proto/src/lib.drift:482-493`), while transport/read failure marks the session dead and returns the transport tag (`packages/mariadb-wire-proto/src/lib.drift:702-704`).

The RPC layer must preserve enough taxonomy for consumers to distinguish:

| Class | Boundary | Meaning | Consumer handling |
|---|---|---|---|
| ambiguous commit outcome | `COMMIT` request fully sent, acknowledgement lost | commit may or may not be durable | retriable/reconcile |
| request not fully sent | failure before full request write completes | definite not-applied at this request boundary | safe to retry as not-applied |
| server rejected | server response/SQL error received | definite server rejection | non-retriable rejection where appropriate |

The proxy harness directly exercises the ambiguous row by forwarding zero server-response bytes after the full COMMIT request, so the client fails in `session_read_packet` and the wire layer marks the session dead. Server rejection is a different structural path: `_query_expect_ok` can only produce `tx-command-server-err` after a real server `ErrPacket` is decoded. The proxy cannot accidentally turn the ambiguous case into a server-rejected case.

The proxy does not realistically exercise "request not fully sent" for COMMIT. An 11-byte COMMIT command is normally handed to the OS as one small write, and the proxy sits on the response side after it receives the client packet. Cover this class with unit/transport-level tests or explicit synthetic write-failure tests, not by adding a proxy mode that tries to create partial COMMIT writes.

Recommended classifier:

```drift
pub fn is_ambiguous_write_error(e: &RpcError) nothrow -> Bool
```

Recommended tag shape:

```text
rpc-wire-ambiguous-write
```

This is production-safe: it classifies real failures correctly and does not provide any way to arm a failure.

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
    "sql": "COMMIT"
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
  "bytes_forwarded_to_server": 11,
  "server_response_bytes_dropped": 7
}
```

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
- `Singular.complete` COMMIT unknown: route `singular` through the proxy and keep `bookkeeper_db` direct.

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

- nth matching COMMIT;
- client remote address;
- connection ordinal;
- DB domain, if the proxy or harness can supply it out-of-band;
- SQL comment marker if a caller can safely add one;
- transaction label if exposed outside the SQL path.

Start with label + one-shot + exact COMMIT matching.

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

## 11. Step plan

- [ ] S1. Production-safe RPC taxonomy: stop flattening all `wire.commit()` errors into only `rpc-wire-commit-failed`; preserve ambiguous-vs-server-rejected distinction.
- [ ] S2. Add RPC classifier, e.g. `is_ambiguous_write_error`.
- [ ] S3. Add regression tests for commit server rejection vs commit transport/read failure classification. Cover "request not fully sent" separately with unit/transport-level or synthetic write-failure tests; do not expect the proxy to produce partial COMMIT writes deterministically.
- [ ] S4. Implement a MariaDB wire proxy in this repo under tooling for transparent forwarding with framed packet-level COM_QUERY inspection. Keep control raw TCP/no `drift-web`; if the design later needs REST/web, move that proxy to app/workflows instead. Borrow framing lessons from `tools/wire_capture_proxy.py`.
- [ ] S5. Add newline-delimited JSON over raw TCP control API for one-shot `drop_server_response_after_forward` on exact COMMIT.
- [ ] S6. Add proxy observability and the `assert_all_fired`/`list`/`status` control ops.
- [ ] S7. Add an e2e test using real pool `max_conns = 1`: arm proxy, trigger COMMIT, assert proxy fired, assert app/client sees ambiguous write error, assert next acquire reconnects.
- [ ] S8. Document app usage: selected DB domain endpoint points at proxy, non-target domains stay direct, control API over raw TCP, no failpoint code in app binary.
- [ ] S9. Share with app team and K before implementation.

S1-S3 are independently shippable and production-safe. They improve behavior for real commit transport failures even before the proxy exists. The proxy e2e then validates exactly the classification path S1-S3 add.

## 12. Consumer follow-up

Known app-side issue from the app team draft:

- `conn.call(...)` and `rpc.skip_remaining(...)` errors currently map to retriable backend-unavailable style errors.
- `rpc.commit(...)` errors currently map to `BackendRejected` in relevant coordinator helper paths.
- Lost ack on COMMIT must not be classified as server rejection.

Once this repo exposes the classifier, consumer commit helpers should branch:

- ambiguous commit transport loss -> retriable/reconcile path;
- commit server error response -> backend rejected;
- pre-send/not-sent transport failure -> retry according to caller semantics.

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

- Proxy ownership: preferred path is this repo under tooling with raw TCP/no `drift-web`. If the control plane needs REST/web via `drift-web`, put it in app/workflows. This repo should not grow a `drift-web` dependency.
- Can the proxy reuse framing logic or fixture knowledge from `tools/wire_capture_proxy.py`, or should it be implemented separately?
- Should the proxy close client-side with FIN or RST by default? Recommendation: abrupt close; make mode configurable only if tests need to distinguish FIN/RST.
- Should timeout-style ack loss (`drop-and-hold` until client timeout) be added after the close-mode MVP?
- If both DB domains ever need to be behind proxies at once, should the harness run one proxy per domain or add an explicit domain discriminator?

Resolved by app team:

- Remote raw TCP control: local-by-default with explicit opt-in for remote smoke environments. Loopback + SSH/port-forwarding is the default; remote bind is opt-in only.
- `COMMIT WORK` and other COMMIT spellings: out of scope for v1. Exact `COMMIT` matching is sufficient because current `mariadb-rpc` emits exact `COMMIT`. Revisit only if the emitted text changes.

## 15. Definition of done

- Production `mariadb-rpc` has no failpoint arming API.
- `mariadb-rpc` exposes production-safe ambiguous commit classification.
- Proxy can arm one-shot exact-COMMIT ack loss over raw TCP control.
- Proxy reassembles MariaDB wire packets before matching; it does not match raw TCP chunk bytes.
- Proxy forwards the full COMMIT request before dropping/closing the client response path.
- Proxy closes both client-side and server-side sockets after firing.
- Proxy accepts a fresh client connection after firing, and the one-shot failpoint does not consume the retry/reconcile COMMIT.
- Caller receives a classifiable ambiguous write transport error.
- Connection is poisoned/dead and the pool discards it.
- Next acquire reconnects cleanly.
- Tests can assert the proxy failpoint fired exactly once and fail loudly if armed but never fired.
- App team can run the same bookkeeper binary against the proxy by changing DB endpoint config.
- App harness can route `singular` and `bookkeeper_db` independently so a one-shot COMMIT failpoint fires in the intended DB domain.
