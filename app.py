"""AI Gateway.

A small FastAPI service that sits between a caller (an orchestrator such as n8n,
a script, or another service) and one or more model backends. It exposes a
model subscription as plain HTTP so callers never touch vendor credentials.

Endpoints
---------
GET  /health          -> liveness + which providers are started
GET  /models          -> models available (Copilot only; see providers/)
POST /chat            -> {model?, prompt, session_id?} generic completion
POST /agent/{name}    -> run a named agent profile (see agents.py) over `input`

Providers
---------
Despite the name, this gateway is not Copilot-only: `model` may be prefixed
`<provider>:<model>` (e.g. `openai:gpt-4.1`, `anthropic:claude-sonnet-4-5`). No
prefix uses the configured default provider (`copilot` unless `AI_PROVIDER` is
set), so every existing caller keeps working unchanged. See providers/__init__.py for the
registry and providers/base.py for the interface a new provider implements.

Auth
----
Copilot, local dev: nothing to configure -- the SDK reuses the existing `gh` /
Copilot CLI login on this machine.

Copilot, containerized/headless: set COPILOT_GITHUB_TOKEN (or
COPILOT_GITHUB_TOKEN_FILE pointing at a mounted secret file). GitHub Copilot has
no shared service token -- this must be a per-user fine-grained PAT with
Copilot enabled. The token is read file-first so it can come from a Docker/K8s
secret without leaking via `docker inspect` or the process environment.

Copilot, multi-tenant (several callers, one account each): pass a `tenant` on
each request and configure per-tenant PATs via COPILOT_TENANT_TOKENS_DIR (one
file per tenant, named after it) or COPILOT_TENANT_TOKENS_FILE (a JSON map
{tenant: pat}). Each request then runs under that tenant's own identity, so
quota, rate limits and attribution are isolated. Set COPILOT_REQUIRE_TENANT=1 to
reject any tenant-less request, so a misconfigured caller can't silently fall
back to a shared token. With no tenant, the single COPILOT_GITHUB_TOKEN is used
-- so the SAME image serves both single-token and multi-tenant use.

OpenAI / Anthropic: set OPENAI_API_KEY(_FILE) / ANTHROPIC_API_KEY(_FILE). Both
are single-key, no multi-tenant routing (`tenant` is rejected). Set
AI_PROVIDER=openai with OPENAI_BASE_URL for an OpenAI-compatible on-prem model.
See providers/openai_provider.py and providers/anthropic_provider.py.

Security
--------
Sessions are created with NO tools available and a deny-all permission handler
(Copilot provider), or with no tools parameter at all (OpenAI/Anthropic
providers, which have no tool access unless one is explicitly requested), so
the model can only produce text -- it cannot touch the filesystem, shell, or
git. This gateway is inference + model selection only, never a command executor.

The AI gateway does NOT authenticate its own callers. Run it on a private network
or behind a reverse proxy that does -- anyone who can reach it can spend the
configured quota on every configured provider.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from agents import AGENTS, get_agent
from providers import create_provider, default_provider, parse_model_id

log = logging.getLogger("ai-gateway")
logging.basicConfig(level=logging.INFO)

# Caller-facing error details are truncated to this length: they're meant to be
# actionable (SDK/provider error messages), not an unbounded internal dump. The
# full exception is still logged server-side at its natural length.
_MAX_ERROR_DETAIL = 500

# No caller authentication (see the module docstring), so this is the one
# built-in guard against a single request driving up compute/token cost. Real
# payloads (a COM event + bounded Redfish evidence) run well under 1 MB.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(1 * 1024 * 1024)))


def _truncated(exc: Exception) -> str:
    log.warning("request failed: %s", exc)
    return str(exc)[:_MAX_ERROR_DETAIL]


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject an oversized request before it reaches a route handler.

    Checked from the Content-Length header only (no buffering the body), so a
    caller can't force the AI gateway to read an unbounded payload into memory
    just to reject it.
    """

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
            return Response(status_code=413, content="payload too large")
        return await call_next(request)

# --------------------------------------------------------------------------- #
# Provider lifecycle (one started instance per provider actually used)
# --------------------------------------------------------------------------- #


class _State:
    def __init__(self) -> None:
        self.providers: dict[str, Any] = {}


