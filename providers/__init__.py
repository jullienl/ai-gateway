"""Provider registry + `<provider>:<model>` routing.

Callers select a provider by prefixing the model id: `openai:gpt-4.1`,
`anthropic:claude-sonnet-4-5`. A bare model id with no prefix (`auto`,
`gpt-5.6-sol`, `claude-sonnet-5`, ...) uses `AI_PROVIDER`, which defaults to
`copilot`, so every existing caller and every shipped agent keeps working
unchanged unless an operator opts into another default.

Adding a provider: implement `ChatProvider` (see base.py) in its own module,
add one line to `_PROVIDERS` below. Nothing in app.py or agents.py needs to
change. Each module is imported lazily, on first actual use, so a deployment
that never uses e.g. "anthropic" never needs that package importable.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from .base import ChatProvider

DEFAULT_PROVIDER = "copilot"


def default_provider() -> str:
    """Return the configured provider used for bare model ids and startup."""
    return os.environ.get("AI_PROVIDER", DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER

# provider name -> (module, class name).
_PROVIDERS: dict[str, tuple[str, str]] = {
    "copilot": ("providers.copilot_provider", "CopilotProvider"),
    "openai": ("providers.openai_provider", "OpenAIProvider"),
    "anthropic": ("providers.anthropic_provider", "AnthropicProvider"),
}


def provider_names() -> list[str]:
    return sorted(_PROVIDERS)


def create_provider(name: str) -> Any:
    """Import and construct the provider registered under `name`.

    Raises KeyError (with the list of available providers) for an unknown name.
    Construction only instantiates the class; call `.start()` before use.
    """
    try:
        module_name, class_name = _PROVIDERS[name]
    except KeyError as exc:
        available = ", ".join(provider_names())
        raise KeyError(f"Unknown provider '{name}'. Available: {available}") from exc
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    return cls()


def parse_model_id(model_id: str) -> tuple[str, str]:
    """Split a `<provider>:<model>` id into `(provider, model)`.

    No `:` present -> the configured default provider and unchanged model id.
    """
    provider, sep, model = model_id.partition(":")
    if not sep:
        return default_provider(), model_id
    return provider, model


__all__ = [
    "ChatProvider",
    "DEFAULT_PROVIDER",
    "default_provider",
    "provider_names",
    "create_provider",
    "parse_model_id",
]
