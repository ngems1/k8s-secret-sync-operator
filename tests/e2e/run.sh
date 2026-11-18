#!/usr/bin/env bash
# End-to-end test on a kind cluster: the real operator image, in-cluster fakes of AWS Secrets
# Manager (Moto) and Azure Key Vault (Lowkey Vault). What is checked:
#   1. AWS: a ConfigMap becomes a Secret with the right values (incl. a JSON field), owned by it
#   2. Azure: same, from Key Vault
#   3. rotation: a new value in the store reaches the Secret on the next resync
#   4. policy: another team's prefix → refused (Event), no Secret; unlisted namespace → refused
#   5. takeover: an existing Secret that isn't ours is left untouched
#   6. deleting the ConfigMap deletes the Secret (ownerReference, garbage collection)
#   7. no secret value in the operator's logs; no access to Secrets outside tenant namespaces
#
# Usage: tests/e2e/run.sh   (docker, kind, kubectl, kustomize)   KEEP=1 keeps the cluster.
# `make e2e` runs it inside the course image with the Docker socket mounted.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
CLUSTER=secret-sync-e2e
# Same pair as project #9: image and digest come from the kind release notes.
KIND_NODE_IMAGE="kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
state="$repo/.e2e"
mkdir -p "$state"
export KUBECONFIG="$state/kubeconfig"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
pass() { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; kubectl -n secret-sync logs deploy/secret-sync --tail=40 || true; exit 1; }
wait_for() { # <seconds> <description> <command...>
  local timeout="$1" what="$2"; shift 2
  local start=$SECONDS
  until "$@" > /dev/null 2>&1; do
    (( SECONDS - start > timeout )) && fail "timed out after ${timeout}s: $what"
    sleep 2
  done
  pass "$what ($((SECONDS - start))s)"
}
secret_value() { kubectl -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d; }
has_secret() { kubectl -n "$1" get secret "$2"; }
event_says() { kubectl -n "$1" get events --field-selector "involvedObject.name=$2" -o jsonpath='{.items[*].message}' | grep -q "$3"; }

cleanup() { [ "${KEEP:-0}" = 1 ] || kind delete cluster --name "$CLUSTER" > /dev/null 2>&1 || true; }
trap cleanup EXIT

log "1/8 kind cluster + operator image"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --image "$KIND_NODE_IMAGE" --wait 120s
# Inside a container that shares the host daemon (make e2e): reach the API over the kind network.
if [ -f /.dockerenv ] && docker inspect "$(hostname)" > /dev/null 2>&1; then
  docker network connect kind "$(hostname)" 2> /dev/null || true
  kind get kubeconfig --internal --name "$CLUSTER" > "$KUBECONFIG"
else
  kind get kubeconfig --name "$CLUSTER" > "$KUBECONFIG"
fi
docker build -q -t secret-sync:e2e "$repo" > /dev/null
kind load docker-image secret-sync:e2e --name "$CLUSTER" > /dev/null
pass "cluster up, image secret-sync:e2e loaded"

log "2/8 fakes, tenants and the operator (deploy/ + tests/e2e/overlay)"
kustomize build "$here/overlay" | kubectl apply -f - > /dev/null
for d in fakes/moto fakes/lowkey-vault secret-sync/secret-sync; do
  kubectl -n "${d%/*}" rollout status "deploy/${d#*/}" --timeout=240s > /dev/null || {
    kubectl -n "${d%/*}" logs "deploy/${d#*/}" --tail=20 || true
    fail "deployment $d did not become ready"
  }
done
pass "moto, lowkey-vault and secret-sync running"

# Seed the fake stores from inside the cluster (the operator image has boto3 and the Azure SDK).
seed() {
  kubectl -n fakes run "seed-$RANDOM" --rm -i --restart=Never --quiet --image=secret-sync:e2e \
    --image-pull-policy=Never --env=AWS_ENDPOINT_URL=http://moto.fakes.svc.cluster.local:5000 \
    --env=AWS_ACCESS_KEY_ID=test --env=AWS_SECRET_ACCESS_KEY=test --env=AWS_DEFAULT_REGION=eu-west-1 \
    --command -- python -c "$1"
}
seed '
import boto3, warnings; warnings.filterwarnings("ignore")
sm = boto3.client("secretsmanager")
sm.create_secret(Name="team-a/app/db", SecretString="{\"username\": \"app\", \"password\": \"pw-v1-0d5e\"}")
sm.create_secret(Name="team-a/app/api-key", SecretString="key-a-91c2")
sm.create_secret(Name="team-b/billing/token", SecretString="billing-token-77f1")
from secret_sync.providers.azure import _EmulatorCredential
from azure.keyvault.secrets import SecretClient
SecretClient("https://lowkey-vault.fakes.svc.cluster.local:8443", _EmulatorCredential(),
             verify_challenge_resource=False, connection_verify=False).set_secret("signing-key", "kv-sign-3a8b")
