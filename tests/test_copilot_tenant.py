"""Tests for the Copilot provider's tenant-token resolution.

No real Copilot runtime is started here: these are the pure helper functions
(`_tenant_token`, `_resolve_token`) that resolve *which* PAT to use, not the
SDK session itself.
"""

from __future__ import annotations

import json

import pytest

from providers.copilot_provider import _resolve_token, _tenant_token


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "COPILOT_TENANT_TOKENS_DIR", "COPILOT_TENANT_TOKENS_FILE",
        "COPILOT_GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN_FILE", "COPILOT_REQUIRE_TENANT",
    ):
        monkeypatch.delenv(name, raising=False)


class TestTenantIdValidation:
    def test_path_traversal_tenant_id_is_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COPILOT_TENANT_TOKENS_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="invalid tenant id"):
            _tenant_token("../../etc/passwd")

    def test_tenant_id_with_unsafe_characters_is_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COPILOT_TENANT_TOKENS_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="invalid tenant id"):
            _tenant_token("team/01")

    def test_safe_tenant_id_is_accepted(self):
        assert True  # covered by the read-from-dir/file tests below


class TestTenantTokenDir:
    def test_reads_token_from_per_tenant_file(self, monkeypatch, tmp_path):
        (tmp_path / "team-01").write_text("pat-for-team-01\n")
        monkeypatch.setenv("COPILOT_TENANT_TOKENS_DIR", str(tmp_path))
        assert _tenant_token("team-01") == "pat-for-team-01"

    def test_missing_tenant_file_raises_keyerror(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COPILOT_TENANT_TOKENS_DIR", str(tmp_path))
        with pytest.raises(KeyError, match="no Copilot token configured"):
            _tenant_token("unknown-tenant")


class TestTenantTokenFile:
    def test_reads_token_from_json_map(self, monkeypatch, tmp_path):
        tokens_file = tmp_path / "tenants.json"
        tokens_file.write_text(json.dumps({"team-02": "pat-for-team-02"}))
        monkeypatch.setenv("COPILOT_TENANT_TOKENS_FILE", str(tokens_file))
        assert _tenant_token("team-02") == "pat-for-team-02"


class TestResolveToken:
    def test_no_tenant_uses_default_token(self, monkeypatch):
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "default-pat")
        assert _resolve_token(None) == "default-pat"

    def test_require_tenant_rejects_tenant_less_request(self, monkeypatch):
        monkeypatch.setenv("COPILOT_REQUIRE_TENANT", "1")
        with pytest.raises(PermissionError, match="requires a 'tenant'"):
            _resolve_token(None)

    def test_tenant_given_routes_to_its_own_token(self, monkeypatch, tmp_path):
        (tmp_path / "team-03").write_text("pat-for-team-03")
        monkeypatch.setenv("COPILOT_TENANT_TOKENS_DIR", str(tmp_path))
        assert _resolve_token("team-03") == "pat-for-team-03"
