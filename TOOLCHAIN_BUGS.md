# Toolchain Bugs

Known toolchain issues that affected this project.

## DEPLOY-001: `drift deploy` does not pass trust store to co-artifact build step

**Status:** fixed in 0.27.92

**Symptom:** `drift deploy` for a manifest with co-artifact dependencies
(e.g., `mariadb-rpc` depends on `mariadb-wire-proto`) fails on the
second artifact with:

```
error: no trusted keys configured for module namespace of '<sibling namespace>'
```

**Root cause:** `drift deploy` built a staged trust overlay that
authorized the deploying manifest's own namespaces, but only passed it
to the **smoke** step via `--trust-store`. The **build** step
(`_build_package` → `build_package_cmd`) received no `--trust-store`
flag. `driftc` fell back to its default `./drift/trust.json`, and if
that file did not exist, the build failed because the freshly-signed
sibling `.zdmp` could not be verified.

**Fix (0.27.92):** `drift deploy` now creates the staged trust overlay
before the build step and passes it to both build and smoke phases.

**Minimum toolchain version for this project:** 0.27.92+abi6
