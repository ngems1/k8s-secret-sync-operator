"""One reconciliation: ConfigMap → fetch every value → create or update the owned Secret.

Rules that make this safe to run again and again:

* **Idempotent:** the Secret carries a SHA-256 of its data; same values → no write at all.
* **All or nothing:** if one value can't be fetched, the Secret is left as it was (a
  half-updated Secret could pair a new username with an old password).
* **Owned:** the Secret has an ownerReference to the ConfigMap, so deleting the ConfigMap
  deletes the Secret (Kubernetes garbage collection, no code needed).
* **No takeover:** an existing Secret that this operator doesn't own is never overwritten.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from secret_sync.providers import Provider, resolve
from secret_sync.spec import PREFIX, SyncSpec

MANAGED_BY = "secret-sync"
LABEL_MANAGED = "app.kubernetes.io/managed-by"
ANN_SOURCE = PREFIX + "source"
ANN_HASH = PREFIX + "content-hash"
ANN_SYNCED_AT = PREFIX + "synced-at"


class TakeoverError(Exception):
    """A Secret with the target name exists and isn't ours: refuse to overwrite it."""


class SecretApi(Protocol):
    """The three Kubernetes calls we need (kube.py implements them; tests fake them)."""

    def read(self, namespace: str, name: str) -> dict[str, Any] | None: ...
    def create(self, namespace: str, body: dict[str, Any]) -> None: ...
    def replace(self, namespace: str, name: str, body: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Owner:
    """The ConfigMap's identity, for the ownerReference."""

    name: str
    uid: str


def content_hash(values: dict[str, str]) -> str:
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def fetch(spec: SyncSpec, provider: Provider) -> dict[str, str]:
    """Every value or an exception: never a partial result."""
    return {key: resolve(provider, ref) for key, ref in spec.items.items()}


def desired(spec: SyncSpec, owner: Owner, values: dict[str, str], now: Callable[[], datetime]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": spec.secret_name,
            "namespace": spec.namespace,
            "labels": {LABEL_MANAGED: MANAGED_BY},
            "annotations": {
                ANN_SOURCE: f"configmap/{spec.configmap} ({spec.provider})",
                ANN_HASH: content_hash(values),
                ANN_SYNCED_AT: now().astimezone(UTC).isoformat(timespec="seconds"),
            },
            "ownerReferences": [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "name": owner.name,
                    "uid": owner.uid,
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in values.items()},
    }


def owned_by(existing: dict[str, Any], owner: Owner) -> bool:
    meta = existing.get("metadata") or {}
    if (meta.get("labels") or {}).get(LABEL_MANAGED) != MANAGED_BY:
        return False
    return any(ref.get("uid") == owner.uid and ref.get("controller") for ref in meta.get("ownerReferences") or [])


def apply(
    spec: SyncSpec,
    owner: Owner,
    provider: Provider,
    api: SecretApi,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    """Returns "created", "updated" or "unchanged". Raises on any problem, changing nothing."""
    values = fetch(spec, provider)
    body = desired(spec, owner, values, now)
    existing = api.read(spec.namespace, spec.secret_name)
    if existing is None:
        api.create(spec.namespace, body)
        return "created"
    if not owned_by(existing, owner):
        raise TakeoverError(
            f"Secret {spec.namespace}/{spec.secret_name} exists and is not managed by this ConfigMap: "
            "refusing to overwrite it (choose another secret-name, or delete it on purpose)"
        )
    current_hash = ((existing.get("metadata") or {}).get("annotations") or {}).get(ANN_HASH)
    if current_hash == body["metadata"]["annotations"][ANN_HASH] and existing.get("data") == body["data"]:
        return "unchanged"
    body["metadata"]["resourceVersion"] = existing["metadata"].get("resourceVersion")  # optimistic locking
    api.replace(spec.namespace, spec.secret_name, body)
    return "updated"
