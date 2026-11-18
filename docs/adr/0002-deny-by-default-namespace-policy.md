# 0002. A deny-by-default policy maps each namespace to the secrets it may read

## Status

Accepted · 2026-11-17

## Context

The operator runs with ONE cloud identity that can read every tenant's secrets. Whoever can
create a ConfigMap in a namespace can ask it to copy a secret into a Secret, and can then read
that Secret. Without a check, "may edit ConfigMaps in team-a" silently becomes "may read every
secret the operator can read": the payment team's database password, the CI deploy keys.
This is the classic **confused deputy** problem, and the core risk of this kind of operator.

## Options considered

### Option A: trust Kubernetes RBAC on ConfigMaps

- ✅ Nothing to build.
- ❌ RBAC controls who edits ConfigMaps, not WHICH cloud secrets they reference. Escalation open.

### Option B: one operator (and one cloud identity) per namespace

- ✅ Strong isolation: each identity can only read its own secrets.
- ❌ N operators to run and upgrade; N identities to manage.

### Option C: one operator + a policy file: namespace → allowed AWS prefixes / Azure vaults

- ✅ One deployment; the isolation lives in a reviewed file owned by the platform team.
- ✅ Deny by default: a namespace that isn't listed can't sync anything.
- ✅ Defence in depth with RBAC: Secret permissions are granted per tenant namespace by
  RoleBinding, so even a policy mistake can't write Secrets in `kube-system`.
- ❌ The policy and the cloud IAM must stay consistent; the operator identity still CAN read
  everything, so the policy check is the boundary.

## Decision

**Option C**, plus a recommendation: in the cloud, scope the operator's identity to the tenant
prefixes too (IAM condition on secret name prefix / Key Vault RBAC per vault), so that a bug in
the policy check is caught by IAM. Prefixes are matched from the start of the id (`team-a/`
does not match `prod/team-a/...`).

## Consequences

- **Good:** the e2e test proves a tenant can't reach another tenant's prefix, and an unlisted
  namespace gets nothing (both as Events on the ConfigMap).
- **Bad:** onboarding a team means a policy entry AND a RoleBinding (one PR, one review).
- **Bad:** a broken policy file stops the operator at startup (fail closed): existing Secrets
  keep working, but nothing syncs until it's fixed.
