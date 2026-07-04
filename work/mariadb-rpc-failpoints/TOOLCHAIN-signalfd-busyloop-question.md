# Toolchain bug — SIGTERM/SIGINT/SIGUSR1 registered via signalfd but never drained: process busy-spins forever and never exits

**From:** mariadb-client (failpoint-proxy work)
**To:** Drift toolchain / runtime team
**Toolchain:** certified `drift-lang@5c6e03f` (abi19, `libdrift_rt_abi19.a`) — `~/opt/drift/certified/current`
**Date:** 2026-07-03

## Summary

Any long-running Drift binary on this toolchain — not just ours — appears to
**never exit on SIGTERM, SIGINT, or SIGUSR1**, and instead **busy-spins one CPU
core at ~100,000+ iterations/second, forever**, the instant one of those
signals is delivered. This is a runtime bootstrap issue (happens before
`main()`/`service_main()` runs), reproduced with a 6-line program that does
nothing but sleep — no sockets, no spawned virtual threads, no application
logic. `SIGKILL` is currently the only way to stop an affected process.

We noticed this while building `mariadb-failpoint-proxy` (a `kind:app` daemon
in this repo): our own docs claimed "plain SIGTERM is sufficient" for
shutdown, since the proxy installs no signal handling of its own. It doesn't
hold — SIGTERM neither stops the process nor merely delays it; it makes
things actively worse (permanent busy-spin) while the process keeps running.

## Isolated repro (no sockets, no spawn, no proxy code)

`work/mariadb-rpc-failpoints/sigterm-signalfd-probe/main.drift`:

```drift
module main;

import std.console as console;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
	console.println("sleeping...");
	conc.sleep(conc.Duration(millis = 60000));
	console.println("woke up");
	return 0;
}
```

```bash
driftc --entry main::main -o sigterm-min main.drift
./sigterm-min &
PID=$!
kill -TERM $PID
sleep 30
kill -0 $PID && echo "still alive"   # -> still alive, every time
```

We also reproduced it against the real `mariadb-failpoint-proxy` binary (this
repo's own long-running daemon, no signal handling of its own) — identical
behavior, so this isn't specific to the sleep-only case.

## What's actually happening (traced with `strace -f -tt`)

At process bootstrap, **before any application code runs**:

1. `rt_sigprocmask(SIG_BLOCK, [INT USR1 TERM], ...)` — blocks these three
   signals process-wide (confirmed on every thread via
   `/proc/<pid>/task/*/status`'s `SigBlk`).
2. `signalfd4(-1, [INT USR1 TERM], 8, SFD_NONBLOCK) = 3` — creates a
   `signalfd` for exactly those three signals.
3. `epoll_ctl(6, EPOLL_CTL_ADD, 3, {events=EPOLLIN, data=0x3})` — registers
   that signalfd into the same epoll instance (fd 6) the reactor uses for
   sockets/timers/eventfd wakeups.

Then, for the rest of the process's life, **nothing ever calls `read()` on
fd 3.** `signalfd` is level-triggered: once a blocked signal is pending, the
fd stays readable until something reads it out. Since nothing does, the
instant SIGTERM/SIGINT/SIGUSR1 arrives:

- `epoll_wait` returns immediately with `{events=EPOLLIN, data=0x3}`.
- The reactor loop takes no action on it (nothing reads or otherwise drains
  the signal), loops back, and calls `epoll_wait` again — which again
  returns immediately. Forever.

Full excerpts (startup, the exact transition at the moment SIGTERM lands, a
steady-state sample, and the per-thread signal masks) are in
`work/mariadb-rpc-failpoints/sigterm-signalfd-probe/strace-excerpt.txt`. Key
data points from that trace (167s window against the real proxy binary,
20,694,892 total syscalls logged):

- **20,694,853** of those syscalls are exactly this `epoll_wait` spin
  (~124,000/sec, ~10µs/iteration).
- **0** — zero — `read(3, ...)` calls anywhere after the signalfd was
  created.
- The last properly-blocking `epoll_wait` returned at `17:30:49.914871`
  (~16s before our `kill -TERM`, harmless one-off startup wakeup); the next
  logged call is at `17:31:05.800471` — ~0.6ms after we sent SIGTERM at
  `17:31:05.799896649` — and the process spins non-stop from that instant
  until we `SIGKILL`ed it ~2m47s later.

## Why we think this is a bug, not intended behavior

- Blocking a signal and multiplexing it via `signalfd`+`epoll` is a
  legitimate, common pattern *if something reads the fd and acts on it*
  (e.g. translates it into an orderly shutdown). Registering it and never
  reading it can't be intentional — it can only ever busy-spin once the
  signal fires, with no possible useful outcome.
- We don't see this documented anywhere in `doc/stdlib/std_concurrent.md` or
  the runtime docs as intended signal-handling behavior a Drift program
  should expect.
- It reproduces identically in a 6-line program with zero application logic,
  so it isn't something we're triggering via a specific stdlib call (our
  probe uses only `conc.sleep`) — it's present in every binary's bootstrap on
  this toolchain.

## Impact

- Any long-running Drift `kind:app` daemon cannot be stopped with a plain
  `SIGTERM`/Ctrl-C (`SIGINT`) once it's past startup — both silently make
  things worse (start a busy-spin) rather than requesting shutdown.
- `SIGKILL` still works fine (confirmed) — so nothing is un-killable, but any
  orchestration that expects a graceful `SIGTERM` (systemd, Docker, process
  supervisors, our own `tools/proxy_gate_smoke.py`) gets a busy-spinning
  zombie for however long its grace period is before falling back to
  `SIGKILL`.
- CPU cost while spinning is real: ~124,000 syscalls/sec pegs a full core.

## What we need / questions

1. Is this the intended signal-handling design (block signals process-wide,
   multiplex via signalfd+epoll), just missing the "read the signalfd and act
   on it" half? Or is the whole signalfd registration itself unintended
   runtime scaffolding (e.g. dead code from an in-progress
   graceful-shutdown feature)?
2. If intended: what's the supported way for an app (`service_main`) to
   observe/handle SIGTERM today, so we can implement our own shutdown instead
   of relying on the runtime's (currently non-functional) default? We don't
   see a documented API for this in `std_concurrent`/`std_runtime`.
3. Given `mariadb-failpoint-proxy`'s own docs currently claim "plain SIGTERM
   is sufficient" (true for *stopping* it, since SIGKILL still works, but
   false for *graceful* shutdown), should we correct that doc now, or hold
   until we know the intended fix/API so we document the right thing once?
4. Worth checking whether this also affects the toolchain's own long-running
   tooling (anything using `driftc`/`drift`'s own daemonized paths, if any) —
   we only tested app-level binaries.

## Repo-local status (not blocking)

Not currently blocking any of our gates — `tools/proxy_gate_smoke.py`'s
`stop_proxy()` already had an independent `SIGKILL` fallback after a grace
period, so `just test` is unaffected. We're holding off correcting
`docs/failpoint-proxy-usage.md`'s SIGTERM claim until we hear back, per
question 3 above.
