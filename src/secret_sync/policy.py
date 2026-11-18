"""Which namespace may read which secrets. DENY by default.

The operator's cloud identity can read many secrets (often every team's). Without this
policy, anyone allowed to create a ConfigMap in ANY namespace could ask the operator to copy
ANY of those secrets into a Secret they can read: a privilege escalation from "edit
ConfigMaps in my namespace" to "read the payment team's database password".

    # policy.toml (mounted from a ConfigMap only the platform team can edit)
    [namespaces.team-a]
    aws_prefixes = ["team-a/"]                                  # secret ids must start with one
    azure_vaults = ["https://kv-team-a.vault.azure.net"]       # vault URLs allowed

A namespace that isn't listed can't sync anything.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from secret_sync.spec import SyncSpec


class PolicyError(Exception):
    """The request is not allowed (or the policy file itself is invalid)."""


@dataclass(frozen=True)
class NamespaceRule:
    aws_prefixes: tuple[str, ...] = ()
    azure_vaults: tuple[str, ...] = ()


@dataclass
class Policy:
    namespaces: dict[str, NamespaceRule] = field(default_factory=dict)

    def check(self, spec: SyncSpec) -> None:
        rule = self.namespaces.get(spec.namespace)
        if rule is None:
            raise PolicyError(f"namespace {spec.namespace!r} is not allowed to sync secrets (policy.toml)")
        if spec.provider == "aws":
            for key, ref in spec.items.items():
                if not any(ref.secret_id.startswith(p) for p in rule.aws_prefixes):
                    allowed = ", ".join(rule.aws_prefixes) or "none"
                    raise PolicyError(
                        f"data[{key!r}]: secret {ref.secret_id!r} is outside this namespace's prefixes ({allowed})"
                    )
        elif spec.vault_url not in rule.azure_vaults:
            raise PolicyError(f"vault {spec.vault_url!r} is not allowed for namespace {spec.namespace!r}")


def load(path: Path) -> Policy:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise PolicyError(f"cannot read policy {path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{path}: invalid TOML: {exc}") from exc
    return parse(data)


def parse(data: dict[str, Any]) -> Policy:
    if set(data) - {"namespaces"}:
        raise PolicyError(f"policy: unknown key(s) {', '.join(sorted(set(data) - {'namespaces'}))}")
    namespaces: dict[str, NamespaceRule] = {}
    for ns, raw in (data.get("namespaces") or {}).items():
        if not isinstance(raw, dict) or set(raw) - {"aws_prefixes", "azure_vaults"}:
            raise PolicyError(f"policy: [namespaces.{ns}] accepts only aws_prefixes and azure_vaults")
        prefixes = raw.get("aws_prefixes", [])
        vaults = raw.get("azure_vaults", [])
        for name, values in (("aws_prefixes", prefixes), ("azure_vaults", vaults)):
            if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                raise PolicyError(f"policy: [namespaces.{ns}] {name} must be a list of non-empty strings")
        if any(p in ("", "*", "/") for p in prefixes):
            raise PolicyError(f"policy: [namespaces.{ns}] a prefix must name something (no '*' or '/')")
        # "team" would also match "team-b/..." (found by drill 1): a prefix must end at a "/".
        unterminated = [p for p in prefixes if not p.endswith("/")]
        if unterminated:
            raise PolicyError(f"policy: [namespaces.{ns}] aws_prefixes must end with '/': {', '.join(unterminated)}")
        namespaces[ns] = NamespaceRule(tuple(prefixes), tuple(v.rstrip("/") for v in vaults))
    return Policy(namespaces)