state = _State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start the configured default provider once on boot.

    Other providers start lazily on first use. ``AI_PROVIDER`` defaults to
    Copilot for backward compatibility, but can select ``openai`` for an
    OpenAI-compatible on-prem model deployment.
    """
    provider_name = default_provider()
    provider = create_provider(provider_name)
    await provider.start()
    state.providers[provider_name] = provider
    try:
        yield
    finally:
        for provider in state.providers.values():
            await provider.stop()
        state.providers.clear()


app = FastAPI(
    title="AI Gateway",
    version="1.0.1",
    summary="HTTP front door to the GitHub Copilot SDK (and friends) for n8n and friends.",
    lifespan=lifespan,
)
app.add_middleware(BodySizeLimitMiddleware)


async def _get_provider(name: str) -> Any:
    """Return a started provider instance, starting it on first use.

    Unknown provider name -> 400. Provider fails to start (e.g. missing API
    key) -> 503 with the reason, so a misconfiguration reads as "not available"
    rather than a bare 500.
    """
    provider = state.providers.get(name)
    if provider is not None:
        return provider

    try:
        provider = create_provider(name)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        await provider.start()
    except Exception as exc:  # noqa: BLE001 - surfaced with detail below
        raise HTTPException(
            status_code=503,
            detail=f"provider '{name}' is not available: {exc}",
        ) from exc

    state.providers[name] = provider
    return provider


async def _run(
    *,
    model: str,
    prompt: str,
    system_prompt: str | None = None,
    session_id: str | None = None,
    tenant: str | None = None,
    timeout: float = 120.0,
) -> str:
    """Route to the provider encoded in `model` (default: copilot), run one prompt."""
    provider_name, bare_model = parse_model_id(model)
    provider = await _get_provider(provider_name)
    return await provider.run(
        model=bare_model,
        prompt=prompt,
        system_prompt=system_prompt,
        session_id=session_id,
        tenant=tenant,
        timeout=timeout,
    )


def _maybe_json(text: str) -> Any:
    """Return parsed JSON if the text is a JSON object/array, else the raw text.

    Handles models that wrap JSON in a ```json ... ``` markdown code fence.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the closing fence.
        inner = stripped[3:]
        newline = inner.find("\n")
        if newline != -1:
            inner = inner[newline + 1 :]
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3]
        stripped = inner.strip()
    if stripped[:1] in "{[":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return text
    return text


# --------------------------------------------------------------------------- #
# Request/response models
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    prompt: str = Field(
        ..., max_length=200_000, description="The user prompt to send to the model."
    )
    model: str = Field("auto", description="Model id (see GET /models).")
    session_id: str | None = Field(
        None, max_length=200, description="Optional id to persist/resume a conversation."
    )
    tenant: str | None = Field(
        None,
        max_length=100,
        description=(
            "Optional tenant id. Routes the request to that tenant's own Copilot "
            "PAT for quota/attribution isolation. Omit to use the single default "
            "token."
        ),
    )


class ChatResponse(BaseModel):
    model: str
    response: str


class AgentRequest(BaseModel):
    input: Any = Field(..., description="Arbitrary payload the agent should analyze.")
    model: str | None = Field(
        None, description="Optional model override; defaults to the agent's model."
    )
    session_id: str | None = Field(
        None, max_length=200, description="Optional id to persist/resume a conversation."
    )
    tenant: str | None = Field(
        None,
        max_length=100,
        description=(
            "Optional tenant id. Routes the request to that tenant's own Copilot "
            "PAT for quota/attribution isolation. Omit to use the single default "
            "token."
        ),
    )


class AgentResponse(BaseModel):
    agent: str
    model: str
    result: Any


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