print("seeded")'

cm() { # <namespace> <name> <provider> <extra annotations yaml> <data yaml>
  kubectl apply -f - > /dev/null <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: $2
  namespace: $1
  annotations:
    secret-sync.code-clinic.io/sync: "true"
    secret-sync.code-clinic.io/provider: $3
$4
data:
$5
EOF
}

log "3/8 AWS Secrets Manager → Secret"
cm team-a app-db aws "" "  DB_USER: team-a/app/db#username
  DB_PASSWORD: team-a/app/db#password
  API_KEY: team-a/app/api-key"
wait_for 60 "Secret team-a/app-db created" has_secret team-a app-db
[ "$(secret_value team-a app-db DB_PASSWORD)" = pw-v1-0d5e ] || fail "DB_PASSWORD"
[ "$(secret_value team-a app-db DB_USER)" = app ] || fail "DB_USER (JSON field)"
[ "$(secret_value team-a app-db API_KEY)" = key-a-91c2 ] || fail "API_KEY"
owner="$(kubectl -n team-a get secret app-db -o jsonpath='{.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}')"
[ "$owner" = ConfigMap/app-db ] || fail "ownerReference is '$owner'"
pass "3 keys correct (one from a JSON field), owned by ConfigMap/app-db"

log "4/8 Azure Key Vault → Secret"
cm team-a signing azure "    secret-sync.code-clinic.io/vault-url: https://lowkey-vault.fakes.svc.cluster.local:8443
    secret-sync.code-clinic.io/secret-name: signing-keys" "  SIGNING_KEY: signing-key"
wait_for 60 "Secret team-a/signing-keys created" has_secret team-a signing-keys
[ "$(secret_value team-a signing-keys SIGNING_KEY)" = kv-sign-3a8b ] || fail "SIGNING_KEY"
pass "value from Key Vault, custom secret-name"

log "5/8 rotation in the store reaches the Secret"
seed 'import boto3; boto3.client("secretsmanager").put_secret_value(SecretId="team-a/app/db", SecretString="{\"username\": \"app\", \"password\": \"pw-v2-8b1f\"}"); print("rotated")'
rotated() { [ "$(secret_value team-a app-db DB_PASSWORD)" = pw-v2-8b1f ]; }
wait_for 90 "DB_PASSWORD rotated to v2 by the resync timer" rotated

log "6/8 policy: tenants can't reach each other's secrets"
cm team-a steal aws "" "  TOKEN: team-b/billing/token"
wait_for 60 "Event on team-a/steal says the prefix is not allowed" event_says team-a steal "outside this namespace's prefixes"
has_secret team-a steal && fail "a Secret was created from another team's prefix"
cm team-c rogue aws "" "  TOKEN: team-a/app/api-key"
wait_for 60 "Event on team-c/rogue says the namespace is not allowed" event_says team-c rogue "is not allowed to sync"
has_secret team-c rogue && fail "unlisted namespace got a Secret"
pass "no Secret for team-a/steal or team-c/rogue"

log "7/8 takeover protection, then garbage collection"
kubectl -n team-b create secret generic tls-cert --from-literal=tls.key=do-not-touch > /dev/null
cm team-b billing aws "    secret-sync.code-clinic.io/secret-name: tls-cert" "  TOKEN: team-b/billing/token"
wait_for 60 "Event on team-b/billing refuses to overwrite tls-cert" event_says team-b billing "refusing to overwrite"
[ "$(secret_value team-b tls-cert tls\\.key)" = do-not-touch ] || fail "the existing Secret was modified"
pass "existing Secret tls-cert untouched"
kubectl -n team-a delete configmap app-db > /dev/null
gone() { ! kubectl -n team-a get secret app-db > /dev/null 2>&1; }
wait_for 60 "deleting ConfigMap app-db deleted Secret app-db (ownerReference)" gone

log "8/8 no values in logs; no Secret access outside tenant namespaces"
logs="$(kubectl -n secret-sync logs deploy/secret-sync)"
for v in pw-v1-0d5e pw-v2-8b1f key-a-91c2 kv-sign-3a8b billing-token-77f1; do
  grep -q -- "$v" <<< "$logs" && fail "secret value $v appears in the operator logs"
done
pass "none of the 5 secret values appear in $(wc -l <<< "$logs") log lines"
sa=system:serviceaccount:secret-sync:secret-sync
[ "$(kubectl auth can-i get secrets -n kube-system --as=$sa)" = no ] || fail "operator can read kube-system Secrets"
[ "$(kubectl auth can-i get secrets -n team-c --as=$sa)" = no ] || fail "operator can read team-c Secrets"
[ "$(kubectl auth can-i get secrets -n team-a --as=$sa)" = yes ] || fail "operator can't read team-a Secrets"
pass "Secrets: team-a yes; team-c and kube-system no"

log "e2e PASSED"
