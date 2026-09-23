#!/usr/bin/env bash
#
# Deploy the AI Gateway to Azure Container Apps.
#
# The AI gateway is deployed with INTERNAL ingress only (reachable from other apps
# in the same Container Apps environment — never public; the AI gateway has no
# inbound authentication of its own). It authenticates to GitHub Copilot with a
# per-user fine-grained PAT (Copilot enabled), stored as a Container Apps secret.
# There is NO shared Copilot service token, so this PAT runs under that account's
# identity and quota.
#
# Prerequisites:
#   - az CLI logged in (az login), an active subscription selected.
#   - The published image is available in GHCR. Customers do not build or push
#     the image as part of deployment.
#   - COPILOT_PAT set in your shell to the fine-grained PAT (do NOT hardcode it):
#       export COPILOT_PAT=github_pat_xxx
#
# Multi-tenant modes — one Copilot PAT per caller (tenant):
#
#   A) Key Vault (RECOMMENDED — no PAT ever on your laptop). The AI gateway app
#      references each PAT straight from Key Vault via a managed identity, so
#      the tokens live ONLY in Azure. You set:
#        export KEYVAULT=<your-key-vault>     # an existing Key Vault (RBAC mode)
#        export TENANTS=team-01,team-02,...   # tenant ids (== ACA secret names)
#      Each tenant's PAT is expected in Key Vault as secret
#        <KV_SECRET_PREFIX><tenant>   (default prefix: copilot-tenant-, e.g.
#        copilot-tenant-team-01). Load those once from Azure Cloud Shell (not your
#        laptop), e.g.:
#          az keyvault secret set --vault-name <your-key-vault> \
#            --name copilot-tenant-team-01 --value github_pat_xxx
#
#   B) Local JSON map (dev / Azure Cloud Shell only). Point TENANTS_FILE at a
#      { "team-01": "github_pat_...", ... } file; each becomes an inline ACA secret.
#        export TENANTS_FILE=./tenants.json    # requires jq
#      Use this only where keeping the PATs in a file is acceptable.
#
#   Both mount one secret per tenant as a file the AI gateway reads per request, and
#   set COPILOT_REQUIRE_TENANT=1 so tenant-less requests are rejected. Tenant ids
#   must be ACA-secret-safe: lowercase [a-z0-9-].
#
# Usage: ./deploy-gateway-azure.sh
#
set -euo pipefail

# ---- Config (override via env before running) -----------------------------
RG="${RG:-rg-ai-gateway}"           # override to reuse an existing group
LOC="${LOC:-westeurope}"
ACA_ENV="${ACA_ENV:-aca-ai-gateway}" # use the SAME environment as the caller
APP_NAME="${APP_NAME:-ai-gateway}"
IMAGE="${IMAGE:-ghcr.io/jullienl/ai-gateway:1.0.1}"
TARGET_PORT="${TARGET_PORT:-8000}"

# Private GHCR pull (optional). Set both to let ACA pull a private image:
#   GHCR_USER=<github-username>  GHCR_TOKEN=<PAT with read:packages>
GHCR_SERVER="ghcr.io"
GHCR_USER="${GHCR_USER:-}"
GHCR_TOKEN="${GHCR_TOKEN:-}"

# TENANTS_FILE (optional): JSON map {tenant: pat}. When set -> multi-tenant mode.
TENANTS_FILE="${TENANTS_FILE:-}"
TENANT_MOUNT="/run/copilot-tenants"

# Key Vault multi-tenant (recommended): reference each bot PAT from Key Vault via
# a user-assigned managed identity — no PAT ever touches this machine.
KEYVAULT="${KEYVAULT:-}"
TENANTS="${TENANTS:-}"
KV_SECRET_PREFIX="${KV_SECRET_PREFIX:-copilot-tenant-}"
UAMI_NAME="${UAMI_NAME:-id-ai-gateway}"

if [[ -n "$KEYVAULT" ]]; then
  # ---- Multi-tenant via Key Vault references -------------------------------
  [[ -n "$TENANTS" ]] || { echo "ERROR: KEYVAULT set but TENANTS (comma list of ids) is empty." >&2; exit 1; }
elif [[ -n "$TENANTS_FILE" ]]; then
  # ---- Multi-tenant: one PAT per tenant from a local JSON map ---------------
  command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required for TENANTS_FILE mode." >&2; exit 1; }
  [[ -f "$TENANTS_FILE" ]] || { echo "ERROR: TENANTS_FILE '$TENANTS_FILE' not found." >&2; exit 1; }
elif [[ -z "${COPILOT_PAT:-}" ]]; then
  echo "ERROR: set COPILOT_PAT (personal), KEYVAULT+TENANTS (multi-tenant via Key Vault)," >&2
  echo "       or TENANTS_FILE (multi-tenant via local JSON map)." >&2
  echo "  export COPILOT_PAT=github_pat_xxx" >&2
  echo "  # or" >&2
  echo "  export KEYVAULT=<your-key-vault> TENANTS=team-01,team-02" >&2
  echo "  # or" >&2
  echo "  export TENANTS_FILE=./tenants.json" >&2
  exit 1
fi

echo ">> Using RG=$RG LOC=$LOC ENV=$ACA_ENV APP=$APP_NAME IMAGE=$IMAGE"

# ---- Resource group + environment (create if missing, else reuse) ---------
az group create --name "$RG" --location "$LOC" -o none
az extension add --name containerapp --upgrade -o none 2>/dev/null || true
az containerapp env create --resource-group "$RG" --name "$ACA_ENV" \
  --location "$LOC" -o none 2>/dev/null || true

