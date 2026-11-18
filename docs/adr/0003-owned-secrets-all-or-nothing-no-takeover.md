# 0003. The Secret is owned by its ConfigMap, written all-or-nothing, and never taken over

## Status

Accepted · 2026-11-17

## Context

The operator writes Secrets that applications depend on. Three ways that goes wrong: a
Secret outlives the ConfigMap that asked for it (orphans nobody dares delete); a Secret is
half-updated (new username, old password); a ConfigMap names a Secret that already exists
for something else (a TLS certificate) and the operator overwrites it.

## Options considered

### Option A: create-or-replace by name (naive)

- ✅ Simple.
- ❌ All three problems.

### Option B: track Secrets in the operator's own state (a database, a ConfigMap of mappings)

- ✅ Explicit bookkeeping.
- ❌ A second source of truth that can drift from the cluster; deletion still needs code.

### Option C: ownerReferences + labels + content hash, checked on every write

- ✅ `ownerReferences` (controller: true) to the ConfigMap: Kubernetes garbage collection
  deletes the Secret with the ConfigMap; no deletion code at all.
- ✅ Before writing, an existing Secret must carry our label AND be controlled by THIS
  ConfigMap's UID; otherwise refuse with an Event (`refusing to overwrite`).
- ✅ All values are fetched first; any failure leaves the Secret untouched.
- ✅ A SHA-256 of the values in an annotation: no write (and no Event) when nothing changed;
  a manual edit of the Secret is restored on the next resync.
- ❌ Removing the `sync` annotation leaves the Secret in place (still owned, deleted with the
  ConfigMap). Documented.

## Decision

**Option C**, with `resourceVersion` on updates (optimistic locking: a concurrent change makes
the write fail and retry instead of silently overwriting).

## Consequences

- **Good:** the e2e test proves GC on deletion and that a foreign Secret is left untouched.
- **Bad:** the content hash is visible to anyone who can read the Secret's metadata; it is a
  hash of the values, not the values (and SHA-256 of high-entropy secrets is not reversible).
