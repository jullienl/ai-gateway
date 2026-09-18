"""OpenAI provider -- stateless Chat Completions backend.

Auth: OPENAI_API_KEY (or OPENAI_API_KEY_FILE), file-first like every other
secret in this gateway. Optional OPENAI_BASE_URL points the same provider at
any OpenAI-compatible endpoint instead (Azure OpenAI's compatible mode,
a self-hosted Ollama/vLLM server, or another OpenAI-API-shaped service) --
nothing else about this provider changes.

No session persistence: `session_id` is accepted for interface parity with the
Copilot provider but is a no-op here, because the Chat Completions API is
stateless -- there is no server-side conversation to resume. Every call is a
single system+user turn, which is exactly what the shipped analyzer-style
agents (e.g. com-rca) need: one event in, one structured result out.

No multi-tenant support: `tenant` raises ValueError if supplied. This provider
authenticates with one shared API key; per-caller identity/quota isolation
(what `tenant` gives you on the Copilot provider) has no equivalent here.
"""

from __future__ import annotations

from typing import Any

from secret_resolution import get_secret

_DEFAULT_MODEL = "gpt-4.1"


class OpenAIProvider:
    """Wraps the OpenAI SDK's async Chat Completions client."""

    name = "openai"

    def __init__(self) -> None:
        self._client: Any | None = None

    async def start(self) -> None:
        # Imported lazily: only a deployment that actually selects this
        # provider needs the `openai` package installed/importable.
        from openai import AsyncOpenAI

        api_key = get_secret("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY (or OPENAI_API_KEY_FILE) is not set; cannot "
                "start the 'openai' provider"
            )
        kwargs: dict[str, Any] = {"api_key": api_key}
        base_url = get_secret("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)

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
            raise ValueError("tenant routing is not supported by the 'openai' provider")
        if self._client is None:
            raise RuntimeError("'openai' provider not started")

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = await self._client.chat.completions.create(
            model=model or _DEFAULT_MODEL,
            messages=messages,
            timeout=timeout,
        )
        return response.choices[0].message.content or ""
