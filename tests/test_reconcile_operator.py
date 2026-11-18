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


class TestOperator:
    def test_sync_success_logs_no_values(self, runtime: FakeSecrets) -> None:
        logs = Logs()
        assert operator.sync("team-a", "app-db", "uid-1", ann(), DATA, logs) == "created"
        assert logs.lines == ["Secret app-db created (2 key(s) from aws)"]
        assert not any("pw-1" in line or "k-1" in line for line in logs.lines)
        assert operator.sync("team-a", "app-db", "uid-1", ann(), DATA, logs) == "unchanged"
        assert len(logs.lines) == 1  # nothing logged (no Event) when nothing changed

    @pytest.mark.parametrize(
        ("namespace", "ann", "data", "match"),
        [
            ("team-a", ann("gcp"), DATA, "must be one of"),
            ("team-b", ann(), DATA, "not allowed to sync"),
            ("team-a", ann(), {"K": "team-b/db"}, "outside this namespace"),
        ],
    )
    def test_rejections_are_permanent(
        self, runtime: FakeSecrets, namespace: str, ann: dict[str, str], data: dict[str, str], match: str
    ) -> None:
        with pytest.raises(kopf.PermanentError, match=match):
            operator.sync(namespace, "cm", "uid", ann, data, Logs())
        assert runtime.writes == []

    def test_store_errors(self, runtime: FakeSecrets, monkeypatch: pytest.MonkeyPatch) -> None:
        down = FakeProvider({}, {"team-a/key": ProviderError("aws: throttled", retryable=True)})
        monkeypatch.setattr(operator.Runtime, "aws", down)
        with pytest.raises(kopf.TemporaryError):
            operator.sync("team-a", "cm", "uid", ann(), {"K": "team-a/key"}, Logs())
        with pytest.raises(kopf.PermanentError, match="not found"):
            operator.sync("team-a", "cm", "uid", ann(), {"K": "team-a/missing"}, Logs())

    def test_takeover_is_permanent(self, runtime: FakeSecrets) -> None:
        runtime.create("team-a", {"metadata": {"name": "cm"}, "data": {}})
        with pytest.raises(kopf.PermanentError, match="refusing"):
            operator.sync("team-a", "cm", "uid", ann(), {"K": "team-a/key"}, Logs())

    def test_not_started_yet_is_temporary(self, runtime: FakeSecrets, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(operator.Runtime, "secrets", None)
        with pytest.raises(kopf.TemporaryError, match="still starting"):
            operator.sync("team-a", "cm", "uid", ann(), {"K": "team-a/key"}, Logs())

    def test_handlers_pass_the_kopf_kwargs(self, runtime: FakeSecrets) -> None:
        kw: dict[str, Any] = {
            "namespace": "team-a",
            "name": "app-db",
            "uid": "uid-1",
            "annotations": ann(),
            "body": {"data": DATA},
            "logger": Logs(),
        }
        operator.on_change(**kw)  # returns None: ConfigMaps have no .status for kopf to store results in
        assert runtime.writes == ["create"]
        operator.resync(**kw)
        assert runtime.writes == ["create"]  # unchanged: no write

    def test_resync_is_quiet_about_rejected_configmaps(self, runtime: FakeSecrets) -> None:
        class Debug(Logs):
            def debug(self, msg: str) -> None:
                self.lines.append("debug: " + msg)

        logs = Debug()
        kw: dict[str, Any] = {
            "namespace": "team-z",
            "name": "cm",
            "uid": "u",
            "annotations": ann(),
            "body": {"data": DATA},
            "logger": logs,
        }
        operator.resync(**kw)  # no exception: on_change already reported it
        assert logs.lines == ["debug: still rejected: namespace 'team-z' is not allowed to sync secrets (policy.toml)"]
        with pytest.raises(kopf.PermanentError):
            operator.on_change(**kw)

    def test_resync_warns_when_the_store_lost_the_secret(self, runtime: FakeSecrets) -> None:
        class Warn(Logs):
            def warning(self, msg: str) -> None:
                self.lines.append("warning: " + msg)

        logs = Warn()
        kw: dict[str, Any] = {
            "namespace": "team-a",
            "name": "cm",
            "uid": "u",
            "annotations": ann(),
            "body": {"data": {"K": "team-a/deleted"}},
            "logger": logs,
        }
        operator.resync(**kw)
        assert logs.lines == ["warning: resync failed, Secret left unchanged: fake: 'team-a/deleted' not found"]

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
