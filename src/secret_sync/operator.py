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
from collections.abc import Callable
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
    # Events are posted explicitly (kopf.event) with clear reasons: Synced, Rejected,
    # StoreError, StoreUnavailable. kopf's own log lines ("Timer 'resync' succeeded" every
    # cycle) stay in the logs: posting them as Events buried the useful ones (e2e + drills).
    settings.posting.loggers = False
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


class StoreError(kopf.PermanentError):
    """The store answered, but retrying won't help (not found, denied): actionable, never silent."""


Post = Callable[[str, str, str], None]  # (type, reason, message) → a Kubernetes Event


def sync(
    namespace: str,
    name: str,
    uid: str,
    annotations: dict[str, str],
    data: dict[str, Any],
    logger: Any,
    post: Post,
    report_rejections: bool = True,
) -> str:
    """Shared by every handler. Never logs or posts values: only ids, counts and reasons."""
    try:
        s = spec.parse(namespace, name, annotations, data)
        Runtime.policy.check(s)
        if Runtime.secrets is None:
            raise kopf.TemporaryError("operator still starting", delay=5)
        result = reconcile.apply(s, reconcile.Owner(name, uid), Runtime.provider(s), Runtime.secrets)
    except (spec.SpecError, policy_mod.PolicyError, reconcile.TakeoverError) as exc:
        SYNCS.labels("rejected").inc()
        if report_rejections:
            post("Warning", "Rejected", str(exc))
        raise kopf.PermanentError(str(exc)) from None
    except ProviderError as exc:
        SYNCS.labels("store_error").inc()
        if exc.retryable:
            post("Warning", "StoreUnavailable", f"{exc}; will retry, Secret left unchanged")
            raise kopf.TemporaryError(str(exc), delay=30) from None
        # e.g. the secret was deleted from the store: the Secret keeps its last value, but the
        # owner must know, at every resync (found by drill 2).
        post("Warning", "StoreError", f"{exc}; Secret left unchanged")
        raise StoreError(str(exc)) from None
    SYNCS.labels(result).inc()
    LAST_SUCCESS.labels(namespace, name).set_to_current_time()
    if result != "unchanged":
        message = f"Secret {s.secret_name} {result} ({len(s.items)} key(s) from {s.provider})"
        logger.info(message)
        post("Normal", "Synced", message)
    return result


# Handlers return None: kopf stores return values in `.status`, which ConfigMaps don't have
# (every resync then logged "Merge-patching finished with inconsistencies": found by the e2e).
@kopf.on.create("configmaps", annotations=SELECT)
@kopf.on.update("configmaps", annotations=SELECT)
@kopf.on.resume("configmaps", annotations=SELECT)
def on_change(**kwargs: Any) -> None:
    _sync_from(kwargs, report_rejections=True)


@kopf.timer("configmaps", annotations=SELECT, interval=RESYNC_SECONDS, initial_delay=RESYNC_SECONDS, idle=None)
def resync(**kwargs: Any) -> None:
    """Picks up rotations in the cloud store; a no-op (no write, no Event) when nothing changed.

    A rejected ConfigMap (bad spec, policy, takeover) was reported once by on_change; the timer
    re-checks it quietly (the policy may have changed). Store errors get an Event every time.
    """
    try:
        _sync_from(kwargs, report_rejections=False)
    except kopf.PermanentError as exc:
        kwargs["logger"].debug(f"not synced: {exc}")


def _sync_from(kw: dict[str, Any], report_rejections: bool) -> str:
    body = kw["body"]

    def post(event_type: str, reason: str, message: str) -> None:
        kopf.event(body, type=event_type, reason=reason, message=message)

    data = dict(body.get("data") or {})
    return sync(
        kw["namespace"], kw["name"], kw["uid"], dict(kw["annotations"]), data, kw["logger"], post, report_rejections
    )
