"""Providers: AWS on Moto's in-process mock, Azure on a fake client. Errors never carry values."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from botocore.exceptions import ClientError, EndpointConnectionError
from conftest import FakeProvider
from moto import mock_aws

from secret_sync.providers import ProviderError, resolve
from secret_sync.providers.aws import AwsSecretsManager
from secret_sync.providers.azure import AzureKeyVault, _EmulatorCredential
from secret_sync.spec import Ref


@pytest.fixture
def sm(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3.client("secretsmanager")


class TestAws:
    def test_string_json_and_binary(self, sm: Any) -> None:
        sm.create_secret(Name="team-a/db", SecretString='{"user": "app", "password": "pw-1", "port": 5432}')
        sm.create_secret(Name="team-a/bin", SecretBinary=b"binary-ok")
        aws = AwsSecretsManager(sm)
        assert resolve(aws, Ref("team-a/db", "password")) == "pw-1"
        assert resolve(aws, Ref("team-a/db", "port")) == "5432"
        assert aws.get("team-a/bin") == "binary-ok"

    def test_not_found_is_permanent_and_has_no_value(self, sm: Any) -> None:
        with pytest.raises(ProviderError, match="ResourceNotFoundException") as exc:
            AwsSecretsManager(sm).get("team-a/missing")
        assert exc.value.retryable is False

    def test_throttling_is_retryable(self) -> None:
        class Throttled:
            def get_secret_value(self, **_: Any) -> Any:
                raise ClientError({"Error": {"Code": "ThrottlingException"}}, "GetSecretValue")

        with pytest.raises(ProviderError) as exc:
            AwsSecretsManager(Throttled()).get("x")
        assert exc.value.retryable is True

    def test_network_error_is_retryable(self) -> None:
        class Down:
            def get_secret_value(self, **_: Any) -> Any:
                raise EndpointConnectionError(endpoint_url="https://secretsmanager.example")

        with pytest.raises(ProviderError, match="EndpointConnectionError") as exc:
            AwsSecretsManager(Down()).get("x")
        assert exc.value.retryable is True

    def test_non_utf8_binary(self) -> None:
        class Binary:
            def get_secret_value(self, **_: Any) -> Any:
                return {"SecretBinary": b"\xff\xfe"}

        with pytest.raises(ProviderError, match="not UTF-8"):
            AwsSecretsManager(Binary()).get("x")


class TestResolve:
    def test_field_errors(self) -> None:
        p = FakeProvider({"plain": "s3cret", "json": '{"a": 1}', "list": "[1]"})
        with pytest.raises(ProviderError, match="is not JSON") as exc:
            resolve(p, Ref("plain", "a"))
        assert "s3cret" not in str(exc.value)
        with pytest.raises(ProviderError, match="has no field 'b'"):
            resolve(p, Ref("json", "b"))
        with pytest.raises(ProviderError, match="has no field"):
            resolve(p, Ref("list", "a"))


class FakeSecret:
    def __init__(self, value: str | None) -> None:
        self.value = value


class FakeKv:
    def __init__(self, result: Any) -> None:
        self.result = result

    def get_secret(self, name: str) -> FakeSecret:
        if isinstance(self.result, Exception):
            raise self.result
        return FakeSecret(self.result)


def http_error(status: int) -> HttpResponseError:
    err = HttpResponseError(message="boom")
    err.status_code = status
    return err


class TestAzure:
    def test_value(self) -> None:
        assert AzureKeyVault("https://kv.example.net", FakeKv("v")).get("k") == "v"

    @pytest.mark.parametrize(
        ("error", "retryable", "match"),
        [
            (ResourceNotFoundError("nf"), False, "not found"),
            (ClientAuthenticationError("auth"), False, "authentication"),
            (http_error(403), False, "HTTP 403"),
            (http_error(429), True, "HTTP 429"),
            (http_error(503), True, "HTTP 503"),
            (ServiceRequestError("dns"), True, "ServiceRequestError"),
        ],
    )
    def test_errors(self, error: Exception, retryable: bool, match: str) -> None:
        with pytest.raises(ProviderError, match=match) as exc:
            AzureKeyVault("https://kv.example.net", FakeKv(error)).get("k")
        assert exc.value.retryable is retryable

    def test_empty_value(self) -> None:
        with pytest.raises(ProviderError, match="has no value"):
            AzureKeyVault("https://kv.example.net", FakeKv(None)).get("k")

    def test_client_construction_modes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SECRET_SYNC_AZURE_EMULATOR", "1")
        assert AzureKeyVault("https://kv.example.net")._client is not None
        monkeypatch.delenv("SECRET_SYNC_AZURE_EMULATOR")
        assert AzureKeyVault("https://kv.example.net")._client is not None  # DefaultAzureCredential, lazy
        assert _EmulatorCredential().get_token("scope").token == "emulator-token"

    def test_clients_fail_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Short SDK retries: the operator's own retries post Events (drill 2)."""
        seen: dict[str, Any] = {}

        class Spy:
            def __init__(self, url: str, credential: Any, **kwargs: Any) -> None:
                seen.update(kwargs)

        monkeypatch.setattr("azure.keyvault.secrets.SecretClient", Spy)
        for emulator in ("1", "0"):
            seen.clear()
            monkeypatch.setenv("SECRET_SYNC_AZURE_EMULATOR", emulator)
            AzureKeyVault("https://kv.example.net")
            assert seen["retry_total"] == 2
            assert seen["connection_timeout"] == 5


def test_aws_client_fails_fast(sm: Any) -> None:
    config = AwsSecretsManager()._client.meta.config
    assert (config.connect_timeout, config.read_timeout) == (5, 10)
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}
