# AI Gateway — Guide

>A deeper reference for the gateway's behavior, configuration, deployment, and
advanced usage. For the quick start and end-user path, see the
[README](README.md).

## Contents

- [1. What it is](#1-what-it-is)
- [2. Files](#2-files)
- [3. How it works internally](#3-how-it-works-internally)
- [4. Configuration](#4-configuration)
- [5. Endpoints](#5-endpoints)
- [6. Local build & debug loop](#6-local-build--debug-loop)
- [7. Using it from n8n](#7-using-it-from-n8n)
- [8. Multi-tenant (one account per caller)](#8-multi-tenant-one-account-per-caller)
- [9. Troubleshooting](#9-troubleshooting)
- [10. Deploying to Azure Container Apps (primary path)](#10-deploying-to-azure-container-apps-primary-path)

---

## 1. What it is

The AI Gateway is a small **FastAPI** service that fronts three model
backends — **GitHub Copilot, OpenAI, and Anthropic** — chosen **per request**
via a `<provider>:<model>` id (see [README § Providers](README.md#providers-choosing-a-backend)).
It is both a generic transport and a prompt library: the gateway owns the
system prompt, callers send data, and the shipped agents are tuned for **HPE
Compute Ops Management** events as a worked example — the service itself is
domain-neutral and not tied to any one backend.

```
event / request
   │
   ▼
 n8n  ── orchestration: webhooks, conditions, parsing, Slack/Teams/ITSM
   │        assembles the DATA. It writes no instructions and holds no
   │        credential.
   │
   │  HTTP Request:  POST /agent/com-rca   { "input": { … } }
   ▼
AI Gateway  ── this service
   │  1. looks up the agent named in the URL              → agents.py
   │  2. BUILDS THE PROMPT: that agent's system prompt = the instructions
   │                       + the caller's `input`, serialized = the user message
   │  3. picks the model AND backend (the agent's, or `model` in the body —
   │                       e.g. "gpt-5.6-sol" → copilot, "openai:gpt-4.1" → openai)
   │  4. resolves credentials for that backend: a PAT (copilot, default or
   │                       per-tenant) or an API key (openai / anthropic)
   │  5. calls that backend: a locked-down session with no tools (copilot),
   │                       or a single stateless call with no tools parameter
   │                       at all (openai / anthropic)
   ▼
Copilot  ·  OpenAI  ·  Anthropic   ← whichever backend was picked runs the
   │                                  chosen model
   │
   │  model's answer as text, parsed into JSON where possible
   ▼
{ "agent", "model", "result": { … } }  ──► back to n8n
```

**Where the prompt lives.** The caller sends the data to analyse, which becomes
the **user message**; the gateway supplies the **system prompt** — the
instructions — from [agents.py](agents.py). That split is the point: you retune a
prompt or change a model in one file, and every caller picks it up without being
edited. (`/chat` is the exception: it forwards your `prompt` verbatim with no
system prompt.)

**Why a gateway instead of calling a model provider directly from the caller?**
The caller stays responsible for orchestration; the gateway does only
`prompt + model + context → response`, on whichever backend you point it at.
You can change models, prompts, or the backend itself without touching the
caller, and the caller never sees a single credential.

---

## 2. Files

The main source files are [app.py](app.py) for runtime behavior,
[agents.py](agents.py) for the prompt/model registry,
[providers/](providers/__init__.py) for the per-backend implementations
(Copilot, OpenAI, Anthropic), and
[deploy/azure/deploy-gateway-azure.sh](deploy/azure/deploy-gateway-azure.sh)
for Azure Container Apps deployment. The rest is support material.

---

## 3. How it works internally

### Providers
Every model backend implements the small `ChatProvider` interface in
[providers/base.py](providers/base.py): `start()`, `stop()`, `run()`. The
registry in [providers/\_\_init\_\_.py](providers/__init__.py) maps a provider
name to its module and parses the `<provider>:<model>` id a caller sends (no
prefix -> `copilot`). Three ship:
[providers/copilot_provider.py](providers/copilot_provider.py) (moved here
verbatim from an earlier single-provider version of this file),
[providers/openai_provider.py](providers/openai_provider.py), and
[providers/anthropic_provider.py](providers/anthropic_provider.py). Add a
fourth by implementing the interface and registering it, nothing in `app.py` or
`agents.py` changes. See the README's
[Providers](README.md#providers-choosing-a-backend) section for the operator-
facing view (env vars, session/tenant support per provider).

### Runtime lifecycle
On startup the app starts the **default** provider (`copilot`) once and keeps it
for the whole process; every other provider starts **lazily** on its first
request, so a deployment that never sends `openai:...`/`anthropic:...` never
needs that vendor's key set. `stop()` is called on every started provider at
shutdown. `/health` reports `runtime_ready` (the default provider only, for
backward compatibility) and `providers_started` (every provider actually in
use).

### One request → one locked-down session
Every `/chat` or `/agent/*` call goes through a single helper (`_run`) that:

1. Splits the requested `model` into `(provider, model)` and gets that
   provider's started instance (starting it if this is its first use).
2. Delegates to that provider's `run()`, which (Copilot) creates a session with
   **`available_tools=[]`** (no tools) and a **deny-all permission handler**, so
   the model can only produce text — it can never touch the filesystem, shell,
   or git; (OpenAI/Anthropic) sends a plain chat/messages call with no tools
   parameter at all, so the same guarantee holds with no extra code.
3. For an agent, the agent's **system prompt** is passed through as the
   provider's system message/role.
4. Returns the model's text.
5. (Copilot only) disconnects the session **unless** a `session_id` was
   supplied (see Sessions) — the other providers are stateless per call, so
   there is nothing to disconnect.

### Structured agent output
Agents are prompted to return JSON. Some models wrap that JSON in a
```` ```json ```` code fence, so the gateway strips the fence and parses it —
the `/agent/*` response therefore returns a real JSON object in `result`, not a
string. If parsing fails, the raw text is returned instead.

### Sessions (optional, persistent context)

> **`copilot`-provider only.** `openai`/`anthropic` accept `session_id` but
> ignore it — every call to those two is a single stateless system+user turn.
> See [README § Providers](README.md#providers-choosing-a-backend).

If you pass a `session_id`, the gateway reuses/persists that Copilot session so a
later call with the **same** id continues the same conversation. Useful to keep
incident context across steps, e.g.:

```
session_id = "COM-<serial>-<incident-id>"

t0  POST /agent/com-rca   {input: event}                → first analysis
t1  POST /agent/com-rca   {input: {ilo_logs…}, session_id}  → refined analysis
```

Omit `session_id` for independent one-shot calls (the session is torn down after
each request).

> **Your `session_id` is hashed into a UUID.** The Copilot runtime accepts only a
> UUID as a session id and rejects anything else at `session.create`
> (`invalid sessionId`). The gateway therefore namespaces your id per tenant and
> maps it through a deterministic **UUIDv5**, so the same `(tenant, session_id)`
> always reaches the same conversation while staying valid. Use whatever
> readable id you like — you never see the UUID.

---

## 4. Configuration

### Agents (models + prompts)
Everything about an agent lives in [agents.py](agents.py). Each entry is:

```python
"com-rca": AgentProfile(
    name="com-rca",
    model="gpt-5.6-sol",          # any id from GET /models, or "auto"
    description="Root-cause analysis of a COM event, optionally with iLO data.",
    system_prompt="You are an HPE Compute Ops Management incident analysis …",
),
```

Bundled agents:

| Agent | Default model | Purpose |
| ----- | ------------- | ------- |
| `com-triage` | `claude-sonnet-5` | Fast classification (category/severity/needs_rca). |
| `com-rca` | `gpt-5.6-sol` | Root-cause analysis (summary, cause, evidence, confidence, actions). |
| `com-remediation` | `claude-opus-5` | Ordered remediation plan with rollback. |
| `com-summary` | `auto` | Plain-language summary for Slack/Teams. |

To add or tune an agent, edit `AGENTS` in [agents.py](agents.py). Keep prompts
here, **not** in the n8n workflow. A caller may override the model per request;
the system prompt always comes from this file.

### Environment variables

| Variable | When | Meaning |
| -------- | ---- | ------- |
| *(none)* | local dev, `copilot` provider | The SDK reuses your existing `gh` / Copilot CLI login — nothing to set. If several `gh` accounts are logged in, the **active** one (`gh auth status`) is used; `gh auth switch` changes it (restart the process after). |
| `COPILOT_GITHUB_TOKEN` | headless/container, or local to override the `gh` login | Your **per-user fine-grained PAT** (Copilot enabled), passed to the SDK. Used when a request has no `tenant`. |
| `COPILOT_GITHUB_TOKEN_FILE` | headless/container (preferred), or local to override the `gh` login | Path to a mounted secret file containing the PAT (read file-first). |
| `COPILOT_TENANT_TOKENS_DIR` | multi-tenant | Directory with **one PAT file per tenant** (file name = tenant id). Secret-volume / Key Vault CSI friendly. |
| `COPILOT_TENANT_TOKENS_FILE` | multi-tenant | Alternative: a single JSON file mapping `{tenant: pat}`. |
| `COPILOT_REQUIRE_TENANT` | multi-tenant | `1`/`true` → reject any request without a `tenant` (400). Stops a misconfigured caller silently using the default token. |
| `OPENAI_API_KEY` / `OPENAI_API_KEY_FILE` | using the `openai` provider (`"model": "openai:..."`) | API key for `api.openai.com` or an OpenAI-compatible endpoint. File-first, same convention as the Copilot token. |
| `OPENAI_BASE_URL` | optional, `openai` provider | Repoints the `openai` provider at any OpenAI-compatible endpoint (Azure OpenAI compatible mode, a self-hosted Ollama/vLLM, …) instead of `api.openai.com`. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_API_KEY_FILE` | using the `anthropic` provider (`"model": "anthropic:..."`) | API key for the Anthropic Messages API. File-first, same convention. |

> **Local dev needs no PAT by default.** `COPILOT_GITHUB_TOKEN`/`_FILE` are not
> container-only — set either one before `uvicorn app:app` to pin the gateway to
> a specific account locally instead of following the ambient `gh` login. See
> [README § Run locally](README.md#run-locally) for the exact commands.

> **Credential fact:** GitHub Copilot has **no shared service token** — it is
> licensed per named user. A headless deployment must use a fine-grained PAT and
> therefore runs under that identity and quota. For a single-user deployment
> that's *your* PAT; when several people or services share the gateway, give each
> its own account PAT (see
> [§8. Multi-tenant](#8-multi-tenant-one-account-per-caller)) so quota and
> attribution are isolated. Service tokens from other vendors' "Copilot" products
> are different products and are **not** usable by this SDK. `OPENAI_API_KEY` /
> `ANTHROPIC_API_KEY` are single-key, no per-tenant routing — a `tenant` on a
> request to those providers is rejected with `400`.

### Security posture (do not change lightly)
Sessions are created with **no tools** and a **deny-all** permission handler. The
gateway is inference + model selection only — never a command executor. Do
**not** reintroduce `PermissionHandler.approve_all`.

---

## 5. Endpoints

See the README for the endpoint table. The only extra rules that matter here are:

- `/chat` takes the caller's `prompt` verbatim.
- `/agent/{name}` uses the agent's system prompt from [agents.py](agents.py).
- `/models` only lists models for the `copilot` provider (and only works in
  single-token mode there); `?provider=openai`/`anthropic` returns a short note
  instead of a real listing — see
  [README § Providers](README.md#providers-choosing-a-backend).
---

## 6. Local build & debug loop

Use the same `.venv` interpreter and Uvicorn command as the README. This guide
only adds the reminder that the repo's virtual environment, not the system
Python, is the one that has every provider's SDK installed (Copilot, OpenAI,
Anthropic).

---

## 7. Using it from n8n

### Networking first
n8n must be able to reach the gateway URL:

- **n8n on the same machine (native):** use `http://127.0.0.1:8000`.
- **n8n in Docker, gateway on the host:** use `http://host.docker.internal:8000`
  (Windows/Mac). Bind the gateway to `0.0.0.0` so it's reachable from the
  container:
  ```powershell
  .venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
  ```
- **Both in Docker Compose:** put them on the same network and use the service
  name, e.g. `http://ai-gateway:8000`.

### The HTTP Request node
Add an **HTTP Request** node in your workflow:

| Field | Value |
| ----- | ----- |
| Method | `POST` |
| URL | `http://<gateway-host>:8000/agent/com-rca` |
| Authentication | None |
| Send Body | On |
| Body Content Type | JSON |
| Specify Body | Using JSON |

Body (map the COM event fields from earlier nodes):

```json
{
  "input": {
    "event": "{{ $json.event }}",
    "ilo_logs": "{{ $json.ilo_logs }}",
    "server_info": "{{ $json.server_info }}"
  }
}
```

The node receives:

```json
{
  "agent": "com-rca",
  "model": "gpt-5.6-sol",
  "result": {
    "summary": "...",
    "likely_root_cause": "...",
    "evidence": "...",
    "confidence": 0.82,
    "recommended_actions": ["...", "..."]
  }
}
```

Reference `{{ $json.result.summary }}`,
`{{ $json.result.recommended_actions }}`, etc. in downstream nodes (Slack, Teams,
Halo, ServiceNow, …).

### Typical workflow shape

```
COM webhook  →  Function/Set (extract event)
             →  HTTP Request  POST /agent/com-triage
             →  IF  result.needs_rca == true
                   →  HTTP Request  POST /agent/com-rca
                   →  HTTP Request  POST /agent/com-remediation
             →  Slack / Teams / ITSM  (use result.*)
```

### Keeping incident context across nodes (optional)
Add a stable `session_id` to each body so successive calls share context:

```json
{
  "input": { "event": "{{ $json.event }}" },
  "session_id": "COM-{{ $json.serial }}-{{ $json.incidentId }}"
}
```

### Choosing / overriding a model (optional)
Add `"model": "auto"` (or any id from `GET /models`) to a body to override the
agent's default model for that call.

---

## 8. Multi-tenant (one account per caller)

> **`copilot`-provider only.** Copilot has no shared service token, which is
> what multi-tenant mode exists to work around. `openai`/`anthropic` are
> single-key providers with no per-tenant routing — a `tenant` sent to either
> is rejected with `400`. See
> [README § Providers](README.md#providers-choosing-a-backend).

The **same image** serves two modes, chosen entirely by configuration:

- **Single-token mode** (default): no `tenant` on requests; every call uses the
  single `COPILOT_GITHUB_TOKEN`. Right for one person or one service.
- **Multi-tenant mode**: each request carries a `tenant` id, and the gateway runs
  that call under **that tenant's own Copilot PAT**. Quota, rate limits and
  attribution are isolated per tenant, so one busy or misbehaving caller can't
  exhaust a shared quota or take the others offline.

Reach for multi-tenant mode when several teams, several services, or a classroom
of workstations share one gateway. It is opt-in — configure nothing and the
gateway behaves exactly as single-token mode.

### Why one account per caller (not one PAT + session_id)
`session_id` and account identity are **orthogonal**: `session_id` only threads a
conversation; it does **not** isolate quota or attribution. Copilot is licensed
per named user, so N callers sharing one PAT share one rate limit and one
identity. Giving each caller its own account is what buys isolation and a small
blast radius. `session_id` still works on top (see below).

### Configure the per-tenant PATs
Provide one PAT per tenant via **one of**:

| Source | Shape | Best for |
| ------ | ----- | -------- |
| **Azure Key Vault** | one secret per tenant (`copilot-tenant-<tenant>`), referenced by the app via a managed identity | **production** — PATs live only in Azure, never on your laptop |
| `COPILOT_TENANT_TOKENS_DIR` | a directory with one file per tenant, file name = tenant id, contents = that tenant's PAT | secret volumes / Key Vault CSI (each PAT its own mounted file) |
| `COPILOT_TENANT_TOKENS_FILE` | a single JSON file `{ "team-01": "github_pat_…", … }` | local dev / Azure Cloud Shell |

Set `COPILOT_REQUIRE_TENANT=1` so a request **without** a `tenant` is rejected
(`400`) — this stops a misconfigured caller from silently falling back to the
default token.

Tenant ids are validated (`[A-Za-z0-9_-]` only) to prevent path traversal into
the tokens directory. For the Azure deploy, tenant ids also become ACA (Azure Container Apps) secret
names, so use lowercase `[a-z0-9-]` (e.g. `team-01` … `team-25`, or `default`).

**Key Vault (recommended — no PATs on your laptop).** Store each bot PAT as a
Key Vault secret **from Azure Cloud Shell** (not your machine), then let the
deploy script wire ACA to read them via a managed identity:

```bash
# in Azure Cloud Shell, once per bot
az keyvault secret set --vault-name <your-key-vault> --name copilot-tenant-team-01 --value github_pat_...
az keyvault secret set --vault-name <your-key-vault> --name copilot-tenant-team-02 --value github_pat_...
```

**Local JSON map (dev / Cloud Shell only).** Where keeping PATs in a file is
acceptable, copy [tenants.example.json](tenants.example.json) to `tenants.json`
(gitignored) and fill in one PAT per tenant. Run the gateway locally against it:

```powershell
$env:COPILOT_TENANT_TOKENS_FILE = (Resolve-Path tenants.json).Path
$env:COPILOT_REQUIRE_TENANT = "1"
.venv\Scripts\python.exe -m uvicorn app:app --port 8000
```

Then a request must carry a `tenant`:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/chat -Method POST `
  -ContentType "application/json" `
  -Body '{"model":"auto","prompt":"Reply with exactly: OK","tenant":"team-01"}'
```

### Call it from n8n (per tenant)
Add the tenant id as `tenant` in the HTTP Request body:

```json
{
  "input": { "event": "{{ $json.event }}" },
  "tenant": "team-07",
  "session_id": "team-07-{{ $json.incidentId }}"
}
```

`session_id` is namespaced per tenant internally (`team-07:…`), so two tenants
reusing the same simple id never share a conversation. A persistent session stays
tied to the account that created it; because tenant→account is stable, the same
`(tenant, session_id)` always resolves to the same account.

> **Verified with two real accounts.** One runtime isolates concurrent sessions
> by the per-request PAT: two sessions ran side by side under their own
> identities, while a session created with a **bad** token was rejected
> (`401 Bad credentials`) instead of falling back to the runtime's global login.
> So each tenant's calls run under — and are billed/rate-limited against — its
> own account, not a shared identity.

### Deploy multi-tenant to Azure
**Recommended — Key Vault (no PATs on your laptop).** After loading the PATs
into Key Vault (above), point the deploy script at the vault + tenant list; it
creates a managed identity, grants it read access, and wires one ACA secret per
tenant as a **Key Vault reference** (ACA pulls the PAT at runtime):

```bash
export KEYVAULT=<your-key-vault>
export TENANTS=team-01,team-02   # one id per caller
./deploy/azure/deploy-gateway-azure.sh
```

**Alternative — local JSON map** (only where a PAT file is acceptable, e.g. Azure
Cloud Shell). Point `TENANTS_FILE` at the map; the script creates one inline ACA
secret per tenant and sets `COPILOT_TENANT_TOKENS_DIR` + `COPILOT_REQUIRE_TENANT=1`:

```bash
# tenants.json = { "team-01": "github_pat_…", … }
export TENANTS_FILE=./tenants.json
./deploy/azure/deploy-gateway-azure.sh
```

> Do **not** point a shared deployment at one personal PAT. Multi-tenant mode
> with `COPILOT_REQUIRE_TENANT=1` enforces this: every caller must present its
> own `tenant` or the request is rejected.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
| ------- | ------------------ |
| `Could not import module "app"` | You aren't running `uvicorn` from the repo root (where app.py lives). |
| `ModuleNotFoundError: copilot` | Running with global `python`; use `.venv\Scripts\python.exe`. |
| `503 Copilot runtime not ready` | Called an endpoint before startup finished, or the runtime failed to start. |
| `502` from `/chat` or `/agent/*` | SDK/model error (e.g. unavailable model, auth). Check the model id against `GET /models`. |
| `404` from `/agent/{name}` | Unknown agent name; see `GET /agents`. || `400` “requires a 'tenant'” | `COPILOT_REQUIRE_TENANT=1` is set but the request had no `tenant` (multi-tenant mode). |
| `404` “no Copilot token for tenant” | The `tenant` isn't configured in `COPILOT_TENANT_TOKENS_DIR`/`_FILE`. |
| n8n gets connection refused | Gateway bound to `127.0.0.1` but n8n is in Docker — bind `0.0.0.0` and use `host.docker.internal`. |
| `result` is a string, not JSON | The model didn't emit JSON for that prompt; tune the agent's system prompt in [agents.py](agents.py). |

---

## 10. Deploying to Azure Container Apps (primary path)

> This walkthrough deploys the `copilot` provider, which needs the most
> setup (a per-user PAT, optionally multi-tenant). For `openai`/`anthropic`
> instead, replace the `copilot-pat` secret and `COPILOT_GITHUB_TOKEN` env var
> below with `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` (or the `_FILE` variant) —
> everything else (internal ingress, image build, n8n wiring) is identical.

n8n and the gateway are decoupled — n8n just makes an HTTP call — so both run in
**the same Container Apps environment**, with the gateway on **internal ingress**
(reachable only by n8n, never public). The only thing that changes from local is
**auth**: the interactive Copilot CLI login can't run headless, so the container
authenticates with your **per-user fine-grained PAT** (Copilot enabled) via
`COPILOT_GITHUB_TOKEN`. There is **no** shared Copilot service token, so it runs
under your identity and quota.

```
Container Apps environment
├── n8n            (public HTTPS ingress)
│      │  POST http://ai-gateway/agent/com-rca
│      ▼
└── ai-gateway   (INTERNAL ingress only)
       COPILOT_GITHUB_TOKEN -> Container Apps secret (your PAT)
```

### Step 1 — build & push the image
The image base is **glibc** (Debian slim), required because the Copilot SDK loads
a native `runtime.node`; **Alpine/musl will not work**. The build also
pre-downloads the Copilot runtime so the container starts fast and needs no
GitHub Releases egress at runtime.

```powershell
# from the repo root. Pin a version tag — never deploy `:latest`, or nobody can
# tell which build is running. Change the code -> push a NEW, unused tag.
docker build -t ghcr.io/<your-org>/ai-gateway:1.0.1 .
docker push  ghcr.io/<your-org>/ai-gateway:1.0.1
```

> ACA can pull GHCR directly. If the package is **private**, pass registry
> credentials to the deploy script (`GHCR_USER` + a `read:packages` token) — see
> the script header.

### Step 2 — deploy (script)
The script deploys into the **same** resource group + environment as n8n, with
internal ingress and the PAT as a secret:

```bash
export COPILOT_PAT=github_pat_xxxxx           # your fine-grained PAT
# optional overrides (defaults reuse the relay/n8n RG + env):
#   RG=<resource-group> ACA_ENV=<aca-environment> APP_NAME=ai-gateway
./deploy/azure/deploy-gateway-azure.sh
```

> **Multi-tenant:** for a per-caller account model, deploy with Key Vault
> references instead of `COPILOT_PAT` — `export KEYVAULT=<vault> TENANTS=team-01,team-02,…`
> so the PATs stay in Key Vault (never on your laptop). See
> [§8. Multi-tenant](#8-multi-tenant-one-account-per-caller).

Equivalent manual `az` command (internal ingress + secret):

```bash
az containerapp create \
  --resource-group <resource-group> --name ai-gateway --environment <aca-environment> \
  --image ghcr.io/<your-org>/ai-gateway:1.0.1 \
  --ingress internal --target-port 8000 \
  --secrets copilot-pat="$COPILOT_PAT" \
  --env-vars COPILOT_GITHUB_TOKEN=secretref:copilot-pat
```

> **Prefer Key Vault for the PAT.** Instead of an inline secret you can bind a
> Key Vault secret to the app and reference it the same way
> (`COPILOT_GITHUB_TOKEN_FILE` if projected as a file, or `secretref:` for an ACA
> secret sourced from Key Vault). The gateway reads the token **file-first**, so a
> mounted secret file is the safer production form.

### Step 3 — point n8n at it
Both apps are in the same environment, so n8n reaches the gateway by app name:

```
http://ai-gateway/agent/com-rca
```

Everything in section 7 (HTTP Request node, body, `result` mapping, sessions,
model override) is identical — only the host changes from `127.0.0.1:8000` to
`ai-gateway`.

### Verify
```bash
# from a shell that can reach the internal env (e.g. an n8n exec, or temporarily
# flip ingress to external for a one-off check):
curl http://ai-gateway/health
```

> **Governance:** in single-token mode the gateway runs under **one** named
> identity and Copilot quota — fine for a single user or service. When several
> people or services share it, deploy **multi-tenant mode** instead: pass
> `TENANTS_FILE=./tenants.json` to the deploy script so each caller gets its own
> PAT and quota — see
> [§8. Multi-tenant](#8-multi-tenant-one-account-per-caller).

---

