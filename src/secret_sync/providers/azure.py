"""Azure Key Vault.

Credentials: DefaultAzureCredential, i.e. AKS Workload Identity in production (no stored
secret). For the local Key Vault emulator (Lowkey Vault) the operator can be started with
SECRET_SYNC_AZURE_EMULATOR=1: a dummy token and no TLS verification. That switch exists for
the lab only and logs a loud warning.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from azure.core.credentials import AccessToken
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
)

from secret_sync.providers import ProviderError

log = logging.getLogger(__name__)

# Fail fast and let the operator retry: its retries are visible (a StoreUnavailable Event, then
# kopf's backoff). The SDK default (10 retries, exponential backoff) blocked a handler for over
# a minute with no Event during an outage (found by drill 2).
CLIENT_OPTIONS: dict[str, Any] = {"retry_total": 2, "connection_timeout": 5, "read_timeout": 10}


class _EmulatorCredential:
    """A token the emulator accepts. NEVER used against a real vault."""

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("emulator-token", int(time.time()) + 3600)


class AzureKeyVault:
    def __init__(self, vault_url: str, client: Any | None = None) -> None:
        self.vault_url = vault_url
        if client is None:
            from azure.keyvault.secrets import SecretClient

            if os.environ.get("SECRET_SYNC_AZURE_EMULATOR") == "1":
                import urllib3

                log.warning("SECRET_SYNC_AZURE_EMULATOR=1: dummy credentials and NO TLS verification (lab only)")
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # said once, above
                client = SecretClient(
                    vault_url,
                    _EmulatorCredential(),
                    verify_challenge_resource=False,
                    connection_verify=False,
                    **CLIENT_OPTIONS,
                )
            else:
                from azure.identity import DefaultAzureCredential

                client = SecretClient(vault_url, DefaultAzureCredential(), **CLIENT_OPTIONS)
        self._client = client

    def get(self, secret_id: str) -> str:
        try:
            secret = self._client.get_secret(secret_id)
        except ResourceNotFoundError:
            raise ProviderError(f"azure: {secret_id!r} not found in {self.vault_url}", retryable=False) from None
        except ClientAuthenticationError:
            raise ProviderError(f"azure: authentication to {self.vault_url} failed", retryable=False) from None
        except HttpResponseError as exc:
            status = exc.status_code or 0
            raise ProviderError(
                f"azure: {secret_id!r}: HTTP {status}", retryable=status in (408, 429) or status >= 500
            ) from None
        except AzureError as exc:
            raise ProviderError(f"azure: {secret_id!r}: {type(exc).__name__}", retryable=True) from None
        if secret.value is None:
            raise ProviderError(f"azure: {secret_id!r} has no value", retryable=False)
        return str(secret.value)
