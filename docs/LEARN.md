# LEARN: writing a Kubernetes operator that handles secrets safely

A guide for juniors. Terms in *italics* link to the [course glossary](https://github.com/ngems1/code-clinic/blob/main/GLOSSARY.md).

## 1. The problem, in plain words

Applications need passwords, API keys and certificates. The company keeps them in a cloud
secret store (AWS Secrets Manager, Azure Key Vault), where they are encrypted, audited and
rotated. Pods, however, read Kubernetes **Secrets**. Copying values by hand doesn't scale and
leaks them into shell history and Git.

An operator can bridge the two: teams write a ConfigMap that holds only *references* ("the
`password` field of `team-a/app/db`"), and the operator keeps a Secret filled with the current
values. The hard part isn't copying; it's doing it **safely**: one team must not be able to read
another team's secrets through the operator, a failure must never leave a half-written Secret,
and values must never end up in logs.

## 2. Concepts from zero

### Operators and the reconcile loop

A *[Kubernetes operator](https://github.com/ngems1/code-clinic/blob/main/GLOSSARY.md#kubernetes-operator)*
is a program that watches Kubernetes objects and makes the world match what they ask for, again
and again: the *[reconcile loop](https://github.com/ngems1/code-clinic/blob/main/GLOSSARY.md#reconcile-loop)*.
Watching means the API server **pushes** changes (no polling). [kopf](https://kopf.readthedocs.io)
does the plumbing in Python: you write handlers for "created", "updated", "resumed at startup",
and "every N seconds" (a timer).

### Idempotency

*[Idempotent](https://github.com/ngems1/code-clinic/blob/main/GLOSSARY.md#idempotency)*: running
reconcile twice gives the same result as running it once. Here: a SHA-256 of the values is
stored on the Secret; same values → no write. Kubernetes may call your handler many times
(restarts, resyncs); idempotency makes that harmless.

### Ownership and garbage collection

An `ownerReference` on the Secret says "this belongs to ConfigMap X (uid ...)". When the
ConfigMap is deleted, Kubernetes deletes the Secret: no cleanup code. The same field tells the
operator which Secrets it may update: anything it doesn't own is left alone.

### Permanent vs temporary errors

- **Permanent**: retrying can't help (bad annotation, not allowed, secret not found). kopf
  records it once as an **Event** on the ConfigMap (`kubectl describe configmap`).
- **Temporary**: retry later with backoff (store unreachable, throttled).

### The confused deputy

The operator holds a powerful credential (it can read every team's secrets). A user who can
only edit ConfigMaps in `team-a` can ask the operator to fetch `team-b/billing/token`. If the
operator obeys, team-a has borrowed the operator's power: the operator is a *confused deputy*.
The fix is a policy that the operator enforces: namespace `team-a` may only read `team-a/*`.
Kubernetes *[RBAC](https://github.com/ngems1/code-clinic/blob/main/GLOSSARY.md#rbac)* can't do
this for you: it controls who edits ConfigMaps, not what they reference.

### Keyless cloud identity

The operator's pod gets cloud credentials from its ServiceAccount:
*[IRSA](https://github.com/ngems1/code-clinic/blob/main/GLOSSARY.md#irsa)* / EKS Pod Identity on
AWS, *[Workload Identity](https://github.com/ngems1/code-clinic/blob/main/GLOSSARY.md#workload-identity)*
on Azure. No access keys anywhere.

## 3. The diagram, step by step

Watch [`architecture.svg`](images/architecture.svg) (10-second loop):

1. **0-1.5 s:** ConfigMap `app-db` in `team-a` is created; the watch event reaches the operator.
2. **2.3 s:** the operator parses it and checks `policy.toml`: `team-a` may read `team-a/` ✓.
3. **2.7-4 s:** it fetches every value from AWS Secrets Manager or Azure Key Vault; values are
   never logged.
4. **4.5-6 s:** it writes Secret `app-db`, owned by the ConfigMap.
5. **7 s:** the application pod mounts the Secret.
6. **8 s:** ConfigMap `steal` in the same namespace references `team-b/billing/token`: denied,
   no Secret, and a Warning Event tells its author why.

## 4. Naive vs production

The naive version is on the `naive` branch (tag `v0.1-naive`): a polling script.

| Concern | Naive (`v0.1-naive`) | What goes wrong | Production (`v1.0.0`) |
|---|---|---|---|
| Who can read what | any ConfigMap, any secret | team-a reads team-b's secrets (confused deputy) | deny-by-default namespace policy (ADR 0002) |
| Kubernetes permissions | `cluster-admin` | a bug or a compromised dependency owns the cluster | read ConfigMaps; Secrets only in tenant namespaces |
| Logs | `DEBUG fetched db = s3cr3t` | every value in the log pipeline, forever | values never logged (e2e checks) |
| Existing Secrets | create, else **replace** | a ConfigMap named like a TLS Secret overwrites it | refuse anything not owned by this ConfigMap |
| Deletion | nothing | orphaned Secrets with live credentials | ownerReference: deleted with the ConfigMap |
| Errors | one exception stops the loop for everyone | one typo breaks every team | per-object handlers, permanent vs temporary errors |
| Feedback | `print()` in the operator's logs | users can't see why nothing happens | Events on their ConfigMap |
| Load | lists every ConfigMap every 30 s | API server load grows with the cluster | watch + per-object timer |
| Writes | every 30 s, always | needless churn, pods restarting on change | write only when the content hash changes |
| Partial failure | half the keys updated | new username + old password | all-or-nothing |
| Credentials | access keys in the Deployment | long-lived keys to leak | IRSA / Workload Identity |
| Providers | AWS only | — | AWS + Azure |

## 5. Common mistakes

- **Trusting RBAC to isolate tenants** in a controller that holds a shared credential.
- **Logging values** "just in debug": debug ends up enabled in production someday.
- **`create` then `replace` on conflict**: that's how operators overwrite other people's Secrets.
- **Writing on every reconcile**: churn, events, pods restarting; compare a hash first.
- **Returning values from kopf handlers** for resources without `.status` (ConfigMaps): kopf
  tries to store them and logs warnings every time (the e2e test found this).
- **Assuming log messages become Events**: in kopf 1.45 they don't (and kopf's own "Timer
  succeeded" lines would bury yours if they did): post explicit Events with clear reasons.
- **Forgetting the platform details**: kopf needs read access to CRDs; numeric UIDs need `USER`
  set for kopf's identity; botocore reads `AWS_DEFAULT_REGION`. All three were found by running
  the e2e test, not by reading docs.

## 6. Try it on your laptop

```bash
make setup && make test        # 60 tests, no cluster
make e2e-up                    # kind + Moto + Lowkey Vault + the operator (~10 min), cluster kept
export KUBECONFIG=.e2e/kubeconfig
kubectl -n team-a describe configmap signing        # Events
kubectl -n team-a get secret signing-keys -o yaml   # ownerReferences, content-hash, synced-at
kubectl -n secret-sync logs deploy/secret-sync      # no values in there
make e2e-down
```

Then do the [drills](DRILLS.md).

## 7. Build exercises

1. **GCP Secret Manager provider.** Add `providers/gcp.py` and `gcp_projects` to the policy.
   <details><summary>Hint</summary>Follow the Provider protocol (one method: <code>get</code>); map NotFound/PermissionDenied to permanent errors and Unavailable/ResourceExhausted to retryable.</details>
2. **Per-ConfigMap refresh interval.** An annotation `refresh: 1h` for secrets that rarely change.
   <details><summary>Hint</summary>kopf timers have one interval; keep the timer short and skip work until the annotation's interval has passed since <code>synced-at</code>.</details>
3. **Restart on rotation.** Add `pod-template-hash`-style annotation support: patch a Deployment
   named in an annotation when the Secret's hash changes.
   <details><summary>Hint</summary>That needs <code>patch deployments</code> in tenant namespaces: add it to the tenant RoleBinding, not cluster-wide. Compare with Stakater Reloader.</details>
4. **High availability.** Run 2 replicas with kopf peering instead of `--standalone`.
   <details><summary>Hint</summary>kopf peering needs its CRDs (KopfPeering / ClusterKopfPeering) installed; only one replica processes at a time.</details>
5. **Map to External Secrets Operator.** Rewrite the two example ConfigMaps as ESO
   `ExternalSecret` + a namespaced `SecretStore`, and compare with `policy.toml`.
   <details><summary>Hint</summary>The <code>SecretStore</code> per namespace plays the role of the policy entry: it decides which credential (and therefore which secrets) the namespace can use.</details>

## 8. 🎯 Interview questions

<details>
<summary>1. What makes a reconcile loop safe to run many times?</summary>

Idempotency (compare desired with actual; write only differences), level-triggered logic
(compute from the current state, not from the event's diff), all-or-nothing writes, optimistic
locking (`resourceVersion`), and ownership checks so you only change what you manage.
</details>

<details>
<summary>2. What is the confused deputy problem in Kubernetes controllers, and how do you prevent it?</summary>

A controller with broad privileges acts on behalf of less-privileged users (anyone who can
create the object it watches). Without checks, users borrow its power. Prevent it by enforcing
per-tenant authorization inside the controller (allow-lists per namespace), scoping its own
credentials, and, where possible, using per-namespace credentials (ESO's namespaced SecretStore).
</details>

<details>
<summary>3. How do you delete resources an operator created when their owner goes away?</summary>

Set `ownerReferences` (with `controller: true`) from the created object to its owner; the
Kubernetes garbage collector deletes dependents when the owner is deleted. Owners must be in the
same namespace (or cluster-scoped). Use finalizers only when cleanup is outside Kubernetes (a
cloud resource).
</details>

<details>
<summary>4. Env vars or volume mounts for Secrets, and why?</summary>

Volume mounts are updated by the kubelet when the Secret changes (rotation without restart, if
the app re-reads the file); env vars are fixed at container start. Env vars also leak more
easily (crash dumps, `/proc`, child processes). Prefer mounts for rotated secrets.
</details>

<details>
<summary>5. When would you build your own operator instead of adopting an existing one?</summary>

When no mature project fits, the logic is core to your platform, and you can staff its
maintenance (CVEs, Kubernetes upgrades, on-call). For secret sync, External Secrets Operator
fits most needs; building one is a great way to learn, rarely the right production call.
</details>

## 9. ✅ Self-check quiz

1. What stops `team-a` from syncing `team-b/billing/token`?
2. A ConfigMap is deleted. What deletes its Secret?
3. The store is down during a resync. What happens to the existing Secret?
4. Why does the operator refuse to update a Secret named `tls-cert` that it didn't create?
5. Where does a user see why their Secret wasn't created?

<details>
<summary>Answers</summary>

1. The policy: `team-a` may only read secret ids starting with `team-a/` (deny by default).
2. The Kubernetes garbage collector, through the Secret's `ownerReference` to the ConfigMap.
3. Nothing: values are fetched first and the Secret is only written if all succeed; the error is
   retried with backoff.
4. Takeover protection: only Secrets labelled by the operator and controlled by this ConfigMap's
   UID may be written.
5. In the Events of their ConfigMap (`kubectl describe configmap <name>`).
</details>

## 10. Further reading

- [kopf documentation](https://kopf.readthedocs.io/) (handlers, timers, errors, events, RBAC).
- Kubernetes: [Operator pattern](https://kubernetes.io/docs/concepts/extend-kubernetes/operator/),
  [Garbage collection / ownerReferences](https://kubernetes.io/docs/concepts/architecture/garbage-collection/),
  [Good practices for Secrets](https://kubernetes.io/docs/concepts/security/secrets-good-practices/).
- AWS: [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html),
  [IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html).
- Azure: [Key Vault secrets](https://learn.microsoft.com/azure/key-vault/secrets/),
  [Workload Identity on AKS](https://learn.microsoft.com/azure/aks/workload-identity-overview).
- *[External Secrets Operator](https://github.com/ngems1/code-clinic/blob/main/GLOSSARY.md#external-secrets-operator)*:
  [docs](https://external-secrets.io/) (the production choice, ADR 0004).
- Course: #11 (a Helm chart using ESO), #16 (event-driven Lambda), #48 (just-in-time access).
