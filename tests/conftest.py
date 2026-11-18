"""Fakes shared by the tests: an in-memory Secret API and a dictionary-backed provider."""

from __future__ import annotations

import copy
from typing import Any

from secret_sync.providers import ProviderError
from secret_sync.spec import ANN_PROVIDER, ANN_SYNC


class FakeSecrets:
    """In-memory stand-in for KubeSecrets that records every write."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], dict[str, Any]] = {}
        self.writes: list[str] = []
        self.version = 0

    def read(self, namespace: str, name: str) -> dict[str, Any] | None:
        found = self.store.get((namespace, name))
        return copy.deepcopy(found) if found else None

    def create(self, namespace: str, body: dict[str, Any]) -> None:
        self._save(namespace, body)
        self.writes.append("create")

    def replace(self, namespace: str, name: str, body: dict[str, Any]) -> None:
        assert body["metadata"].get("resourceVersion") == self.store[(namespace, name)]["metadata"]["resourceVersion"]
        self._save(namespace, body)
        self.writes.append("replace")

    def _save(self, namespace: str, body: dict[str, Any]) -> None:
        self.version += 1
        saved = copy.deepcopy(body)
        saved["metadata"]["resourceVersion"] = str(self.version)
        self.store[(namespace, saved["metadata"]["name"])] = saved


class FakeProvider:
    def __init__(self, values: dict[str, str], fail: dict[str, ProviderError] | None = None) -> None:
        self.values = values
        self.fail = fail or {}
        self.calls: list[str] = []

    def get(self, secret_id: str) -> str:
        self.calls.append(secret_id)
        if secret_id in self.fail:
            raise self.fail[secret_id]
        if secret_id not in self.values:
            raise ProviderError(f"fake: {secret_id!r} not found", retryable=False)
        return self.values[secret_id]


def ann(provider: str = "aws", **extra: str) -> dict[str, str]:
    return {
        ANN_SYNC: "true",
        ANN_PROVIDER: provider,
        **{f"secret-sync.code-clinic.io/{k}": v for k, v in extra.items()},
    }
