# AI Gateway

A small, standalone **FastAPI** service that runs AI agents over plain HTTP —
on **GitHub Copilot, OpenAI, or Anthropic**, picked per request — with no
vendor SDK, API key, or credential living inside the caller. The caller sends
data to analyse; the gateway owns the prompt, picks the model, and talks to
whichever backend answers it.

> Originally built as part of the
> [HPE-COM-Event-Integrations](https://github.com/jullienl/HPE-COM-Event-Integrations)
> project (an HPE Compute Ops Management event pipeline) and split out here as a
> standalone, domain-neutral service — it has no dependency on that repo and
> can be used from any caller.

## Contents

- [At a glance](#at-a-glance)
- [Endpoints](#endpoints)
- [Providers: choosing a backend](#providers-choosing-a-backend)
- [Agents: the instructions the gateway sends](#agents-the-instructions-the-gateway-sends)
- [Run locally](#run-locally)
- [Security](#security)
- [Auth when containerized](#auth-when-containerized)
- [Run with Docker](#run-with-docker)
- [Multi-tenant (one account per caller)](#multi-tenant-one-account-per-caller)
- [Use it as the COM event analyzer](#use-it-as-the-com-event-analyzer)
- [No GitHub Copilot license? Bring your own model](#no-github-copilot-license-bring-your-own-model)
- [n8n integration](#n8n-integration)

## At a glance

It is two layers:

- **A gateway** in front of three model backends — **GitHub Copilot, OpenAI,
  Anthropic** — one endpoint per agent, locked-down sessions where the backend
  supports them, optional per-caller identity. The backend is chosen **per
  request**, not baked into the deployment; see
  [Providers](#providers-choosing-a-backend).
- **A prompt library.** An *agent* is a name + a model + a **system prompt**, and
  the gateway owns that prompt: callers send data, not instructions. Four agents
  ship, tuned for **HPE Compute Ops Management** events, `com-triage`,
  `com-rca`, `com-remediation`, `com-summary`. Retune them, delete them, or add
  your own in [agents.py](agents.py); see
  [Agents](#agents-the-instructions-the-gateway-sends).

So it works out of the box for COM events, and as a plain AI front door for
anything else, on whichever backend you already have — a Copilot seat, an
OpenAI or Anthropic key, or several at once.

```
caller  (an HTTP consumer, make, n8n, a script, …)
   │
   │  POST /agent/com-rca   { "input": { …event data… } }
   │  └─ the data to analyse (the caller writes no INSTRUCTIONS)
   ▼
AI Gateway  ← this service
   │  1. looks up the agent named in the URL              → agents.py
   │  2. BUILDS THE PROMPT: that agent's system prompt = the instructions
   │                       + your `input`, serialized = the user message
   │  3. picks the model AND backend (the agent's, or `model` in the body —
   │                       e.g. "gpt-5.6-sol" → copilot, "openai:gpt-4.1" → openai)
   │  4. calls that backend: a locked-down session with no tools (copilot),
   │                       or a single stateless call (openai / anthropic)
   ▼
Copilot  ·  OpenAI  ·  Anthropic   ← whichever backend was picked runs the model
   │
   │  the model's answer, parsed back into JSON
   ▼
{ "result": { "summary": …, "likely_root_cause": …, … } }   → returned to the caller
```

So the **instructions live in the gateway, not in the caller**: the caller sends
the data to analyse, and [agents.py](agents.py) supplies the system prompt that
tells the model what to do with it. Retune a prompt or switch models there and
every caller picks it up without changing.

`/chat` is the exception, it takes your `prompt` verbatim and applies no system
prompt at all, so there the caller does write the whole thing. Use `/agent/{name}`
when you want the prompt owned by the gateway.

Anything that can make an HTTP request can use it. Two callers are documented
here:

| Caller | What it sends | Start here |
| --- | --- | --- |
| **COM event shim / bridge** (in [HPE-COM-Event-Integrations](https://github.com/jullienl/HPE-COM-Event-Integrations)) | A normalized Compute Ops Management event + a Redfish evidence bundle read from the server's iLO, so the analysis lands inside the ticket it creates | [Use it as the COM event analyzer](#use-it-as-the-com-event-analyzer) |
| **n8n** | Whatever a workflow assembles | [n8n integration](#n8n-integration) |

**Requirements:** one of — a GitHub account with Copilot enabled, an OpenAI API
key, or an Anthropic API key; any single one is enough to start. Python 3.11+
or Docker either way. Nothing else, the gateway has no database and no cloud
dependency. See [Providers](#providers-choosing-a-backend) for how the backend
is picked per request, or
[Bring your own model](#no-github-copilot-license-bring-your-own-model) for
any other vendor — a caller only speaks a small HTTP contract, and this
gateway is one implementation of it, not a requirement.

> Design notes, credential facts, and governance guidance are in
> [GUIDE.md](GUIDE.md).

## Endpoints

| Method | Path              | Body                              | Purpose |
| ------ | ----------------- | --------------------------------- | ------- |
| GET    | `/health`         | —                                 | Liveness + which providers are started. |
| GET    | `/models`         | —                                 | Models available to your Copilot subscription. **Copilot only**, see below; pass `?provider=openai` etc. for a hint instead. |
| GET    | `/agents`         | —                                 | The configured agent profiles, including which provider each uses. |
| POST   | `/chat`           | `{ "prompt", "model?", "session_id?", "tenant?" }` | Generic completion. |
| POST   | `/agent/{name}`   | `{ "input", "model?", "session_id?", "tenant?" }`  | Run a named agent (system prompt baked in). |

> **`model` picks the provider too:** prefix it `<provider>:<model>`, e.g.
> `"model": "openai:gpt-4.1"`. No prefix defaults to `copilot`, so every
> existing call keeps working unchanged. See
> [Providers](#providers-choosing-a-backend).

> **`/models` returns `501` in tenant-only mode, by design.** The SDK's
> `list_models()` is a *client-level* call: it takes no token and runs under the
> runtime's global identity. A multi-tenant deployment authenticates
> *per session* (`create_session(github_token=...)`) and sets no
> `COPILOT_GITHUB_TOKEN`, so there is no global identity for it to use and the
> SDK answers `Not authenticated`. Inference is unaffected, `/chat` and
> `/agent/*` pass the tenant's PAT. Set `COPILOT_GITHUB_TOKEN` (single-token
> mode) if you need model listing.

`session_id` is any readable string: the gateway namespaces it per tenant and
hashes it into a deterministic **UUIDv5**, because the Copilot runtime only
accepts a UUID and rejects anything else at `session.create`. **The OpenAI and
Anthropic providers ignore it** — see
[Providers](#providers-choosing-a-backend).

## Providers: choosing a backend

Despite the name, this gateway is not Copilot-only. Every model id may be
prefixed `<provider>:`:

```json
{ "model": "copilot:auto" }        // default, same as omitting the prefix
{ "model": "openai:gpt-4.1" }
{ "model": "anthropic:claude-sonnet-4-5" }
```

No prefix defaults to `copilot`, so every call and every shipped agent in
[agents.py](agents.py) — none of which specify a provider — keeps working
exactly as before.

| Provider | Auth | Session/`session_id` | Multi-tenant `tenant` |
| --- | --- | --- | --- |
| `copilot` (default) | `COPILOT_GITHUB_TOKEN(_FILE)`, or ambient `gh` login locally | Persistent, server-side (see above) | ✅ supported |
| `openai` | `OPENAI_API_KEY(_FILE)` | Accepted, **ignored** — every call is a single system+user turn | ❌ rejected (`400`) |
| `anthropic` | `ANTHROPIC_API_KEY(_FILE)` | Accepted, **ignored** — same as `openai` | ❌ rejected (`400`) |

The stateless behavior is not a limitation for the shipped analyzer agents
(`com-rca` and friends): each call is one COM event in, one structured result
out, nothing relies on multi-turn resume. It only matters if you use `/chat`
for an ongoing conversation, in which case stick to the `copilot` provider or
resend history yourself.

`OPENAI_BASE_URL` (optional) repoints the `openai` provider at any
OpenAI-compatible endpoint instead of `api.openai.com` — Azure OpenAI's
compatible mode, a self-hosted Ollama/vLLM server, or another
OpenAI-API-shaped service — with no other change.

A provider starts **lazily on first use** (except `copilot`, started eagerly at
boot to preserve `/health`'s existing behavior), so a deployment that never
sends `openai:...`/`anthropic:...` never needs that vendor's key set. An
unknown provider name is a `400`; a configured-but-unreachable one (missing key)
is a `503` with the reason, on the request that first tries it.

Pin an agent to a specific provider in [agents.py](agents.py):

```python
AGENTS["com-rca-openai"] = AgentProfile(
    name="com-rca-openai",
    provider="openai",
    model="gpt-4.1",
    description="Same com-rca prompt, run on OpenAI instead of Copilot.",
    system_prompt=AGENTS["com-rca"].system_prompt,  # reuse an existing entry's prompt
)
```

> Add it as a statement **after** the `AGENTS = {...}` dict literal, not as a
> key inside it — a dict literal can't reference its own keys while still being
> built. Copying the prompt text inline works from anywhere, including inside
> the literal.

## Agents: the instructions the gateway sends

An **agent** is a name, a model, and a **system prompt**. All three live in
[agents.py](agents.py), which is the only place instructions are written:

```python
"com-rca": AgentProfile(
    name="com-rca",
    model="gpt-5.6-sol",       # any id from GET /models, or "auto"
    description="Root-cause analysis of a COM event, optionally with iLO data.",
    system_prompt="You are an HPE Compute Ops Management incident analysis …",
),
```

Four ship with the gateway. They are **worked examples** tuned for infrastructure
events, not fixed behaviour:

| Agent | Default model | What its prompt tells the model to return |
| --- | --- | --- |
| `com-triage` | `claude-sonnet-5` | `category`, `severity`, `needs_rca`, `one_line` |
| `com-rca` | `gpt-5.6-sol` | `summary`, `likely_root_cause`, `evidence`, `confidence` (0–1), `recommended_actions` |
| `com-remediation` | `claude-opus-5` | `steps` (`action`/`rationale`/`risk`), `requires_downtime`, `rollback` |
| `com-summary` | `auto` | A few sentences of plain prose for Slack/Teams, no JSON |

`GET /agents` returns whatever is currently configured, so the running service
always tells you the truth about itself.

### Yes, you can change them

Edit `AGENTS` in [agents.py](agents.py) to retune a prompt, point an agent at a
different model, delete the ones you don't need, or add your own. Nothing else in
the gateway depends on these particular names. Restart the service afterwards,
the registry is read once at import.

Two things are worth knowing before you do:

- **Callers cannot override the instructions.** A request may override `model`,
  but there is no way to pass a system prompt to `/agent/{name}`, that is the
  point of the split. If you want a caller to supply the whole prompt, use
  `/chat`, which forwards `prompt` verbatim and applies no system prompt at all.
- **Ask for JSON if you want structured output.** The gateway strips a
  ```` ```json ```` fence and parses the text, returning a real object in
  `result`; if it isn't JSON, you get the raw string instead. Nothing validates
  the shape, so the fields you name in the prompt *are* the contract.

### The one prompt you should not rename fields in

If you use the gateway as the COM event analyzer, the shim or bridge in
[HPE-COM-Event-Integrations](https://github.com/jullienl/HPE-COM-Event-Integrations)
reads exactly four fields out of `com-rca`'s response:

`summary` · `likely_root_cause` · `confidence` · `recommended_actions`

Rewording the prompt is fine, and extra fields are ignored, `evidence` is
already one that is never read. But **renaming** any of those four makes the
analysis vanish from tickets silently: the call still succeeds, the enricher just
finds nothing to attach. If you change them, change
[ilo_ai.py](https://github.com/jullienl/HPE-COM-Event-Integrations/blob/main/com-event-core/com_event_core/enrich/ilo_ai.py)
to match, or point `AI_AGENT` at an agent of your own instead of editing
`com-rca`.

## Run locally

The default provider is `copilot`, and this section covers that path since it
needs the most setup. Using OpenAI or Anthropic instead is simpler — no
session/login mechanics, just `pip install -r requirements.txt`,
`export OPENAI_API_KEY=...` (or `ANTHROPIC_API_KEY`), `uvicorn app:app`, and
`"model": "openai:gpt-4.1"` on the request; see
[Providers](#providers-choosing-a-backend).

For `copilot`, nothing needs configuring locally: the SDK reuses your existing
`gh` / Copilot CLI login on this machine. If you have several `gh` accounts,
whichever one is **active** (`gh auth status`) is the identity the gateway runs
under, `gh auth switch --hostname github.com --user <account>` changes it
(restart the gateway process afterwards; it caches the Copilot client at
startup).

```powershell
git clone https://github.com/jullienl/ai-gateway.git
cd ai-gateway

# in a virtual environment of your choice
pip install -r requirements.txt

uvicorn app:app --host 127.0.0.1 --port 8000
```

### Pinning a specific PAT locally instead of the ambient `gh` login

Optional: only needed if you want the gateway tied to one account regardless
of which `gh` account happens to be active. Same variables as
[containerized auth](#auth-when-containerized), just set before you launch
`uvicorn` instead of passed to `docker run`:

```powershell
# option A: token straight in the environment (dev convenience; visible to
# child processes and `Get-ChildItem Env:` for this session)
$env:COPILOT_GITHUB_TOKEN = "github_pat_xxxxxxxx"
uvicorn app:app --host 127.0.0.1 --port 8000

# option B: token from a file (safer: nothing sensitive in the environment)
"github_pat_xxxxxxxx" | Out-File -Encoding ascii -NoNewline .\copilot-pat.txt
$env:COPILOT_GITHUB_TOKEN_FILE = (Resolve-Path .\copilot-pat.txt)
uvicorn app:app --host 127.0.0.1 --port 8000
```

```bash
# bash equivalent
export COPILOT_GITHUB_TOKEN_FILE=./copilot-pat.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

With neither variable set (the default), the line above about the ambient `gh`
login applies, that's the "not set" case, and it's what the smoke test below
relies on.

### Smoke test

```powershell
# health
Invoke-RestMethod http://127.0.0.1:8000/health

# list models your subscription can use
# (works locally because your `gh` login is the runtime's global identity;
#  a tenant-only container deployment returns 501 instead)
Invoke-RestMethod http://127.0.0.1:8000/models | ConvertTo-Json -Depth 5

# generic chat
Invoke-RestMethod -Uri http://127.0.0.1:8000/chat -Method POST `
  -ContentType "application/json" `
  -Body '{"model":"auto","prompt":"Reply with: Hello n8n"}'

# run the RCA agent over an event payload
Invoke-RestMethod -Uri http://127.0.0.1:8000/agent/com-rca -Method POST `
  -ContentType "application/json" `
  -Body '{"input":{"event":"Power supply 2 failed","serial":"CZ1234"}}'
```

## Security

- **The gateway does not authenticate its own callers.** There is no API key or
  token check on any endpoint. Run it on a private network, on internal-only
  ingress, or behind a reverse proxy that authenticates, anyone who can reach it
  can spend the configured provider's quota. Do not expose it to the internet.
- Sessions are created with **no tools available** and a **deny-all permission
  handler**, so the model can only return text. The gateway is inference + model
  selection only, never a filesystem/shell/git command executor. Do **not**
  reintroduce `PermissionHandler.approve_all`.
- Prompts and responses go to whichever backend answered the call — Copilot,
  OpenAI, or Anthropic — under the configured account or key. Whatever a
  caller sends leaves your network, so don't send data you are not willing to
  share with that model provider.

## Auth when containerized

This section, like Run locally above, covers the `copilot` provider — it's the
one with session/identity mechanics. For `openai`/`anthropic`, containerized
auth is just `OPENAI_API_KEY_FILE` / `ANTHROPIC_API_KEY_FILE` (or the
non-file env var) set on the container; see [Providers](#providers-choosing-a-backend).

GitHub Copilot has **no shared service token**, it is licensed per named user.
For a headless/containerized deployment, provide a **fine-grained PAT** from an
account with Copilot enabled as:

- `COPILOT_GITHUB_TOKEN_FILE`: path to a mounted secret file (preferred), or
- `COPILOT_GITHUB_TOKEN`: the token directly (dev fallback).

The gateway reads it **file-first** so it doesn't leak via `docker inspect` /
`/proc/<pid>/environ`.

Every request then runs under that one account's identity and quota. If several
people or services share the gateway, see multi-tenant mode below.

## Run with Docker

```bash
docker build -t ai-gateway:1.0.1 .

docker run -d --name ai-gateway -p 8000:8000 \
  -v /run/secrets/copilot_pat:/run/secrets/copilot_pat:ro \
  -e COPILOT_GITHUB_TOKEN_FILE=/run/secrets/copilot_pat \
  ai-gateway:1.0.1
```

Vault the PAT on the host first, never bake it into the image or pass it as a
plain `-e COPILOT_GITHUB_TOKEN=...` where you can avoid it (that's the
dev-fallback path, visible to `docker inspect`):

```bash
sudo mkdir -p /run/secrets
sudo sh -c 'umask 077; read -rs PAT && printf "%s" "$PAT" > /run/secrets/copilot_pat'
sudo chown root:root /run/secrets/copilot_pat   # paste the PAT, press Enter
```

`umask 077` plus the trailing `chown` keep the file at `600 root:root`; the
container reads it read-only via the bind mount above and it never touches the
image, `docker inspect`, or shell history.

Or with Docker Compose, whose native `secrets:` block does the same file-mount
for you:

```yaml
# docker-compose.yml
services:
  ai-gateway:
    build: .
    image: ai-gateway:1.0.1
    container_name: ai-gateway
    ports:
      - "8000:8000"
    environment:
      COPILOT_GITHUB_TOKEN_FILE: /run/secrets/copilot_pat
    secrets:
      - copilot_pat
secrets:
  copilot_pat:
    file: ./copilot-pat.txt   # 600, gitignored, or external: true for a
                              # secret already provisioned in Swarm/CI
```

```bash
docker compose up -d --build
```

To let another container reach it by name, join both to a shared network
(add a top-level `networks:` block referencing it, `external: true` if it
already exists) — see that caller's own docs for the network name it expects.

On a network that inspects TLS, the gateway also needs your corporate CA
bundle, it makes its own outbound HTTPS call to the model API and will
otherwise fail with a certificate error. The bundle must contain the internal
CA **plus** the public roots, `SSL_CERT_FILE` *replaces* the trust store
rather than adding to it, so a file holding only the corporate CA breaks every
other outbound call:

```bash
  -v /etc/ssl/certs/corp-ca.pem:/etc/ssl/certs/corp-ca.pem:ro \
  -e SSL_CERT_FILE=/etc/ssl/certs/corp-ca.pem \
  -e NODE_EXTRA_CA_CERTS=/etc/ssl/certs/corp-ca.pem \
```

### Azure Container Apps

See [GUIDE.md §10](GUIDE.md#10-deploying-to-azure-container-apps-primary-path).
Use **internal** ingress; a caller in the same environment then reaches it by
app name (`http://ai-gateway`).

### Multi-tenant (one account per caller)

The same image also runs multi-tenant: send a `tenant` on each request and map
each tenant to its **own** PAT via `COPILOT_TENANT_TOKENS_DIR` (one file per
tenant) or `COPILOT_TENANT_TOKENS_FILE` (a `{tenant: pat}` JSON map). Set
`COPILOT_REQUIRE_TENANT=1` to reject tenant-less requests.

This isolates **quota, rate limits and attribution** per tenant, useful when
several teams, several services, or a classroom of workstations share one
gateway, and necessary because `session_id` alone does *not* separate identity.
See [GUIDE.md §8](GUIDE.md#8-multi-tenant-one-account-per-caller).

Multi-tenant mode is entirely opt-in: with none of those variables set, the
single `COPILOT_GITHUB_TOKEN` is used and `tenant` can be omitted.

## Use it as the COM event analyzer

This is the service behind `ENRICHERS=ilo_ai` in the COM event **shim** and
**bridge** from
[HPE-COM-Event-Integrations](https://github.com/jullienl/HPE-COM-Event-Integrations),
the two on-prem consumers, which behave identically here. A COM event arrives,
the consumer reads Redfish evidence from that server's iLO, posts both here,
and puts the analysis in the ticket or chat message it creates.

The full walkthrough — getting a Copilot PAT, running the gateway next to the
shim or bridge, and the environment variables that connect them — lives in
that repo, not here, since it's specific to that pipeline:
[Set up the analyzer](https://github.com/jullienl/HPE-COM-Event-Integrations/blob/main/com-event-core/README.md#set-up-the-analyzer).
Generic container instructions are above, under
[Run with Docker](#run-with-docker).

The prompt it relies on is `com-rca`, see
[The one prompt you should not rename fields in](#the-one-prompt-you-should-not-rename-fields-in)
if you retune it.

## No GitHub Copilot license? Bring your own model

**OpenAI and Anthropic are now built in** — see
[Providers](#providers-choosing-a-backend) above, set `OPENAI_API_KEY` or
`ANTHROPIC_API_KEY`, and use `"model": "openai:..."` / `"anthropic:..."`. No
separate service needed for those two.

For any other vendor (Gemini, Bedrock, a fully custom pipeline) — or if you'd
rather run a completely independent service instead of adding a provider here —
a caller doesn't know or care which one answers. `AI_ANALYZER_URL` (for the
shim/bridge case) just needs to point at any HTTP service that implements
[the analyzer contract](https://github.com/jullienl/HPE-COM-Event-Integrations/blob/main/com-event-core/README.md#the-analyzer-contract):
POST `/agent/<name>` with `{ "input": {...}, "session_id": ... }`, answer `200`
with `{ "result": { "summary", "likely_root_cause", "confidence",
"recommended_actions" } }`. Nothing about the model, vendor, or SDK is checked —
only those field names.

A minimal FastAPI analyzer that reuses the com-rca prompt from
[agents.py](agents.py) verbatim, calling OpenAI directly (illustrative — you'd
normally just use the built-in `openai` provider for this exact case instead):

```python
# analyzer.py -- minimal bring-your-own-model analyzer
import json
import os

from fastapi import FastAPI
from openai import OpenAI
from pydantic import BaseModel

app = FastAPI()
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])  # or any other SDK/provider

SYSTEM_PROMPT = (  # copied verbatim from agents.py's "com-rca" agent
    "You are an HPE Compute Ops Management incident analysis agent.\n"
    "Analyze COM events together with any provided iLO telemetry and logs.\n"
    "Return a structured analysis with these fields:\n"
    "- summary: incident summary\n"
    "- likely_root_cause: the most probable root cause\n"
    "- evidence: the specific signals in the input that support it\n"
    "- confidence: a number between 0 and 1\n"
    "- recommended_actions: an ordered list of concrete remediation steps\n"
    "Base every conclusion on the supplied data; if data is missing, say so."
)


class AgentRequest(BaseModel):
    input: dict
    session_id: str | None = None


@app.post("/agent/{name}")
def run_agent(name: str, body: AgentRequest):
    response = client.chat.completions.create(
        model="gpt-4.1",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(body.input)},
        ],
    )
    return {"result": json.loads(response.choices[0].message.content)}
```

Point the shim or bridge at it exactly as documented above — nothing in
com-event-core changes:

```bash
AI_ANALYZER_URL=http://your-analyzer:8000
AI_AGENT=com-rca            # any name your service recognises; it's just a URL segment
```

`AI_ANALYZER_TOKEN`, if set, is sent as `Authorization: Bearer <token>` on every
request — that's how the shim/bridge authenticates *itself* to your service;
whether to check it is entirely up to your implementation (`com_event_core`
never calls the analyzer the other way around).

This repo does not ship that alternate service, only the Copilot-backed gateway
documented above, but the contract was kept deliberately small and
provider-agnostic for exactly this case: parse `input.event` and
`input.redfish`, return the four fields, done.

## n8n integration

Point an **HTTP Request** node at `POST http://<gateway-host>:8000/agent/com-rca`
with a JSON body like:

```json
{
  "input": {
    "event": "...",
    "ilo_logs": "...",
    "server_info": "..."
  }
}
```

and use `result` from the response as the agent output.
