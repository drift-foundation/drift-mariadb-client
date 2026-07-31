# tools/cert_deps.py — one derivation site for EXTERNAL `--dep name@version` pins
# in gate compiles. mariadb-client's adaptation of drift-workflows'
# tools/cert_deps.py, the validated reference shape named by build-orchestrator's
# 2026-07-31T043815Z announce (DRIFT_LANG_SRC retired; certify lane = one exec of
# the run toolchain's `drift lock emit --source-rebuild`, shipped in 0.33.92).
#
# Two lanes:
#
#   - STRICT (default, dev loop): the committed drift/lock.json is the
#     authoritative graph — exact versions read from its resolved map, falling
#     back to the manifest-declared version when the lock has no entry (the
#     emitter's historical behavior, unchanged). Stdlib-only.
#
#   - SOURCE-REBUILD (DRIFT_CERT_MODE=certify, exported by the orchestrator for
#     gate runs): the lock is EVIDENCE, not the graph authority. Resolution is
#     ONE EXEC of the run toolchain's own binary:
#         drift lock emit --artifact <name> --source-rebuild
#     which resolves via drift-lang's single source-rebuild authority
#     (resolve_source_rebuild — the same call path build/deploy/prepare take),
#     honoring DRIFT_RUN_SNAPSHOT + DRIFT_PKG_ROOT from the standard cert env.
#     stdout is exactly the `--dep` flags; evidence/diagnostics pass through to
#     stderr (the gate log's evidence trail); errors fail closed (non-zero exit,
#     empty stdout). A pre-0.33.92 toolchain rejects the flag at argument
#     parsing — that is the intended wrong-toolchain-for-the-lane signal; never
#     add a fallback, a version sniff, or a hand-rolled resolver here.
#
# The exec happens even when the artifact declares no external deps (all of ours
# today): a hardcoded empty flag list is a latent skew — if a dep ever appears,
# the gate must derive it correctly instead of silently compiling without it.
# Empty-stdout NO-DEPS and empty-stdout ERROR are distinguished by exit code.
#
# NOT routed through this shim: the stress/perf fixture pins naming our own
# packages deployed under build/deploy (emit_test_plan.deployed_dep_flags,
# perf_baseline.py). Those pin the packages UNDER TEST published as local
# fixtures — not upstream dep derivation — and are exempt from the
# "no hand-rolled --dep pins in certify mode" rule (drift-net-tls
# 2026-07-31T044103Z §2). Co-artifacts are likewise excluded here: source
# builds compile them from their src trees, never as pins.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _strict_versions(lock_path, artifact, exclude):
    if not Path(lock_path).exists():
        return {}
    with open(lock_path) as f:
        lock = json.load(f)
    resolved = (lock.get("artifacts", {}).get(artifact, {}) or {}).get("resolved", {}) or {}
    return {n: v["version"] for n, v in resolved.items() if n not in exclude}


def _certify_versions(manifest_path, artifact, exclude, env):
    root = env.get("DRIFT_TOOLCHAIN_ROOT")
    if not root:
        sys.exit("cert-deps: DRIFT_TOOLCHAIN_ROOT must be set for certify-lane dep derivation")
    drift = Path(root) / "bin" / "drift"
    if not drift.is_file():
        sys.exit(f"cert-deps: drift CLI not found at {drift}")
    proc = subprocess.run(
        [str(drift), "lock", "emit", "--artifact", artifact,
         "--manifest", str(manifest_path), "--source-rebuild"],
        capture_output=True, text=True, env=dict(env))
    # Evidence + diagnostics are the CLI's stderr contract — surface them verbatim.
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        sys.exit(f"cert-deps: `drift lock emit --artifact {artifact} --source-rebuild` failed "
                 f"(exit {proc.returncode}; toolchain >= 0.33.92 required)")
    tokens = proc.stdout.split()
    if len(tokens) % 2:
        sys.exit(f"cert-deps: `drift lock emit` stdout violates the --dep flags contract "
                 f"(odd token count): {proc.stdout!r}")
    versions = {}
    for flag, pin in zip(tokens[::2], tokens[1::2]):
        if flag != "--dep" or "@" not in pin:
            sys.exit(f"cert-deps: `drift lock emit` stdout violates the --dep flags contract: "
                     f"{proc.stdout!r}")
        name, _, ver = pin.partition("@")
        if name not in exclude:
            versions[name] = ver
    return versions  # empty is legitimate: every emitted dep excluded, or none declared


def external_pins(manifest_path, artifact, lock_path, declared, exclude, env=None):
    """['name@M.N.P', ...] external-dep pins for a source build of `artifact` —
    the ONE derivation site both lanes go through.

    declared: {name: manifest-declared version} for the artifact's external
    (non-co-artifact) package_deps. exclude: co-artifact names (compiled from
    source, never pinned)."""
    env = os.environ if env is None else env
    exclude = set(exclude)
    if env.get("DRIFT_CERT_MODE") == "certify":
        versions = _certify_versions(manifest_path, artifact, exclude, env)
        missing = sorted(set(declared) - set(versions))
        if missing:
            sys.exit(f"cert-deps: declared dep(s) {missing} absent from certify-lane "
                     f"resolution for {artifact!r}")
        # Emit the resolver's full external set (transitives included), not just
        # the declared list — it matches the flag list `drift build` passes.
        return [f"{n}@{versions[n]}" for n in sorted(versions)]
    versions = _strict_versions(lock_path, artifact, exclude)
    return [f"{n}@{versions.get(n) or declared[n]}" for n in sorted(declared)]
