# Failure drills

Break things on purpose on a local cluster: kind, an in-cluster Moto (AWS Secrets Manager) and
Lowkey Vault (Azure Key Vault). Nothing touches a real cloud account.

**Setup:**

```bash
make e2e-up                        # runs the e2e test and KEEPS the cluster (~10 min)
export KUBECONFIG=.e2e/kubeconfig
kubectl -n team-a get configmaps,secrets
val() { kubectl -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d; echo; }
events() { kubectl -n "$1" get events --field-selector "involvedObject.name=$2" \
  --sort-by=.lastTimestamp -o custom-columns=TYPE:.type,REASON:.reason,MSG:.message --no-headers; }
```

The e2e overlay runs the operator with a short resync (`SECRET_SYNC_RESYNC_SECONDS=10`) so you
don't wait five minutes. `make e2e-down` deletes everything.

## Drill 1: A prefix without its slash

**Scenario.** A platform engineer onboards a team and writes the policy entry quickly:
`aws_prefixes = ["team"]` instead of `["team-a/"]`. It looks harmless ("team-a's secrets all
start with team").

```bash
pol=$(kubectl -n secret-sync get deploy secret-sync -o jsonpath='{.spec.template.spec.volumes[0].configMap.name}')
kubectl -n secret-sync get configmap "$pol" -o jsonpath='{.data.policy\.toml}' > /tmp/policy.toml
sed -i 's|aws_prefixes = \["team-a/"\]|aws_prefixes = ["team"]|' /tmp/policy.toml
kubectl -n secret-sync create configmap "$pol" --from-file=policy.toml=/tmp/policy.toml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n secret-sync rollout restart deploy/secret-sync
sleep 25
kubectl -n secret-sync get pods
kubectl -n secret-sync logs deploy/secret-sync | grep -m1 "must end with"
kubectl -n team-a get secret steal signing-keys
```

Restore `team-a/` the same way afterwards.

**What to observe.** The new pod runs but never syncs anything: startup fails with
`Activity 'startup' failed with an exception and will try again in 60 seconds: policy:
[namespaces.team-a] aws_prefixes must end with '/': team`. The old
pod is gone (the Deployment uses `Recreate`), but **existing Secrets stay** (`signing-keys` is
still there) and **no new Secret appears** (`steal` is still `NotFound`).

**Questions**

1. Without the "must end with /" rule, what could team-a read with the prefix `team`?
2. Why is "the operator refuses to start" better here than "the operator ignores the bad entry"?
3. What do the applications notice while the operator is down?

<details>
<summary>Answers</summary>

1. Every secret whose id starts with `team`: `team-b/billing/token` included. A string prefix
   is not a path prefix; that's exactly what the first run of this drill showed (before the
   fix, the `steal` ConfigMap got team-b's token). `str.startswith` checks characters, not
   path segments.
2. A policy is security configuration: a half-applied policy is a policy nobody reviewed.
   Fail closed and loud (pod not ready, alert) instead of running with rules that differ from
   the file in Git. A policy check in CI (`make test` loads `deploy/policy.toml`) catches it
   before it ships.
3. Nothing, at first: Secrets already exist and pods keep mounting them. Only rotations and
   new ConfigMaps wait. That's why the operator writes Secrets and never sits on the request
   path: its outage is a delay, not an incident.
</details>

## Drill 2: The store goes down

**Scenario.** The cloud store has an outage (or a NetworkPolicy change cuts it off) during a
rotation. Then it comes back, but empty: Lowkey Vault keeps secrets in memory, like a vault
restored from the wrong backup.

```bash
val team-a signing-keys SIGNING_KEY
kubectl -n fakes scale deploy/lowkey-vault --replicas=0
kubectl -n team-a annotate configmap signing touched="$(date +%s)" --overwrite   # force a sync
sleep 35
val team-a signing-keys SIGNING_KEY
events team-a signing
kubectl -n fakes scale deploy/lowkey-vault --replicas=1 && kubectl -n fakes rollout status deploy/lowkey-vault
sleep 25
events team-a signing
```

**What to observe.** While the store is down, the Secret keeps its value
(`kv-sign-3a8b` before and after) and the operator retries every 30 s, each time with an Event:

```text
Warning   StoreUnavailable   azure: 'signing-key': ServiceRequestTimeoutError; will retry, Secret left unchanged
```

Once the store is back without the secret, every resync posts:

```text
Warning   StoreError         azure: 'signing-key' not found in https://lowkey-vault.fakes.svc.cluster.local:8443; Secret left unchanged
```

and the Secret still holds the last good value.

**Questions**

1. Why keep the old value instead of deleting the Secret when the store says "not found"?
2. Why is a "not found" posted at every resync, while a rejected ConfigMap is reported once?
3. How would you alert on this without reading Events?
4. The first run of this drill showed **no** Event for over a minute of outage. Why, and what
   is the fix?

<details>
<summary>Answers</summary>

1. Deleting would turn a store problem into an application outage at the next pod restart.
   The last good value is usually still valid; the owner gets a Warning and decides. (A real
   revocation is done by disabling the credential at its source, not by emptying the Secret.)
2. A rejection only changes when someone edits the ConfigMap or the policy: one Event is
   enough. A store error is the world drifting under a working ConfigMap; it must stay
   visible until fixed. Before this drill, it was only logged at DEBUG: silent.
3. `secret_sync_total{result="store_error"}` increases and
   `secret_sync_last_success_timestamp_seconds` stops moving: alert on
   `time() - secret_sync_last_success_timestamp_seconds > 3 * resync interval`.
4. The Azure SDK's default retry policy (10 retries with exponential backoff) kept retrying
   *inside* one handler call, so the operator never got the error to report, and the update
   handler for the ConfigMap waited behind it (botocore's 60 s connect timeout does the same).
   Fix: short SDK timeouts and at most 3 attempts (`CLIENT_OPTIONS`, `CLIENT_CONFIG` in
   `providers/`), so the operator's own retry, which is visible, takes over. Retries belong at
   one layer, where someone can see them.
</details>

## Drill 3: Someone edits the Secret by hand

**Scenario.** During an incident, someone "fixes" an API key with `kubectl patch secret` instead
of updating the store.

```bash
kubectl -n team-a apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: api
  namespace: team-a
  annotations:
    secret-sync.code-clinic.io/sync: "true"
    secret-sync.code-clinic.io/provider: aws
data:
  API_KEY: team-a/app/api-key
EOF
sleep 8 && val team-a api API_KEY
kubectl -n team-a patch secret api --type merge -p '{"data":{"API_KEY":"'"$(printf hacked | base64)"'"}}'
val team-a api API_KEY
sleep 15 && val team-a api API_KEY
events team-a api
```

**What to observe.** `key-a-91c2` → `hacked` → `key-a-91c2` again after the next resync, with
two Events: `Normal Synced Secret api created (1 key(s) from aws)` then
`Normal Synced Secret api updated (1 key(s) from aws)`.

**Questions**

1. The content-hash annotation on the Secret didn't change during the edit. How did the
   operator notice?
2. Is restoring the value the right call during an incident?
3. How would you stop people from editing these Secrets in the first place?

<details>
<summary>Answers</summary>

1. It compares the hash of the values it fetched with a hash of the Secret's **current data**,
   not just with the annotation. The annotation records what it wrote; the data says what is
   there now. Trusting only the annotation would leave the manual edit in place forever.
2. Yes: the store is the source of truth, and a silent difference between store and cluster is
   how a fix gets lost at the next rotation. The incident fix belongs in the store; the
   `updated` Event leaves a trace of the correction.
3. RBAC: tenants get `get` on Secrets, not `patch`. Optionally a ValidatingAdmissionPolicy
   that refuses changes to Secrets labelled `managed-by: secret-sync` unless the request comes
   from the operator's ServiceAccount.
</details>
