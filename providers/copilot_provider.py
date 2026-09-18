"""GitHub Copilot provider -- the gateway's default and original backend.

This is the only provider with per-tenant PAT resolution and persistent,
server-side sessions, both of which are Copilot-runtime features with no
equivalent in the OpenAI/Anthropic providers. Bare model ids with no
`<provider>:` prefix route here (see providers/__init__.py).
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from copilot import CopilotClient

from secret_resolution import get_secret

# A tenant id becomes a filename under COPILOT_TENANT_TOKENS_DIR, so it must not
# allow path traversal -- restrict to a safe character set.
_TENANT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Fixed namespace for hashing a human-readable session key into a UUID. It must
# never change: a different namespace yields different UUIDs, which would
# orphan every stored conversation.
_SESSION_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _tenant_token(tenant: str) -> str:
    """Resolve the fine-grained PAT for one tenant.

    Sources, in order:
      COPILOT_TENANT_TOKENS_DIR/<tenant>  one file per tenant (Key Vault / Docker /
                                          K8s secret friendly -- each PAT is its
                                          own mounted file).
      COPILOT_TENANT_TOKENS_FILE          a JSON map {tenant: pat} (single secret).

    Raises ValueError for an unsafe tenant id, KeyError if the tenant has no
    configured token.
    """
    import json

    if not _TENANT_RE.match(tenant):
        raise ValueError(f"invalid tenant id '{tenant}' (allowed: A-Z a-z 0-9 _ -)")

    tokens_dir = os.environ.get("COPILOT_TENANT_TOKENS_DIR")
    if tokens_dir:
        path = os.path.join(tokens_dir, tenant)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read().rstrip("\n")

    tokens_file = os.environ.get("COPILOT_TENANT_TOKENS_FILE")
    if tokens_file:
        with open(tokens_file, encoding="utf-8") as handle:
            mapping = json.load(handle)
        if tenant in mapping:
            return str(mapping[tenant])

    raise KeyError(f"no Copilot token configured for tenant '{tenant}'")


def _resolve_token(tenant: str | None) -> str | None:
    """Pick the PAT for this request.

    - tenant given -> that tenant's PAT (per-tenant quota/attribution isolation).
    - no tenant    -> the single default COPILOT_GITHUB_TOKEN.

    COPILOT_REQUIRE_TENANT=1 rejects tenant-less requests so a shared deployment
    can't silently fall back to a shared/personal token.
    """
    require = os.environ.get("COPILOT_REQUIRE_TENANT", "").lower() in ("1", "true", "yes")
    if tenant is None:
        if require:
            raise PermissionError("this gateway requires a 'tenant' on every request")
        return get_secret("COPILOT_GITHUB_TOKEN")
    return _tenant_token(tenant)


def _deny_all_permissions(_request: Any, _invocation: Any) -> Any:
    """Refuse every tool-permission request. Belt-and-suspenders alongside
    passing no tools -- guarantees the model can never execute a tool."""
    from copilot.generated.rpc import PermissionDecisionReject

    return PermissionDecisionReject(feedback="tools disabled on this gateway")


def _extract_text(event: Any) -> str:
    """Pull the assistant text out of a send_and_wait result, defensively."""
    if event is None:
        return ""
    data = getattr(event, "data", None)
    content = getattr(data, "content", None)
    if isinstance(content, str):
        return content
    if content is not None:
        return str(content)
    return str(event)


class CopilotProvider:
    """Wraps the GitHub Copilot SDK behind the ChatProvider interface."""

    name = "copilot"

    def __init__(self) -> None:
        self._client: CopilotClient | None = None

    @property
    def ready(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        client = CopilotClient()
        await client.start()
        self._client = client

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.stop()
        self._client = None

    async def list_models(self) -> list[Any]:
        if self._client is None:
            raise RuntimeError("Copilot runtime not ready")
        return await self._client.list_models()

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
        """Create a locked-down session, send one prompt, return the text."""
        if self._client is None:
            raise RuntimeError("Copilot runtime not ready")

        # Resolve the identity first so tenant errors surface before we touch
        # the SDK.
        token = _resolve_token(tenant)

        # Namespace the session per tenant so two tenants reusing the same
        # simple session_id (e.g. "incident1") never share a conversation. A
        # persistent session is tied to the bot identity that created it;
        # tenant->bot is stable, so the same effective id always resolves to
        # the same bot.
        effective_session_id = session_id
        if session_id and tenant:
            effective_session_id = f"{tenant}:{session_id}"

        # The Copilot runtime REQUIRES sessionId to be a UUID -- it rejects
        # anything else with "session.create failed ... invalid sessionId"
        # (the SDK itself defaults to str(uuid.uuid4()) when none is given).
        # Our ids are meaningful strings, so hash them into a deterministic
        # UUIDv5: same logical id -> same UUID on every request, which is what
        # makes resume work, while staying a valid UUID. Do NOT pass the raw
        # string through.
        if effective_session_id:
            effective_session_id = str(uuid.uuid5(_SESSION_NAMESPACE, effective_session_id))

        kwargs: dict[str, Any] = {
            "model": model,
            "on_permission_request": _deny_all_permissions,
            "available_tools": [],  # no tools -> pure inference
        }
        if effective_session_id:
            kwargs["session_id"] = effective_session_id
        if system_prompt:
            kwargs["system_message"] = {"mode": "replace", "content": system_prompt}
        if token:
            kwargs["github_token"] = token

        session = await self._client.create_session(**kwargs)
        try:
            result = await session.send_and_wait(prompt, timeout=timeout)
            return _extract_text(result)
        finally:
            # Leave persistent sessions in the SDK store for resume; only tear
            # down ephemeral ones.
            if not effective_session_id:
                await session.disconnect()
