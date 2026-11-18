"""Parsing ConfigMaps, and the namespace policy that keeps tenants apart."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ann

from secret_sync.policy import Policy, PolicyError, load, parse
from secret_sync.spec import Ref, SpecError, wants_sync
from secret_sync.spec import parse as parse_spec

VAULT = "https://kv-team-a.vault.azure.net"


class TestSpec:
    def test_aws(self) -> None:
        s = parse_spec("team-a", "app-db", ann(), {"PW": "team-a/db#password", "KEY": "team-a/key"})
        assert (s.provider, s.secret_name, s.vault_url) == ("aws", "app-db", None)
        assert s.items == {"KEY": Ref("team-a/key"), "PW": Ref("team-a/db", "password")}

    def test_azure_with_custom_name_and_trailing_slash(self) -> None:
        s = parse_spec(
            "ns", "cm", ann("azure", **{"vault-url": VAULT + "/", "secret-name": "creds"}), {"K": "signing-key"}
        )
        assert (s.vault_url, s.secret_name) == (VAULT, "creds")

    def test_wants_sync(self) -> None:
        assert wants_sync(ann())
        assert not wants_sync({"secret-sync.code-clinic.io/sync": "yes"})
        assert not wants_sync(None)

    @pytest.mark.parametrize(
        ("ann", "data", "error"),
        [
            (ann("gcp"), {"K": "x"}, "must be one of aws, azure"),
            (ann(**{"secret-name": "Bad_Name"}), {"K": "x"}, "not a valid Secret name"),
            (ann("azure"), {"K": "x"}, "https:// Key Vault URL"),
            (ann("azure", **{"vault-url": "http://kv.example.net"}), {"K": "x"}, "https:// Key Vault URL"),
            (ann(**{"vault-url": VAULT}), {"K": "x"}, "only applies to provider azure"),
            (ann(), {}, "nothing to sync"),
            (ann(), {"bad key!": "x"}, "not a valid Secret key"),
            (ann(), {"K": ""}, "expected '<secret id>'"),
            (ann(), {"K": "id#"}, "expected '<secret id>'"),
            (ann(), {"K": 5}, "must be a string"),
        ],
    )
    def test_errors(self, ann: dict[str, str], data: dict[str, object], error: str) -> None:
        with pytest.raises(SpecError, match=error):
            parse_spec("ns", "cm", ann, data)


POLICY = parse({"namespaces": {"team-a": {"aws_prefixes": ["team-a/"], "azure_vaults": [VAULT + "/"]}}})


class TestPolicy:
    def test_allowed_aws_and_azure(self) -> None:
        POLICY.check(parse_spec("team-a", "cm", ann(), {"K": "team-a/app/db"}))
        POLICY.check(parse_spec("team-a", "cm", ann("azure", **{"vault-url": VAULT}), {"K": "x"}))

    def test_other_teams_prefix_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="outside this namespace's prefixes"):
            POLICY.check(parse_spec("team-a", "cm", ann(), {"OK": "team-a/x", "STEAL": "team-b/db"}))

    def test_prefix_is_not_a_substring_match(self) -> None:
        with pytest.raises(PolicyError):
            POLICY.check(parse_spec("team-a", "cm", ann(), {"K": "prod/team-a/db"}))

    def test_unlisted_namespace_is_refused(self) -> None:
        with pytest.raises(PolicyError, match="is not allowed to sync"):
            POLICY.check(parse_spec("team-z", "cm", ann(), {"K": "team-a/x"}))

    def test_unlisted_vault_is_refused(self) -> None:
        other = ann("azure", **{"vault-url": "https://kv-team-b.vault.azure.net"})
        with pytest.raises(PolicyError, match="is not allowed for namespace"):
            POLICY.check(parse_spec("team-a", "cm", other, {"K": "x"}))

    def test_empty_policy_denies_everything(self) -> None:
        with pytest.raises(PolicyError):
            Policy().check(parse_spec("team-a", "cm", ann(), {"K": "team-a/x"}))

    @pytest.mark.parametrize(
        ("data", "error"),
        [
            ({"tenants": {}}, "unknown key"),
            ({"namespaces": {"a": {"aws": []}}}, "accepts only"),
            ({"namespaces": {"a": {"aws_prefixes": "a/"}}}, "list of non-empty strings"),
            ({"namespaces": {"a": {"aws_prefixes": [""]}}}, "list of non-empty strings"),
            ({"namespaces": {"a": {"aws_prefixes": ["*"]}}}, "must name something"),
            ({"namespaces": {"a": {"aws_prefixes": ["team"]}}}, "must end with '/'"),
        ],
    )
    def test_invalid_policies(self, data: dict[str, object], error: str) -> None:
        with pytest.raises(PolicyError, match=error):
            parse(data)

    def test_load(self, tmp_path: Path) -> None:
        f = tmp_path / "policy.toml"
        f.write_text('[namespaces.team-a]\naws_prefixes = ["team-a/"]\n', encoding="utf-8")
        assert load(f).namespaces["team-a"].aws_prefixes == ("team-a/",)
        f.write_text("[namespaces", encoding="utf-8")
        with pytest.raises(PolicyError, match="invalid TOML"):
            load(f)
        with pytest.raises(PolicyError, match="cannot read"):
            load(tmp_path / "missing.toml")

    def test_shipped_policies_are_valid(self) -> None:
        root = Path(__file__).resolve().parent.parent
        assert "team-a" in load(root / "deploy" / "policy.toml").namespaces
        assert "team-b" in load(root / "tests" / "e2e" / "overlay" / "policy.toml").namespaces
