"""Reconcile rules (idempotent, all-or-nothing, owned, no takeover) and the kopf error mapping."""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import Any

import kopf
import pytest
from conftest import FakeProvider, FakeSecrets, ann

from secret_sync import operator
from secret_sync.policy import parse as parse_policy
from secret_sync.providers import ProviderError
from secret_sync.reconcile import ANN_HASH, LABEL_MANAGED, Owner, TakeoverError, apply, content_hash
from secret_sync.spec import parse as parse_spec

OWNER = Owner("app-db", "uid-1")
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
SPEC = parse_spec("team-a", "app-db", ann(), {"PW": "team-a/db#password", "KEY": "team-a/key"})


def provider(pw: str = "pw-1") -> FakeProvider:
    return FakeProvider({"team-a/db": f'{{"password": "{pw}"}}', "team-a/key": "k-1"})


def decoded(secrets: FakeSecrets, key: str) -> str:
    return base64.b64decode(secrets.store[("team-a", "app-db")]["data"][key]).decode()


class TestReconcile:
    def test_create_then_unchanged_then_update(self) -> None:
        secrets = FakeSecrets()
        assert apply(SPEC, OWNER, provider(), secrets, lambda: NOW) == "created"
        body = secrets.store[("team-a", "app-db")]
        assert body["metadata"]["labels"][LABEL_MANAGED] == "secret-sync"
        assert body["metadata"]["ownerReferences"][0] | {} == {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "name": "app-db",
            "uid": "uid-1",
            "controller": True,
            "blockOwnerDeletion": True,
        }
        assert decoded(secrets, "PW") == "pw-1"
        assert apply(SPEC, OWNER, provider(), secrets) == "unchanged"
        assert secrets.writes == ["create"]  # no write when nothing changed
        assert apply(SPEC, OWNER, provider("pw-2"), secrets) == "updated"
        assert decoded(secrets, "PW") == "pw-2"
        assert secrets.writes == ["create", "replace"]

    def test_all_or_nothing(self) -> None:
        secrets = FakeSecrets()
        apply(SPEC, OWNER, provider(), secrets)
        broken = FakeProvider({"team-a/db": '{"password": "pw-2"}'}, {"team-a/key": ProviderError("x", retryable=True)})
        with pytest.raises(ProviderError):
            apply(SPEC, OWNER, broken, secrets)
        assert decoded(secrets, "PW") == "pw-1"  # untouched: no half-updated Secret
        assert secrets.writes == ["create"]

    def test_refuses_to_overwrite_a_foreign_secret(self) -> None:
        secrets = FakeSecrets()
        secrets.create("team-a", {"metadata": {"name": "app-db"}, "data": {"tls.key": "b2xk"}})
        with pytest.raises(TakeoverError, match="refusing to overwrite"):
            apply(SPEC, OWNER, provider(), secrets)
        assert secrets.store[("team-a", "app-db")]["data"] == {"tls.key": "b2xk"}

    def test_refuses_a_secret_owned_by_another_configmap(self) -> None:
        secrets = FakeSecrets()
        apply(SPEC, Owner("other", "uid-2"), provider(), secrets)
        with pytest.raises(TakeoverError):
            apply(SPEC, OWNER, provider(), secrets)

    def test_hash_is_order_independent_and_changes_with_values(self) -> None:
        assert content_hash({"a": "1", "b": "2"}) == content_hash({"b": "2", "a": "1"})
        assert content_hash({"a": "1"}) != content_hash({"a": "2"})

    def test_a_tampered_secret_is_restored(self) -> None:
        secrets = FakeSecrets()
        apply(SPEC, OWNER, provider(), secrets)
        secrets.store[("team-a", "app-db")]["data"]["PW"] = base64.b64encode(b"hacked").decode()
        assert ANN_HASH in secrets.store[("team-a", "app-db")]["metadata"]["annotations"]
        assert apply(SPEC, OWNER, provider(), secrets) == "updated"
        assert decoded(secrets, "PW") == "pw-1"


class Logs:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg: str) -> None:
        self.lines.append(msg)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> FakeSecrets:
    secrets = FakeSecrets()
    monkeypatch.setattr(
        operator.Runtime, "policy", parse_policy({"namespaces": {"team-a": {"aws_prefixes": ["team-a/"]}}})
    )
    monkeypatch.setattr(operator.Runtime, "secrets", secrets)
    monkeypatch.setattr(operator.Runtime, "aws", provider())
    return secrets


DATA = {"PW": "team-a/db#password", "KEY": "team-a/key"}