def _tenant_http_error(exc: Exception) -> HTTPException:
    """Map tenant-resolution failures to the right HTTP status.

    ValueError       -> 400 (unsafe/invalid tenant id)
    PermissionError  -> 400 (tenant required but missing)
    KeyError         -> 404 (unknown tenant / no configured token)

    Scope: every current provider's `run()` only ever raises these three types
    for a tenant/permission reason (see providers/base.py's documented
    contract) -- never for an unrelated business-logic error -- so catching
    them broadly around `_run()` below is safe today. A future provider that
    raises one of these for a different reason must not reuse them for that.
    """
    if isinstance(exc, (ValueError, PermissionError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=404, detail=str(exc))


# Hint model id shown in /models' note for a provider with no listing support.
_MODEL_HINTS = {"openai": "gpt-4.1", "anthropic": "claude-sonnet-4-5"}


@app.get("/health")
async def health() -> dict[str, Any]:
    provider_name = default_provider()
    provider = state.providers.get(provider_name)
    return {
        "status": "ok",
        "runtime_ready": bool(provider is not None and getattr(provider, "ready", True)),
        "providers_started": sorted(state.providers),
    }


@app.get("/models")
async def models(provider: str | None = None) -> dict[str, Any]:
    provider = provider or default_provider()
    if provider != "copilot":
        # No shared "list models" shape exists across vendors; OpenAI/Anthropic
        # model ids come from the vendor's own docs, not this endpoint.
        hint = _MODEL_HINTS.get(provider, "<model>")
        return {
            "provider": provider,
            "models": [],
            "note": (
                f"Model listing isn't implemented for '{provider}'; consult that "
                f"vendor's docs for available model ids, then request one as "
                f"'{provider}:{hint}'."
            ),
        }

    # list_models() is a CLIENT-level RPC (models.list): it takes no token and
    # runs under the runtime's global identity, unlike /agent/* which passes a
    # per-session github_token. A tenant-only deployment (per-tenant PATs, no
    # COPILOT_GITHUB_TOKEN) has no global identity, so this call cannot succeed
    # there -- the SDK returns "Not authenticated. Please authenticate first."
    # Report that as a 501 with the reason instead of a bare 500 stack trace.
    copilot = await _get_provider("copilot")
    try:
        infos = await copilot.list_models()
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
        if "not authenticated" in str(exc).lower():
            raise HTTPException(
                status_code=501,
                detail=(
                    "/models needs a globally authenticated Copilot runtime, but "
                    "this gateway has no COPILOT_GITHUB_TOKEN (tenant-only mode). "
                    "list_models() is client-level and accepts no per-tenant "
                    "token, so model listing is unavailable here. Inference is "
                    "unaffected: /agent/* passes the tenant's PAT per session."
                ),
            ) from exc
        raise
    return {
        "provider": provider,
        "models": [
            {
                "id": getattr(m, "id", None),
                "name": getattr(m, "name", None),
                "policy": getattr(getattr(m, "policy", None), "state", None),
            }
            for m in infos
        ],
    }


@app.get("/agents")
async def list_agents() -> dict[str, Any]:
    return {
        "agents": [
            {
                "name": a.name,
                "provider": a.provider,
                "model": a.model,
                "description": a.description,
            }
            for a in AGENTS.values()
        ]
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    try:
        text = await _run(
            model=req.model,
            prompt=req.prompt,
            session_id=req.session_id,
            tenant=req.tenant,
        )
    except (ValueError, PermissionError, KeyError) as exc:  # tenant/provider routing
        raise _tenant_http_error(exc) from exc
    except HTTPException:  # provider unknown/unavailable -- already the right status
        raise
    except Exception as exc:  # surface SDK/model errors as 502
        raise HTTPException(status_code=502, detail=_truncated(exc)) from exc
    return ChatResponse(model=req.model, response=text)


@app.post("/agent/{agent_name}", response_model=AgentResponse)
async def run_agent(agent_name: str, req: AgentRequest) -> AgentResponse:
    try:
        profile = get_agent(agent_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # A caller override is used verbatim (it must include a `<provider>:`
    # prefix itself for a non-Copilot override); otherwise the agent's own
    # provider + model are combined, so an agent pinned to a non-default
    # provider (see agents.py) routes correctly without the caller knowing.
    model = req.model or f"{profile.provider}:{profile.model}"
    payload = req.input
    prompt = payload if isinstance(payload, str) else json.dumps(payload, indent=2)

    try:
        text = await _run(
            model=model,
            prompt=prompt,
            system_prompt=profile.system_prompt,
            session_id=req.session_id,
            tenant=req.tenant,
        )
    except (ValueError, PermissionError, KeyError) as exc:  # tenant/provider routing
        raise _tenant_http_error(exc) from exc
    except HTTPException:  # provider unknown/unavailable -- already the right status
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_truncated(exc)) from exc

    return AgentResponse(agent=profile.name, model=model, result=_maybe_json(text))
