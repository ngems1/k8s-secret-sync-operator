# 0001. A Python operator on kopf: event-driven, with a periodic resync

## Status

Accepted · 2026-11-17

## Context

The operator must react when an annotated ConfigMap is created, changed or deleted, and must
also notice when a value changes in the cloud store (a rotation), which produces no Kubernetes
event at all. The course is Python-first, and the challenge allows "operator (or controller)".

## Options considered

### Option A: a polling script (the naive version)

- ✅ 40 lines, no framework.
- ❌ Lists every ConfigMap in the cluster every 30 s: API-server load grows with the cluster.
- ❌ No retries, no backoff: one bad ConfigMap raises and stops the loop for everyone.
- ❌ No events for users: failures are only in the operator's logs.

### Option B: Go with controller-runtime (Kubebuilder)

- ✅ The ecosystem standard: informers, work queues, leader election, envtest.
- ❌ A second language for a Python course, and a lot of generated scaffolding to explain.

### Option C: Python with kopf

- ✅ Watches (no polling), per-object handlers, retries with backoff, Kubernetes Events from
  log messages, timers per object, liveness endpoint: the operator plumbing is solved.
- ✅ Handlers are plain functions around a pure `reconcile.apply()`, so the logic is unit-tested
  without a cluster.
- ❌ Fewer users than controller-runtime; no built-in leader election without its peering CRD.
- ❌ The official Kubernetes client is synchronous; kopf runs sync handlers in a thread pool.

## Decision

**Option C.** `on.create` / `on.update` / `on.resume` handle ConfigMap changes; a kopf
**timer** re-syncs each annotated ConfigMap every `SECRET_SYNC_RESYNC_SECONDS` (default 300)
to pick up rotations, and writes only when the content hash changed. One replica with
`--standalone` and a `Recreate` strategy (no peering CRD to install).

## Consequences

- **Good:** API traffic is proportional to annotated ConfigMaps, not to the cluster.
- **Good:** errors become Events on the ConfigMap the user owns (`kubectl describe cm`).
- **Bad:** a rotation reaches the cluster up to 5 minutes late; pods that read the Secret as
  env vars still need a restart (Reloader, or mount it as a volume).
- **Bad:** a single replica: while it restarts, nothing syncs (existing Secrets keep working).
