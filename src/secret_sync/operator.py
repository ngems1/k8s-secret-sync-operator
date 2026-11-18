"""The kopf handlers. Run with:

    kopf run --standalone --all-namespaces --liveness=http://0.0.0.0:8080/healthz -m secret_sync.operator

Events, not polling: kopf watches ConfigMaps and calls `sync` when an annotated one is created,
changed, or found at startup. A timer re-syncs every SECRET_SYNC_RESYNC_SECONDS (default 300)
so rotations in the cloud store reach the cluster.

Error handling decides what users see (as Kubernetes Events on the ConfigMap):
    bad ConfigMap / not allowed / name taken  → PermanentError: one Warning, no retry storm
    store unreachable / throttled             → TemporaryError: retried with backoff
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, ClassVar

import kopf
from kubernetes import config as kube_config
from prometheus_client import Counter, Gauge, start_http_server

from secret_sync import policy as policy_mod
from secret_sync import reconcile, spec
from secret_sync.kube import KubeSecrets
from secret_sync.providers import Provider, ProviderError
from secret_sync.providers.aws import AwsSecretsManager
from secret_sync.providers.azure import AzureKeyVault

SELECT = {spec.ANN_SYNC: "true"}
RESYNC_SECONDS = float(os.environ.get("SECRET_SYNC_RESYNC_SECONDS", "300"))
POLICY_PATH = Path(os.environ.get("SECRET_SYNC_POLICY", "/etc/secret-sync/policy.toml"))

SYNCS = Counter("secret_sync_total", "Sync attempts by result", ["result"])
LAST_SUCCESS = Gauge("secret_sync_last_success_timestamp_seconds", "Last successful sync", ["namespace", "configmap"])


class Runtime:
    """Long-lived clients, created once at startup (and replaced in tests)."""

    policy: ClassVar[policy_mod.Policy] = policy_mod.Policy()
    secrets: ClassVar[reconcile.SecretApi | None] = None
    aws: ClassVar[Provider | None] = None
    azure: ClassVar[dict[str, Provider]] = {}

    @classmethod
    def provider(cls, s: spec.SyncSpec) -> Provider:
        if s.provider == "aws":
            if cls.aws is None:
                cls.aws = AwsSecretsManager()
            return cls.aws
        if s.vault_url is None:  # impossible after spec.parse; keeps the type checker honest
            raise spec.SpecError("azure provider without a vault URL")
        if s.vault_url not in cls.azure:
            cls.azure[s.vault_url] = AzureKeyVault(s.vault_url)
        return cls.azure[s.vault_url]


@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, logger: Any, **_: Any) -> None:
    # kopf's own bookkeeping goes into annotations under OUR prefix, not kopf.zalando.org.
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix="secret-sync.code-clinic.io")
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(prefix="secret-sync.code-clinic.io")
    # Log messages (ours and kopf's "Handler ... failed permanently: <why>") become Kubernetes
    # Events on the ConfigMap, where its owner looks. kopf 1.45 turned this OFF by default
    # (posting.loggers=False): without it users saw nothing (found by the e2e).
    settings.posting.loggers = True
    settings.posting.level = logging.INFO
    # The Azure SDK logs every HTTP request and response (headers included) at INFO.
    logging.getLogger("azure").setLevel(logging.WARNING)
    try:
        kube_config.load_incluster_config()
    except kube_config.ConfigException:
        kube_config.load_kube_config()
    Runtime.policy = policy_mod.load(POLICY_PATH)  # a broken policy stops the operator: fail closed
    Runtime.secrets = KubeSecrets()
    start_http_server(int(os.environ.get("SECRET_SYNC_METRICS_PORT", "9100")))
    logger.info(f"policy loaded: {len(Runtime.policy.namespaces)} namespace(s) allowed")


def sync(namespace: str, name: str, uid: str, annotations: dict[str, str], data: dict[str, Any], logger: Any) -> str:
    """Shared by every handler. Never logs values."""
    try:
        s = spec.parse(namespace, name, annotations, data)
        Runtime.policy.check(s)
        if Runtime.secrets is None:
            raise kopf.TemporaryError("operator still starting", delay=5)
        result = reconcile.apply(s, reconcile.Owner(name, uid), Runtime.provider(s), Runtime.secrets)
    except (spec.SpecError, policy_mod.PolicyError, reconcile.TakeoverError) as exc:
        SYNCS.labels("rejected").inc()
        raise kopf.PermanentError(str(exc)) from None
    except ProviderError as exc:
        SYNCS.labels("store_error").inc()
        if exc.retryable:
            raise kopf.TemporaryError(str(exc), delay=30) from None
        raise kopf.PermanentError(str(exc)) from None
    SYNCS.labels(result).inc()
    LAST_SUCCESS.labels(namespace, name).set_to_current_time()
    if result != "unchanged":
        logger.info(f"Secret {s.secret_name} {result} ({len(s.items)} key(s) from {s.provider})")
    return result


# Handlers return None: kopf stores return values in `.status`, which ConfigMaps don't have
# (every resync then logged "Merge-patching finished with inconsistencies": found by the e2e).
@kopf.on.create("configmaps", annotations=SELECT)
@kopf.on.update("configmaps", annotations=SELECT)
@kopf.on.resume("configmaps", annotations=SELECT)
def on_change(**kwargs: Any) -> None:
    _sync_from(kwargs)


@kopf.timer("configmaps", annotations=SELECT, interval=RESYNC_SECONDS, initial_delay=RESYNC_SECONDS, idle=None)
def resync(**kwargs: Any) -> None:
    """Picks up rotations in the cloud store; a no-op (no write) when nothing changed.

    A rejected ConfigMap (bad spec, policy, takeover) was already reported by on_change; the
    timer re-checks it quietly (the policy may have changed) instead of an ERROR every cycle.
    """
    try:
        _sync_from(kwargs)
    except kopf.PermanentError as exc:
        kwargs["logger"].debug(f"still rejected: {exc}")


def _sync_from(kw: dict[str, Any]) -> str:
    data = dict(kw["body"].get("data") or {})
    return sync(kw["namespace"], kw["name"], kw["uid"], dict(kw["annotations"]), data, kw["logger"])