class Events:
    """Collects (type, reason, message) like kopf.event would post them."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str, str]] = []

    def __call__(self, event_type: str, reason: str, message: str) -> None:
        self.posted.append((event_type, reason, message))

    @property
    def reasons(self) -> list[str]:
        return [r for _, r, _ in self.posted]


def run(
    namespace: str, data: dict[str, str], *, ann_: dict[str, str] | None = None, report: bool = True
) -> tuple[str, Events, Logs]:
    events, logs = Events(), Logs()
    result = operator.sync(namespace, "app-db", "uid-1", ann_ or ann(), data, logs, events, report)
    return result, events, logs


class TestOperator:
    def test_sync_success_posts_one_event_and_logs_no_values(self, runtime: FakeSecrets) -> None:
        result, events, logs = run("team-a", DATA)
        assert result == "created"
        assert events.posted == [("Normal", "Synced", "Secret app-db created (2 key(s) from aws)")]
        assert logs.lines == ["Secret app-db created (2 key(s) from aws)"]
        assert not any("pw-1" in m or "k-1" in m for _, _, m in events.posted)
        result, events, _ = run("team-a", DATA)
        assert (result, events.posted) == ("unchanged", [])  # no write, no Event

    @pytest.mark.parametrize(
        ("namespace", "ann_", "data", "match"),
        [
            ("team-a", ann("gcp"), DATA, "must be one of"),
            ("team-b", ann(), DATA, "not allowed to sync"),
            ("team-a", ann(), {"K": "team-b/db"}, "outside this namespace"),
        ],
    )
    def test_rejections_post_a_warning_once(
        self, runtime: FakeSecrets, namespace: str, ann_: dict[str, str], data: dict[str, str], match: str
    ) -> None:
        events = Events()
        with pytest.raises(kopf.PermanentError, match=match):
            operator.sync(namespace, "cm", "uid", ann_, data, Logs(), events)
        assert events.reasons == ["Rejected"]
        quiet = Events()
        with pytest.raises(kopf.PermanentError):
            operator.sync(namespace, "cm", "uid", ann_, data, Logs(), quiet, report_rejections=False)
        assert quiet.posted == []
        assert runtime.writes == []

    def test_store_errors(self, runtime: FakeSecrets, monkeypatch: pytest.MonkeyPatch) -> None:
        down = FakeProvider({}, {"team-a/key": ProviderError("aws: throttled", retryable=True)})
        monkeypatch.setattr(operator.Runtime, "aws", down)
        events = Events()
        with pytest.raises(kopf.TemporaryError):
            operator.sync("team-a", "cm", "uid", ann(), {"K": "team-a/key"}, Logs(), events)
        with pytest.raises(operator.StoreError, match="not found"):
            operator.sync("team-a", "cm", "uid", ann(), {"K": "team-a/missing"}, Logs(), events, False)
        assert events.reasons == ["StoreUnavailable", "StoreError"]  # store errors: always reported

    def test_takeover_is_rejected(self, runtime: FakeSecrets) -> None:
        runtime.create("team-a", {"metadata": {"name": "cm"}, "data": {}})
        events = Events()
        with pytest.raises(kopf.PermanentError, match="refusing"):
            operator.sync("team-a", "cm", "uid", ann(), {"K": "team-a/key"}, Logs(), events)
        assert events.reasons == ["Rejected"]

    def test_not_started_yet_is_temporary(self, runtime: FakeSecrets, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(operator.Runtime, "secrets", None)
        with pytest.raises(kopf.TemporaryError, match="still starting"):
            operator.sync("team-a", "cm", "uid", ann(), {"K": "team-a/key"}, Logs(), Events())

    def test_handlers_post_through_kopf_and_return_none(
        self, runtime: FakeSecrets, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        posted: list[tuple[str, str]] = []
        monkeypatch.setattr(kopf, "event", lambda body, *, type, reason, message: posted.append((type, reason)))

        class Debug(Logs):
            def debug(self, msg: str) -> None:
                self.lines.append("debug: " + msg)

        kw: dict[str, Any] = {
            "namespace": "team-a",
            "name": "app-db",
            "uid": "uid-1",
            "annotations": ann(),
            "body": {"data": DATA},
            "logger": Debug(),
        }
        assert operator.on_change(**kw) is None  # ConfigMaps have no .status to store results in
        assert operator.resync(**kw) is None
        assert runtime.writes == ["create"]
        assert posted == [("Normal", "Synced")]
        rejected = {**kw, "namespace": "team-z", "logger": Debug()}
        operator.resync(**rejected)  # quiet: no exception, no Event
        assert posted == [("Normal", "Synced")]
        assert rejected["logger"].lines[0].startswith("debug: not synced: namespace 'team-z'")
        with pytest.raises(kopf.PermanentError):
            operator.on_change(**rejected)
        assert posted[-1] == ("Warning", "Rejected")

    def test_provider_clients_are_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(operator.Runtime, "aws", None)
        monkeypatch.setattr(operator.Runtime, "azure", {})
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        aws_spec = parse_spec("ns", "cm", ann(), {"K": "x"})
        assert operator.Runtime.provider(aws_spec) is operator.Runtime.provider(aws_spec)
        az = parse_spec("ns", "cm", ann("azure", **{"vault-url": "https://kv.example.net"}), {"K": "x"})
        monkeypatch.setenv("SECRET_SYNC_AZURE_EMULATOR", "1")
        logging.getLogger("secret_sync").setLevel(logging.ERROR)
        assert operator.Runtime.provider(az) is operator.Runtime.provider(az)


class TestKubeSecrets:
    def test_read_create_replace(self) -> None:
        from unittest.mock import MagicMock

        from kubernetes.client import V1ObjectMeta, V1Secret
        from kubernetes.client.exceptions import ApiException

        from secret_sync.kube import KubeSecrets

        api = MagicMock()
        api.read_namespaced_secret.return_value = V1Secret(metadata=V1ObjectMeta(name="s"), data={"k": "dg=="})
        kube = KubeSecrets(api)
        assert kube.read("ns", "s") == {"metadata": {"name": "s"}, "data": {"k": "dg=="}}
        api.read_namespaced_secret.side_effect = ApiException(status=404)
        assert kube.read("ns", "missing") is None
        api.read_namespaced_secret.side_effect = ApiException(status=403)
        with pytest.raises(ApiException):
            kube.read("ns", "forbidden")
        kube.create("ns", {"metadata": {"name": "s"}})
        kube.replace("ns", "s", {"metadata": {"name": "s"}})
        api.create_namespaced_secret.assert_called_once_with("ns", {"metadata": {"name": "s"}})
        api.replace_namespaced_secret.assert_called_once_with("s", "ns", {"metadata": {"name": "s"}})
