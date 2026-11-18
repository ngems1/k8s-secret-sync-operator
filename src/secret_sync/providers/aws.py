"""AWS Secrets Manager.

Credentials come from the standard chain: on EKS, IRSA or EKS Pod Identity give the pod a role
with no stored keys. `AWS_ENDPOINT_URL` (honoured by boto3 itself) points it at an emulator in
tests.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from secret_sync.providers import ProviderError

# Errors that a retry can't fix: wrong id, deleted secret, missing permission.
PERMANENT = {
    "ResourceNotFoundException",
    "InvalidRequestException",
    "InvalidParameterException",
    "AccessDeniedException",
    "DecryptionFailure",
}


# Fail fast and let the operator retry visibly (StoreUnavailable Event + backoff) instead of
# botocore's 60 s connect timeout hiding an outage inside one handler call (drill 2).
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 3})


class AwsSecretsManager:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client or boto3.client("secretsmanager", config=CLIENT_CONFIG)

    def get(self, secret_id: str) -> str:
        try:
            response = self._client.get_secret_value(SecretId=secret_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            raise ProviderError(f"aws: {secret_id!r}: {code}", retryable=code not in PERMANENT) from None
        except BotoCoreError as exc:
            raise ProviderError(f"aws: {secret_id!r}: {type(exc).__name__}", retryable=True) from None
        if "SecretString" in response:
            return str(response["SecretString"])
        binary = response.get("SecretBinary", b"")
        try:
            return bytes(binary).decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderError(f"aws: {secret_id!r}: binary secret is not UTF-8 text", retryable=False) from None
