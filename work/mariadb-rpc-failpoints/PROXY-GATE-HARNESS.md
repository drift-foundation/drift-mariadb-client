# Proxy gate harness follow-up

Status: DONE (S8, see PLAN.md). `tools/proxy_gate_smoke.py`, wired into `just test` via `flocker --key mariadb-mdb114-a`. Both the v1 gate smoke and the later S7-dependent second case below are implemented. Kept this doc as the historical record of the requirement rather than deleting it — if `just test` ever stops exercising the actual subprocess (e.g. someone routes around `proxy_gate_smoke.py`), treat that as a regression against everything below.

## Problem

`just test` currently exercises proxy internals such as packet framing and control-protocol logic, but it does not automatically exercise the actual `mariadb-failpoint-proxy` process as a running TCP proxy. The manual live passthrough smoke proves the path, but manual-only coverage is not enough for a binary that downstream projects such as Microflows may rely on during certification.

## Required v1 gate smoke

Add a first-class correctness-gate smoke that:

- builds `mariadb-failpoint-proxy` from local source, without deploy/sign/publish;
- starts it as a subprocess on a test data port, forwarding to `mdb114-a`;
- waits for readiness deterministically;
- runs the existing live proxy passthrough client through the proxy;
- captures proxy stderr JSON Lines;
- asserts the proxy emitted `proxy_start`, `client_accept`, `backend_connect`, `commit_observed`, and `conn_close`;
- tears the proxy down reliably;
- fails the gate on any missing event, client failure, startup failure, or teardown failure.

Readiness can start as retry-connect on the data port. Once the control API `health` op is stable, prefer that.

## Preferred implementation

Use a repo-local harness that is invoked by the normal gate and returns one exit code. A small Python harness is acceptable if the shared `drift_test_run.py` plan format cannot directly express "start long-lived process A, run client B, inspect A logs, stop A".

Avoid leaving this as a manual runbook or shell-only side path.

## Later S7 gate smoke

After the COMMIT-loss fault action and control API e2e exist, add a second proxy process smoke:

- arm one-shot `drop_server_response_after_forward`;
- drive a real `conn.commit()` through a real pool with `max_conns = 1`;
- assert the client receives `RpcCommitErrorKind::AmbiguousWrite`;
- assert the proxy fired exactly once;
- assert the next acquire/reconnect path can commit normally.

This second case validates the proxy as a failpoint tool, not just a transparent proxy.
