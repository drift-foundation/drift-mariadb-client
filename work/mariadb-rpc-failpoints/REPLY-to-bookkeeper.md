# Reply — mariadb-rpc side to bookkeeper, re: ambiguous-COMMIT failpoints

**From:** mariadb-rpc / mariadb-wire-proto (Drift Foundation)
**To:** PushCoin — bookkeeper app team
**Date:** 2026-06-30
**Re:** your update on H1/H2 routing, nth-COMMIT, max_conns=1 config, proxy ownership, and the A–E start

---

Thanks — all four points land. Confirmations and one ownership correction below.

## 1. Proxy is ours — H1 is not waiting on an external ask

We're keeping the fault-injection proxy as a product of this repo — a certified `kind:app` Drift artifact
(`mariadb-failpoint-proxy`, source at `failpoint-proxy/src/`, built and certified here like uflowsd), with a
raw-TCP / JSON-Lines control plane and no `drift-web` dependency. (`tools/wire_capture_proxy.py` is only a
framing reference, not where it lives.) Your harness consumes the certified binary as an external process — it
slots into `run_ledger_stress.py` like uflowsd — but the
wire-accurate framing and COMMIT matching live with the wire code here.

The practical consequence for you: **nth-COMMIT is our deliverable, not a cross-team ask.** H1 is blocked
only on us shipping the proxy, not on a separate request to the mariadb team. So your "two open items with the
mariadb team" collapse to one (the commit-error contract, item 3 below).

## 2. nth-COMMIT — confirmed, with arm-time counting

We'll implement `match.nth` (default `1`). The semantics we're pinning so H1 is deterministic:

- **counting origin is arm-time, not connection-open** — the counter starts at the `arm` ack and counts only
  matching COMMITs seen thereafter on the data listener;
- the first `n-1` matches pass through normally; only the nth fires; then one-shot auto-disarm;
- this is what makes it robust under `max_conns=1`, where the data connection often pre-exists (warmup / a
  prior op) and counting from connection-open would be off by the earlier traffic.

So H1 arms `nth=2` (singular `start` → `complete`); H2 keeps the default `nth=1`. Please still confirm the `2`
empirically via proxy observability (`match_count` / which match fired) rather than assuming it — the proxy
reports it so the test can assert it instead of trusting the ordering.

## 3. Pre-cert H2 end-to-end — yes, contract frozen

Build bookkeeper against the new commit-error contract before a cert cut; that's fine. We landed a typed,
commit-specific error (cleaner than classifiers on the shared `RpcError`), and we're freezing it as of S1/S2 so
your consumer branch won't break at cert:

```drift
pub variant RpcCommitErrorKind { AmbiguousWrite, NotSent, ServerRejected }
pub struct RpcCommitError { pub kind: RpcCommitErrorKind, pub cause_tag: String }
pub fn commit(...) nothrow -> core.Result<Void, RpcCommitError>
```

Branch on `kind` only; `cause_tag` is the original lower-level wire tag (e.g. `wire-read-failed`,
`tx-command-server-err`) for diagnostics — no string classification to parse. `AmbiguousWrite` is the
full-COMMIT-sent, ack-lost class; unknown/unexpected failures fold into it (conservative — reconcile is always
safe). Treat this shape as stable from S1/S2 onward; if anything has to change before cert we'll flag it to you
first, not silently at the cut. (Note: `commit()` now returns `RpcCommitError`, not `RpcError` — a deliberate
signature break, agreed while app/tests are the only consumers.)

## 4. H2 validates an already-coded path — agreed

Understood that the ledger DAO already implements commit-unknown (distinct tag, deliberately not rolled back),
so H2 isn't waiting on new bookkeeper behavior. The narrow follow-up we both expect: once S1–S3 land, branch
that commit helper on `RpcCommitError.kind` — `AmbiguousWrite` → retriable/reconcile, `ServerRejected` →
backend-rejected, `NotSent` → retry as not-applied. We've recorded that as the consumer follow-up in the plan (§12).

## 5. max_conns=1 config isolation — agreed

H1/H2 run as sequential single-operation boundary tests in their own `max_conns=1` config (keepalive off,
finite/fail-fast acquire), kept separate from the concurrent matrix and the concurrent-redispatch case D, which
stay on the normal multi-connection config. Noted in the plan so the harness wiring doesn't accidentally fold
them into one config.

## 6. Start A and C now — go

Yes, start the cheap unblocked cases in parallel with the proxy build:

- **A** (live-lease → 202, lease not stolen) and **C** (uflowsd restart mid-pending) are independent of the
  proxy — only H1/H2 depend on it — and need no new plumbing;
- run them in the **normal multi-conn config**, not the `max_conns=1` boundary config (they're not
  boundary-fault tests);
- the proxy is the long pole, so getting A/C moving now is the right use of the critical path.

A–E are unblocked and ready; H1/H2 gate on the proxy, which is on us.

## Sequencing from our side

1. **S1–S3 (typed `RpcCommitError` contract + regression tests)** — independently shippable and
   production-safe; we can land these ahead of the proxy, and they're what your pre-cert H2 branch builds on.
2. **S4–S7 (proxy + raw-TCP control + nth-COMMIT + e2e)** — the long pole for H1/H2; ours to build.

We'll ping you when S1–S3 is on a branch you can build against, and again when the proxy can arm a one-shot
exact-COMMIT (H2) and nth-COMMIT (H1).

— mariadb-rpc / mariadb-wire-proto
