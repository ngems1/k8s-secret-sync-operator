# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions: [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-11-18

### Added

- kopf operator: annotated ConfigMaps (`secret-sync.code-clinic.io/*`) become Secrets with values
  from AWS Secrets Manager or Azure Key Vault (`<id>[#<json field>]` references); resync timer for
  rotations; Events on the ConfigMap; Prometheus metrics; liveness endpoint.
- Deny-by-default `policy.toml`: namespace → allowed AWS prefixes / Azure vaults.
- Reconcile rules: Secret owned by its ConfigMap (garbage-collected with it), all-or-nothing
  writes, content hash (no write when unchanged, manual edits restored), `resourceVersion`
  optimistic locking, refusal to overwrite Secrets it doesn't own.
- Deployment: restricted pod, non-root read-only image, NetworkPolicy, RBAC with Secret
  permissions only through per-tenant RoleBindings.
- Tests: 60 unit tests (Moto for AWS, a fake Key Vault), and an end-to-end test on kind with
  in-cluster Moto and Lowkey Vault.

### Fixed (found by the end-to-end test and the drills)

- kopf derives its identity from `getpass.getuser()`: crash with a numeric UID and no
  `/etc/passwd` entry → `USER` set in the image.
- A Service named `moto` injects `MOTO_PORT=tcp://…` into pods; Moto read it as its port →
  `enableServiceLinks: false` for the fakes.
- botocore reads `AWS_DEFAULT_REGION`, not `AWS_REGION` → the Deployment sets it.
- kopf needs `list`/`watch` on CRDs; without it, API discovery failed and no Events were posted.
- kopf 1.45 no longer posts log messages as Events (and when re-enabled, its own "Timer
  succeeded" lines buried the useful ones) → explicit Events: Synced, Rejected, StoreError,
  StoreUnavailable.
- Handlers returned values that kopf tried to store in the ConfigMap's (non-existent) `.status`.
- The Azure SDK logged every HTTP exchange at INFO → silenced to WARNING.
- Rejected ConfigMaps logged an ERROR on every resync → quiet (already reported once).
- The AWS prefix `"team"` matched `team-b/…` → prefixes must end with `/`.
- A secret deleted from the store was only reported at DEBUG on resync → a Warning Event.
- During a store outage the SDKs' default retries (Azure: 10 with backoff; botocore: 60 s
  connect timeout) blocked a handler for over a minute with no Event → short timeouts, at most
  3 attempts, then the operator's own visible retry (StoreUnavailable Event + backoff).
