"""The Kubernetes side: read / create / replace Secrets with the official client."""

from __future__ import annotations

from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException


class KubeSecrets:
    def __init__(self, api: client.CoreV1Api | None = None) -> None:
        self._api = api or client.CoreV1Api()

    def read(self, namespace: str, name: str) -> dict[str, Any] | None:
        try:
            secret = self._api.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        result: dict[str, Any] = client.ApiClient().sanitize_for_serialization(secret)
        return result

    def create(self, namespace: str, body: dict[str, Any]) -> None:
        self._api.create_namespaced_secret(namespace, body)

    def replace(self, namespace: str, name: str, body: dict[str, Any]) -> None:
        self._api.replace_namespaced_secret(name, namespace, body)
