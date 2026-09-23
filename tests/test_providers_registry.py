"""Tests for the provider registry: `<provider>:<model>` routing."""

from __future__ import annotations

import pytest

from providers import DEFAULT_PROVIDER, create_provider, default_provider, parse_model_id, provider_names


def test_bare_model_id_defaults_to_copilot():
    assert parse_model_id("gpt-5.6-sol") == (DEFAULT_PROVIDER, "gpt-5.6-sol")
    assert parse_model_id("auto") == (DEFAULT_PROVIDER, "auto")


def test_bare_model_id_uses_configured_default_provider(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "OPENAI")
    assert default_provider() == "openai"
    assert parse_model_id("local-model") == ("openai", "local-model")


def test_prefixed_model_id_is_split():
    assert parse_model_id("openai:gpt-4.1") == ("openai", "gpt-4.1")
    assert parse_model_id("anthropic:claude-sonnet-4-5") == ("anthropic", "claude-sonnet-4-5")


def test_provider_names_lists_all_registered_providers():
    assert provider_names() == ["anthropic", "copilot", "openai"]


def test_create_provider_unknown_name_raises_keyerror():
    with pytest.raises(KeyError, match="Unknown provider 'bogus'"):
        create_provider("bogus")


def test_create_provider_returns_the_right_class():
    provider = create_provider("copilot")
    assert provider.name == "copilot"