# ---- Registry credentials (only if pulling a private image) ---------------
REGISTRY_ARGS=()
if [[ -n "$GHCR_USER" && -n "$GHCR_TOKEN" ]]; then
  REGISTRY_ARGS=(--registry-server "$GHCR_SERVER" \
                 --registry-username "$GHCR_USER" \
                 --registry-password "$GHCR_TOKEN")
fi

# ---- Container app: INTERNAL ingress, PAT(s) as secret(s) -----------------
SECRET_ARGS=()
ENV_ARGS=()
VOLUME_ARGS=()
IDENTITY_ARGS=()

if [[ -n "$KEYVAULT" ]]; then
  # A) Key Vault references via a user-assigned managed identity — the PATs stay
  #    in Key Vault and are pulled by ACA at runtime; nothing lands here.
  echo ">> Multi-tenant via Key Vault '$KEYVAULT' (identity: $UAMI_NAME)"
  az identity create -g "$RG" -n "$UAMI_NAME" -o none 2>/dev/null || true
  UAMI_ID="$(az identity show -g "$RG" -n "$UAMI_NAME" --query id -o tsv)"
  UAMI_PID="$(az identity show -g "$RG" -n "$UAMI_NAME" --query principalId -o tsv)"
  KV_ID="$(az keyvault show -n "$KEYVAULT" --query id -o tsv)"
  # RBAC-mode vault: grant the identity read access to secrets (idempotent).
  az role assignment create --assignee-object-id "$UAMI_PID" \
    --assignee-principal-type ServicePrincipal \
    --role "Key Vault Secrets User" --scope "$KV_ID" -o none 2>/dev/null || true

  IFS=',' read -ra _ids <<< "$TENANTS"
  for tenant in "${_ids[@]}"; do
    tenant="$(echo "$tenant" | tr -d '[:space:]')"
    [[ -z "$tenant" ]] && continue
    if [[ ! "$tenant" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
      echo "ERROR: tenant id '$tenant' is not ACA-secret-safe (use lowercase [a-z0-9-])." >&2
      exit 1
    fi
    kv_uri="https://${KEYVAULT}.vault.azure.net/secrets/${KV_SECRET_PREFIX}${tenant}"
    SECRET_ARGS+=("$tenant=keyvaultref:${kv_uri},identityref:${UAMI_ID}")
  done
  IDENTITY_ARGS=(--user-assigned "$UAMI_ID")
  VOLUME_ARGS=(--secret-volume-mount "$TENANT_MOUNT")
  ENV_ARGS=("COPILOT_TENANT_TOKENS_DIR=$TENANT_MOUNT" "COPILOT_REQUIRE_TENANT=1")
  echo ">> ${#SECRET_ARGS[@]} tenant secret(s) referenced from Key Vault."
elif [[ -n "$TENANTS_FILE" ]]; then
  # B) One inline ACA secret per tenant from a local JSON map.
  while IFS= read -r tenant; do
    if [[ ! "$tenant" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
      echo "ERROR: tenant id '$tenant' is not ACA-secret-safe (use lowercase [a-z0-9-])." >&2
      exit 1
    fi
    pat="$(jq -r --arg k "$tenant" '.[$k]' "$TENANTS_FILE")"
    SECRET_ARGS+=("$tenant=$pat")
  done < <(jq -r 'keys[]' "$TENANTS_FILE")

  # Mount all secrets as files under TENANT_MOUNT (file name == tenant id),
  # then point the AI gateway at that directory and require a tenant on every call.
  VOLUME_ARGS=(--secret-volume-mount "$TENANT_MOUNT")
  ENV_ARGS=("COPILOT_TENANT_TOKENS_DIR=$TENANT_MOUNT" "COPILOT_REQUIRE_TENANT=1")
  echo ">> Multi-tenant mode: ${#SECRET_ARGS[@]} bot PAT(s) from $TENANTS_FILE"
else
  SECRET_ARGS=("copilot-pat=$COPILOT_PAT")
  ENV_ARGS=("COPILOT_GITHUB_TOKEN=secretref:copilot-pat")
  echo ">> Single-token mode: single COPILOT_PAT"
fi

az containerapp create \
  --resource-group "$RG" --name "$APP_NAME" --environment "$ACA_ENV" \
  --image "$IMAGE" \
  --ingress internal --target-port "$TARGET_PORT" \
  --secrets "${SECRET_ARGS[@]}" \
  --env-vars "${ENV_ARGS[@]}" \
  "${IDENTITY_ARGS[@]}" \
  "${VOLUME_ARGS[@]}" \
  "${REGISTRY_ARGS[@]}" \
  -o none

# Internal FQDN other apps in the same environment use to reach the AI gateway.
FQDN="$(az containerapp show --resource-group "$RG" --name "$APP_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv)"

echo
echo "==================================================================="
echo " AI Gateway deployed (INTERNAL ingress)."
echo " Internal base URL (from n8n in the same environment):"
echo "     http://$APP_NAME"
echo "   or"
echo "     https://$FQDN"
echo
echo " In n8n's HTTP Request node, call e.g.:"
echo "     http://$APP_NAME/agent/com-rca"
echo "==================================================================="
if [[ -n "$KEYVAULT" || -n "$TENANTS_FILE" ]]; then
  echo " MULTI-TENANT: every request MUST include a 'tenant' (e.g. \"team-07\")."
  echo "     Each tenant runs under its own PAT/quota. Tenant-less = 400."
  [[ -n "$KEYVAULT" ]] && echo "     PATs referenced from Key Vault '$KEYVAULT' (none stored locally)."
else
  echo " NOTE: the AI gateway runs under YOUR Copilot identity/quota (per-user PAT)."
  echo "       For several callers, redeploy with KEYVAULT=<kv> TENANTS=team-01,..."
fi
