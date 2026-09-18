"""Provider abstraction: the interface every model backend implements.

A "provider" is a chat backend -- GitHub Copilot, OpenAI, Anthropic, or another
one you add. `app.py` only ever talks to this interface, never to a vendor SDK
directly, so adding a provider never requires touching the endpoints.
"""

from __future__ import annotations

from typing import Any, Protocol


class ChatProvider(Protocol):
    """Minimal interface app.py needs from any model backend."""

    name: str

    async def start(self) -> None:
        """Prepare the backend for use (open a client, start a runtime, ...).

        Called once per provider: eagerly at process boot for the default
        provider, lazily on first use for every other one.
        """
        ...

    async def stop(self) -> None:
        """Release whatever `start()` acquired. Called once at shutdown."""
        ...

    async def run(
        self,
        *,
        model: str,
        prompt: str,
        system_prompt: str | None,
        session_id: str | None,
        tenant: str | None,
        timeout: float,
    ) -> str:
        """Send one prompt (+ optional system prompt), return the model's text.

        `session_id` and `tenant` are accepted for interface parity with the
        Copilot provider; a provider that can't support one of them (no
        server-side session store, no per-tenant identity) should raise
        `ValueError` when it is supplied rather than silently ignoring it.
        """
        ...


__all__: list[str] = ["ChatProvider"]
