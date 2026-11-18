# 0004. Build this for learning; adopt External Secrets Operator in production

## Status

Accepted · 2026-11-17

## Context

The challenge asks us to develop an operator that creates Secrets from AWS Secrets Manager and
Azure Key Vault. A mature open-source project already does this: the **External Secrets
Operator** (ESO, a CNCF project), with dozens of providers, templating, push secrets,
`ClusterSecretStore` scoping and a large user base. Project #11 already uses it.

## Options considered

### Option A: adopt ESO everywhere, build nothing

- ✅ Maintained by many people; security fixes arrive without your effort.
- ❌ Nothing learned about how operators work, which is the point of this course project.

### Option B: build and run this operator in production

- ✅ Exactly the features you need, nothing more; small and auditable (about 600 lines,
  comments included).
- ❌ You own every bug, CVE and Kubernetes upgrade; one maintainer is a bus factor of one.

### Option C: build it to learn the patterns, recommend ESO for production

- ✅ Juniors learn watches, reconciliation, idempotency, ownership, events and the
  confused-deputy risk by building them; then they can read ESO's design and its
  `SecretStore` scoping with understanding.

## Decision

**Option C.** This repository is production-grade in its engineering (tests, policy, RBAC,
threat model) but the README recommends ESO for real clusters, and LEARN.md maps each concept
here to its ESO equivalent (ConfigMap annotations ↔ `ExternalSecret`, policy.toml ↔
namespaced `SecretStore`, resync timer ↔ `refreshInterval`).

## Consequences

- **Good:** honest guidance: the right answer at work is usually "adopt, don't build".
- **Bad:** readers who skip the README may deploy this anyway; it is safe to run, but they
  take on its maintenance.
