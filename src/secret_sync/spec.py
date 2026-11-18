"""What a ConfigMap asks for: parse and validate its annotations and data.

    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: app-db
      annotations:
        secret-sync.code-clinic.io/sync: "true"
        secret-sync.code-clinic.io/provider: aws          # or: azure
        secret-sync.code-clinic.io/secret-name: app-db    # optional, default = ConfigMap name
        secret-sync.code-clinic.io/vault-url: https://kv-team-a.vault.azure.net   # azure only
    data:
      DB_PASSWORD: team-a/app/db#password    # <secret id>[#<field of a JSON secret>]
      API_KEY: team-a/app/api-key

The ConfigMap holds only REFERENCES (safe to commit to Git); the values live in the cloud
secret store and only ever land in the Kubernetes Secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PREFIX = "secret-sync.code-clinic.io/"
ANN_SYNC = PREFIX + "sync"
ANN_PROVIDER = PREFIX + "provider"
ANN_SECRET_NAME = PREFIX + "secret-name"
ANN_VAULT_URL = PREFIX + "vault-url"
PROVIDERS = ("aws", "azure")
# RFC 1123 subdomain (Secret names) and the env-var-ish keys Secrets accept.
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_KEY = re.compile(r"^[-._a-zA-Z0-9]{1,253}$")
_VAULT = re.compile(r"^https://[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?/?$")


class SpecError(Exception):
    """The ConfigMap is wrong; retrying won't help until someone edits it."""


@dataclass(frozen=True)
class Ref:
    """One value to fetch: a secret in the store, optionally one field of a JSON secret."""

    secret_id: str
    field: str | None = None

    @classmethod
    def parse(cls, raw: str, key: str) -> Ref:
        text = raw.strip()
        secret_id, _, field = text.partition("#")
        if not secret_id or (text.endswith("#")):
            raise SpecError(f"data[{key!r}]: expected '<secret id>' or '<secret id>#<field>', got {raw!r}")
        return cls(secret_id, field or None)


@dataclass(frozen=True)
class SyncSpec:
    namespace: str
    configmap: str
    provider: str
    secret_name: str
    vault_url: str | None
    items: dict[str, Ref]


def wants_sync(annotations: dict[str, str] | None) -> bool:
    return (annotations or {}).get(ANN_SYNC) == "true"


def parse(namespace: str, name: str, annotations: dict[str, str] | None, data: dict[str, Any] | None) -> SyncSpec:
    ann = annotations or {}
    provider = ann.get(ANN_PROVIDER, "")
    if provider not in PROVIDERS:
        raise SpecError(f"annotation {ANN_PROVIDER} must be one of {', '.join(PROVIDERS)} (got {provider!r})")
    secret_name = ann.get(ANN_SECRET_NAME, name)
    if not _NAME.match(secret_name):
        raise SpecError(f"{secret_name!r} is not a valid Secret name")
    vault_url = ann.get(ANN_VAULT_URL)
    if provider == "azure":
        if not vault_url or not _VAULT.match(vault_url):
            raise SpecError(f"annotation {ANN_VAULT_URL} must be an https:// Key Vault URL for provider azure")
        vault_url = vault_url.rstrip("/")
    elif vault_url:
        raise SpecError(f"annotation {ANN_VAULT_URL} only applies to provider azure")
    if not data:
        raise SpecError("the ConfigMap has no data: nothing to sync")
    items: dict[str, Ref] = {}
    for key, raw in sorted(data.items()):
        if not _KEY.match(key):
            raise SpecError(f"data key {key!r} is not a valid Secret key")
        if not isinstance(raw, str):
            raise SpecError(f"data[{key!r}] must be a string")
        items[key] = Ref.parse(raw, key)
    return SyncSpec(namespace, name, provider, secret_name, vault_url, items)
