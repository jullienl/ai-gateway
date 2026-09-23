"""Anthropic provider -- stateless Messages API backend.

Auth: ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY_FILE), file-first like every
other secret in this AI gateway.

Same statelessness caveat as the OpenAI provider: `session_id` is accepted but
unused, every call is a single turn. Same no-multi-tenant caveat: `tenant`
raises ValueError if supplied.
"""

from __future__ import annotations

from typing import Any

from secret_resolution import get_secret

_DEFAULT_MODEL = "claude-sonnet-4-5"
_DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider:
    """Wraps the Anthropic SDK's async Messages client."""

    name = "anthropic"

    def __init__(self) -> None:
        self._client: Any | None = None

    async def start(self) -> None:
        # Imported lazily: only a deployment that actually selects this
        # provider needs the `anthropic` package installed/importable.
        from anthropic import AsyncAnthropic

        api_key = get_secret("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY_FILE) is not set; "
                "cannot start the 'anthropic' provider"
            )
        self._client = AsyncAnthropic(api_key=api_key)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None

    async def run(
        self,
        *,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        session_id: str | None = None,
        tenant: str | None = None,
        timeout: float = 120.0,
    ) -> str:
        if tenant is not None:
            raise ValueError("tenant routing is not supported by the 'anthropic' provider")
        if self._client is None:
            raise RuntimeError("'anthropic' provider not started")

        response = await self._client.messages.create(
            model=model or _DEFAULT_MODEL,
            max_tokens=_DEFAULT_MAX_TOKENS,
            system=system_prompt or "",
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout,
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
