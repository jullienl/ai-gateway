# AI Gateway Operator Guide

This guide is for operators deploying and configuring the published AI Gateway.
Customers use the published GHCR image.

## Contents

- [1. Choose a backend](#1-choose-a-backend)
- [2. Available agents and prompts](#2-available-agents-and-prompts)
- [3. Where prompts live](#3-where-prompts-live)
- [4. Use the published image](#4-use-the-published-image)
- [5. Run with Docker](#5-run-with-docker)
- [6. HTTP, HTTPS, and certificates](#6-http-https-and-certificates)
- [7. Multiple gateway replicas](#7-multiple-gateway-replicas-and-load-balancing)
- [8. Verify the deployment](#8-verify-the-deployment)
- [9. HPE COM integration](#9-hpe-com-integration)
- [10. Automation integrations](#10-automation-integrations)
- [11. Azure Container Apps](#11-azure-container-apps)
- [12. Copilot multi-tenant mode](#12-copilot-multi-tenant-mode)
- [13. Security](#13-security)
- [14. Troubleshooting](#14-troubleshooting)

## 1. Choose a backend

The gateway supports these backends:

| Backend | Required configuration |
| --- | --- |
| GitHub Copilot | `COPILOT_GITHUB_TOKEN_FILE` or `COPILOT_GITHUB_TOKEN` |
| OpenAI | `OPENAI_API_KEY_FILE` or `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY_FILE` or `ANTHROPIC_API_KEY` |
| Customer-hosted OpenAI-compatible model | `AI_PROVIDER=openai`, `OPENAI_BASE_URL`, and an API key if required |

For a customer-hosted model, the server must implement the OpenAI Chat
Completions API at `/v1/chat/completions`. The gateway must be able to reach the
server from its deployment network.

## 2. Available agents and prompts

The gateway provides these named agents by default:

| Agent | Default backend/model | Prompt purpose |
| --- | --- | --- |
| `com-triage` | Copilot / `claude-sonnet-5` | Classify the event and decide whether deeper analysis is needed. |
| `com-rca` | Copilot / `gpt-5.6-sol` | Analyze the event and supplied telemetry to identify the likely root cause. |
| `com-remediation` | Copilot / `claude-opus-5` | Propose safe, ordered remediation steps, risks, and rollback. |
| `com-summary` | Copilot / `auto` | Produce a concise human-readable event summary. |

The full system prompts are owned by the gateway configuration and are not
duplicated in this guide. The configured agent list, provider, model, and
description are available at runtime:

```bash
curl http://localhost:8000/agents
```

Use the agent name in the request path, such as `/agent/com-rca`. A request may
override the model with a provider-qualified value such as
`openai:customer-model`.

## 3. Where prompts live

The gateway applies each agent's system prompt to the caller's `input`. The
caller supplies event data and evidence, not instructions. This keeps prompt
ownership, model selection, and credentials in the gateway rather than in each
integrating application.

Prompt summaries in this guide are customer-facing descriptions. The running
configuration and `GET /agents` endpoint are authoritative if an operator has
customized the agents.

## 4. Use the published image

The public image is published to GHCR:

```text
ghcr.io/jullienl/ai-gateway:1.0.2
```

This example uses release `1.0.2`. The published package is public and can be
pulled directly from GHCR.

Customers do not need to clone this repository, build an image, or publish an
image.

## 5. Run with Docker

The following example uses Copilot with a mounted secret file:

```bash
docker pull ghcr.io/jullienl/ai-gateway:1.0.2

docker run -d --name ai-gateway -p 8000:8000 \
  -v /run/secrets/copilot_pat:/run/secrets/copilot_pat:ro \
  -e COPILOT_GITHUB_TOKEN_FILE=/run/secrets/copilot_pat \
  ghcr.io/jullienl/ai-gateway:1.0.2
```

For an OpenAI-compatible on-prem model:

```bash
docker run -d --name ai-gateway -p 8000:8000 \
  -v /run/secrets/model_api_key:/run/secrets/model_api_key:ro \
  -e AI_PROVIDER=openai \
  -e OPENAI_BASE_URL=https://model-server.example.com/v1 \
  -e OPENAI_API_KEY_FILE=/run/secrets/model_api_key \
  ghcr.io/jullienl/ai-gateway:1.0.2
```

Keep secret files outside the image and do not commit them.

### Docker Compose

```yaml
services:
  ai-gateway:
    image: ghcr.io/jullienl/ai-gateway:1.0.2
    ports:
      - "8000:8000"
    environment:
      AI_PROVIDER: openai
      OPENAI_BASE_URL: https://model-server.example.com/v1
      OPENAI_API_KEY_FILE: /run/secrets/model_api_key
    secrets:
      - model_api_key

secrets:
  model_api_key:
    file: ./model-api-key.txt
```

```bash
docker compose up -d
```

## 6. HTTP, HTTPS, and certificates

Use HTTP only for localhost or an isolated trusted private network:

```text
OPENAI_BASE_URL=http://model-server:8000/v1
```

Use HTTPS for shared, public, or otherwise untrusted network paths:

```text
OPENAI_BASE_URL=https://model-server.example.com:8443/v1
```

If the model server uses a private certificate authority, mount a merged CA
bundle and set:

```text
SSL_CERT_FILE=/run/secrets/model-ca-bundle.pem
```

The bundle must contain both the private CA and public roots. A CA file
containing only the private certificate can break other HTTPS connections.

The gateway does not authenticate its callers. Put it on a private network,
use internal ingress, or place it behind an authenticated reverse proxy.

## 7. Multiple gateway replicas and load balancing

Multiple instances of the published image can run behind a load balancer or
platform ingress. Use the same provider configuration, secret files, agent
configuration, and model endpoint on every replica.

### Stateless providers

OpenAI, customer-hosted OpenAI-compatible, and Anthropic requests are
stateless. Requests can be distributed freely across healthy replicas. This is
the recommended scaling pattern for event analysis, where each event is one
independent request.

### Copilot sessions

Copilot requests without `session_id` can also be distributed freely. A
Copilot request with `session_id` asks the provider runtime to resume a
persistent conversation. Configure session affinity, also called sticky
sessions, so repeated requests for the same conversation reach the same
gateway replica. Without affinity, a later request may reach another replica
whose runtime does not hold that conversation.

All replicas should expose `GET /health` to the load balancer. Remove an
instance from rotation when its health response is not ready. The gateway does
not share session state, provider clients, or in-memory state between replicas.

## 8. Verify the deployment

Check gateway health:

```bash
curl http://localhost:8000/health
```

Run an agent:

```bash
curl -X POST http://localhost:8000/agent/com-rca \
  -H "Content-Type: application/json" \
  -d '{"input":{"event":{"title":"Example event"}}}'
```

A successful response includes an `agent`, a `model`, and a `result`. The COM
root-cause agent expects these result fields:

- `summary`
- `likely_root_cause`
- `evidence`
- `confidence`
- `recommended_actions`

The selected model must return valid JSON for the gateway to return a structured
result. Otherwise, the result may be returned as text.

### Structured `com-rca` output

For the HPE COM integration, `com-rca` should return:

```json
{
  "summary": "Short incident summary",
  "likely_root_cause": "Most likely cause",
  "evidence": "Signals supporting the conclusion",
  "confidence": 0.85,
  "recommended_actions": ["First action", "Second action"]
}
```

`confidence` is a number from 0 to 1. `recommended_actions` is an ordered list.
The gateway parses JSON responses and JSON responses wrapped in a Markdown code
fence. Plain text is returned as text and may not populate downstream analysis
fields.

## 9. HPE COM integration

The HPE COM bridge and shim from the public
[HPE-COM-Event-Integrations](https://github.com/jullienl/HPE-COM-Event-Integrations)
project call the gateway through the analyzer contract.
Configure the caller with:

```text
AI_ANALYZER_URL=http://ai-gateway:8000
AI_AGENT=com-rca
```

Use an HTTPS URL when the gateway is reached across a shared or untrusted
network. The integration-specific setup is documented in the
[HPE COM event analyzer guide](https://github.com/jullienl/HPE-COM-Event-Integrations/blob/main/com-event-core/README.md#set-up-the-analyzer).

## 10. Automation integrations

n8n, Zapier, Make, and other automation platforms are optional callers. Use
their HTTP request action with:

| Field | Value |
| --- | --- |
| Method | `POST` |
| URL | `http://<gateway-host>:8000/agent/com-rca` |
| Body | JSON |
| Authentication | The authentication configured by the reverse proxy, if any |

Example body:

```json
{
  "input": {
    "event": "...event data...",
    "evidence": "...optional evidence..."
  }
}
```

For external or untrusted access, use an HTTPS reverse-proxy URL instead of the
HTTP example.

## 11. Azure Container Apps

The repository includes `deploy/azure/deploy-gateway-azure.sh` for operators
using Azure Container Apps. The script deploys the published GHCR image and
configures internal ingress.

Set the deployment values before running it:

```bash
export RG=<resource-group>
export ACA_ENV=<container-apps-environment>
export APP_NAME=ai-gateway
export IMAGE=ghcr.io/jullienl/ai-gateway:1.0.2
export COPILOT_PAT=<copilot-token>
./deploy/azure/deploy-gateway-azure.sh
```

For Copilot multi-tenant mode, use the Key Vault mode described below instead of
putting a token in the shell.

The container listens on HTTP port 8000. Azure Container Apps terminates HTTPS
at ingress. Calls from applications in the same environment can use the app
name, such as `http://ai-gateway`; external callers should use the platform
HTTPS endpoint.

## 12. Copilot multi-tenant mode

Multi-tenant mode is for several callers using separate Copilot identities. It
is not a feature of the OpenAI-compatible or Anthropic providers.

Use one of these sources:

- `COPILOT_TENANT_TOKENS_DIR`: one mounted token file per tenant.
- `COPILOT_TENANT_TOKENS_FILE`: a JSON map of tenant names to tokens.
- Azure Key Vault references through the deployment script.

Set:

```text
COPILOT_REQUIRE_TENANT=1
```

Then every request must include a tenant. Tenant names must be safe identifiers
and must match the configured token entries.

### Docker example: one token file per tenant

Create a secrets directory on the host with one file per tenant. The filename
is the tenant value sent in each request:

```text
./copilot-tenants/
├── team-01
└── team-02
```

Each file contains only that tenant's Copilot token. Mount the directory
read-only and require a tenant on every request:

```bash
docker run -d --name ai-gateway -p 8000:8000 \
  -v "$(pwd)/copilot-tenants:/run/copilot-tenants:ro" \
  -e COPILOT_TENANT_TOKENS_DIR=/run/copilot-tenants \
  -e COPILOT_REQUIRE_TENANT=1 \
  ghcr.io/jullienl/ai-gateway:1.0.2
```

Call the gateway with the matching tenant:

```bash
curl -X POST http://localhost:8000/agent/com-rca \
  -H "Content-Type: application/json" \
  -d '{"tenant":"team-01","input":{"event":{"title":"Example event"}}}'
```

The request runs under the Copilot identity stored in
`./copilot-tenants/team-01`. A request without `tenant`, or with an unknown
tenant, is rejected. Protect the directory on the host and never commit it.

For Azure, the recommended pattern is Key Vault plus a managed identity. Keep
customer tokens in Key Vault rather than in a local JSON file.

## 13. Security

The gateway does not authenticate callers itself. Run it on a private network
or internal ingress, or put an authenticated reverse proxy in front of it. Do
not expose an unauthenticated gateway directly to the internet.

Keep provider credentials in mounted secret files. Prompts, event data, and
model responses are sent to the selected backend, so confirm that the selected
backend is approved for the data being processed.

The gateway does not give models filesystem, shell, or source-control tools.
Requests are limited to 1 MB by default.

## 14. Troubleshooting

| Symptom | Action |
| --- | --- |
| Image pull denied | Check the image name, version tag, registry connectivity, and container runtime logs. |
| Gateway cannot reach the model | Check `OPENAI_BASE_URL`, DNS, firewall rules, and routing from the gateway container. |
| TLS certificate failure | Set `SSL_CERT_FILE` to a merged private and public CA bundle. |
| `401` from the model | Check the API key or mounted secret file. |
| `502` from `/agent/*` | Check model availability, model name, timeout, and response format. |
| Result is plain text | The model did not return valid JSON for the selected agent. |
| Caller gets connection refused | Check the gateway address, ingress, port 8000, and reverse proxy. |
| Caller receives unauthorized access | Configure the authentication expected by the gateway's private network or reverse proxy. |

