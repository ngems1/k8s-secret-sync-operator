"""Secret stores. Each provider turns a secret id into its current value, or raises ProviderError.

Values are never logged, never put in exception messages, never returned in events.
"""

from __future__ import annotations

import json
from typing import Protocol

from secret_sync.spec import Ref


class ProviderError(Exception):
    """Fetching failed. `retryable` is False when retrying can't help (not found, denied)."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Provider(Protocol):
    def get(self, secret_id: str) -> str:
        """The secret's current value as text."""
        ...


def resolve(provider: Provider, ref: Ref) -> str:
    """Fetch one value; for `id#field`, the secret must be a JSON object containing `field`."""
    raw = provider.get(ref.secret_id)
    if ref.field is None:
        return raw
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        raise ProviderError(
            f"{ref.secret_id!r} is not JSON, so it has no field {ref.field!r}", retryable=False
        ) from None
    if not isinstance(document, dict) or ref.field not in document:
        raise ProviderError(f"{ref.secret_id!r} has no field {ref.field!r}", retryable=False)
    value = document[ref.field]
    return value if isinstance(value, str) else json.dumps(value)
