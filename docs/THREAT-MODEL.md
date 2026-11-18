# Threat model

**Scope:** the operator (code, image, Deployment), its Kubernetes permissions, its cloud
identity, the policy file, and the Secrets it writes. Trust boundaries: *namespace users →
ConfigMaps*, *the operator → cloud secret stores*, *the operator → Secrets in tenant namespaces*.

**Out of scope:** the security of AWS Secrets Manager / Azure Key Vault themselves, etcd
encryption at rest, and what applications do with the Secrets once mounted.

## Assets

- **Cloud secrets** of every tenant (the operator's identity can read them).
- **Kubernetes Secrets** in tenant namespaces, and in all other namespaces.
- **The policy file**: the only boundary between tenants inside the operator.
- **The operator's identity** (IRSA role / Workload Identity) and its ServiceAccount.

## Threats

| # | Asset | Threat | Likelihood / Impact | Mitigation in this repo | Residual risk / NOT protected against |
|---|---|---|---|---|---|
| T1 | cloud secrets | **Confused deputy**: a user who can create ConfigMaps in their namespace references another team's secret | H / H | deny-by-default `policy.toml`: namespace → allowed prefixes/vaults, prefix matched from the start (ADR 0002); e2e step 6 | the operator's cloud identity can still read everything: scope it in IAM too (prefix conditions, per-vault RBAC) |
| T2 | Kubernetes Secrets | **Overwriting an existing Secret** (a TLS cert, another app's credentials) by naming it in `secret-name` | M / H | write only Secrets labelled by us AND controlled by this ConfigMap's UID; otherwise refuse (ADR 0003); e2e step 7 | a tenant can still choose any free name in its own namespace |
| T3 | Kubernetes Secrets | **Operator compromise** reads or writes Secrets cluster-wide (the naive version has cluster-admin) | M / H | Secret verbs only via per-tenant RoleBindings; cluster-wide only get/list/watch/patch ConfigMaps + create Events; e2e checks `can-i` in kube-system and team-c | ConfigMaps everywhere are readable by the operator (they shouldn't hold secrets) |
| T4 | cloud secrets | **Values leak through logs, Events or errors** | M / H | values never logged; errors carry ids and codes only; e2e greps the operator logs for all 5 test values; unit tests | Kubernetes Secrets are base64, not encrypted: enable etcd encryption and restrict `get secrets` |
| T5 | operator identity | **Long-lived cloud keys** in the Deployment (naive) | M / H | IRSA / Pod Identity on EKS, Workload Identity on AKS (ServiceAccount annotations); no keys in manifests | the e2e uses dummy keys for the emulator only |
| T6 | availability | **Store outage or throttling** breaks apps | M / M | all-or-nothing writes: on failure the existing Secret is kept; temporary errors retried with backoff; resync | a Secret that was never synced stays missing until the store is back |
| T7 | integrity | **Half-updated Secret** (new user, old password) | L / H | fetch everything, then one write; `resourceVersion` optimistic locking | — |
| T8 | policy file | **Policy tampering** widens a tenant's access | L / H | the policy lives in the operator namespace (platform team only); invalid policy = operator won't start (fail closed); config hash rolls the pod | an admin with edit rights in `secret-sync` can change it: protect with GitOps + review |
| T9 | operator image | **Supply chain**: a malicious dependency in a pod that can read every tenant's secrets | L / H | pinned lockfile (`uv.lock`), Trivy scan of the image in CI, non-root read-only container, NetworkPolicy egress to 53/443/6443 only | 443 to anywhere: exfiltration over HTTPS is still possible; an egress proxy/allow-list of store endpoints would close it |
| T10 | Azure emulator switch | `SECRET_SYNC_AZURE_EMULATOR=1` disables TLS verification | L / H | only set in the e2e overlay; logs a loud warning; tests | someone copying the e2e overlay to production |

## Assumptions

- etcd encryption at rest is enabled, and `get secrets` is restricted in tenant namespaces.
- The `secret-sync` namespace (policy, Deployment) is administered by the platform team only.
- The operator's cloud identity is scoped as narrowly as the policy (ADR 0002).

## How to verify

- `make test`: `TestPolicy` (T1), `test_refuses_to_overwrite_a_foreign_secret`,
  `test_refuses_a_secret_owned_by_another_configmap` (T2), `test_sync_success_logs_no_values`,
  `test_not_found_is_permanent_and_has_no_value`, `test_field_errors` (T4), `test_all_or_nothing` (T6, T7).
- `make e2e`: steps 6 (T1), 7 (T2), 8 (T3, T4).
- [Drills](DRILLS.md) and the review exercise (branch `review-exercise`).
