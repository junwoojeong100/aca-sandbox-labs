#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

require_base_commands

SUBSCRIPTION_ID=${SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}
RESOURCE_GROUP=${RESOURCE_GROUP:-rg-ai-workspace-sandbox-lab}
LOCATION=${LOCATION:-koreacentral}
COMPACT_SUBSCRIPTION_ID=${SUBSCRIPTION_ID//-/}
ACR_NAME=${ACR_NAME:-aiws${COMPACT_SUBSCRIPTION_ID:0:20}}
IDENTITY_NAME=${IDENTITY_NAME:-id-ai-workspace-office-acr-pull}
LOG_WORKSPACE_NAME=${LOG_WORKSPACE_NAME:-log-ai-workspace-sandbox}
CONTAINER_ENV_NAME=${CONTAINER_ENV_NAME:-env-ai-workspace-sandbox}
OFFICE_POOL_NAME=${OFFICE_POOL_NAME:-ai-workspace-office-sbx}
IMAGE_REPOSITORY=${IMAGE_REPOSITORY:-office-sandbox}
IMAGE_TAG=${IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)}
IMAGE="$ACR_NAME.azurecr.io/$IMAGE_REPOSITORY:$IMAGE_TAG"
WORK_DIR="$WORK_ROOT/office"
mkdir -p "$WORK_DIR"

az account set --subscription "$SUBSCRIPTION_ID"
az extension add --name containerapp --upgrade --allow-preview true --yes --output none
az extension add --name quota --upgrade --yes --output none
for provider in \
  Microsoft.App \
  Microsoft.ContainerRegistry \
  Microsoft.ManagedIdentity \
  Microsoft.OperationalInsights \
  Microsoft.Quota; do
  az provider register --namespace "$provider" --wait
done

QUOTA_SCOPE="/subscriptions/$SUBSCRIPTION_ID/providers/Microsoft.App/locations/$LOCATION"
for resource_name in ManagedEnvironmentCount SessionPools; do
  az quota show --resource-name "$resource_name" --scope "$QUOTA_SCOPE" \
    --query '{limit:properties.limit.value}' --output json
  az quota usage show --resource-name "$resource_name" --scope "$QUOTA_SCOPE" \
    --query '{usage:properties.usages.value}' --output json
done

az group create --name "$RESOURCE_GROUP" --location "$LOCATION" \
  --tags purpose=ai-workspace-sandbox-lab --output none

if ! az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --output none 2>/dev/null; then
  az acr check-name --name "$ACR_NAME" \
    --query nameAvailable --output tsv | grep -qx true \
    || die "ACR name is unavailable: $ACR_NAME"
  az acr create \
    --name "$ACR_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Basic \
    --admin-enabled false \
    --tags purpose=ai-workspace-office-sandbox \
    --output none
fi
ADMIN_ENABLED=$(az acr show --name "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query adminUserEnabled --output tsv)
[[ "$ADMIN_ENABLED" == "false" ]] || die "ACR admin user must be disabled"

if ! az identity show --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null; then
  az identity create \
    --name "$IDENTITY_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --tags purpose=ai-workspace-office-image-pull \
    --output none
fi
IDENTITY_ID=$(az identity show --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" --query id --output tsv)
IDENTITY_PRINCIPAL_ID=$(az identity show --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" --query principalId --output tsv)
ACR_ID=$(az acr show --name "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" --query id --output tsv)
ensure_role_assignment AcrPull "$IDENTITY_PRINCIPAL_ID" ServicePrincipal "$ACR_ID"

if ! az monitor log-analytics workspace show \
  --name "$LOG_WORKSPACE_NAME" \
  --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null; then
  az monitor log-analytics workspace create \
    --name "$LOG_WORKSPACE_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --retention-time 30 \
    --tags purpose=ai-workspace-sandbox-monitoring \
    --output none
fi
LOG_WORKSPACE_ID=$(az monitor log-analytics workspace show \
  --name "$LOG_WORKSPACE_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query customerId --output tsv)

if ! az containerapp env show --name "$CONTAINER_ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null; then
  LOG_WORKSPACE_KEY=$(az monitor log-analytics workspace get-shared-keys \
    --name "$LOG_WORKSPACE_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query primarySharedKey --output tsv)
  az containerapp env create \
    --name "$CONTAINER_ENV_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --enable-workload-profiles true \
    --logs-destination log-analytics \
    --logs-workspace-id "$LOG_WORKSPACE_ID" \
    --logs-workspace-key "$LOG_WORKSPACE_KEY" \
    --tags purpose=ai-workspace-office-sandbox \
    --output none
  unset LOG_WORKSPACE_KEY
fi

az acr build \
  --registry "$ACR_NAME" \
  --image "$IMAGE_REPOSITORY:$IMAGE_TAG" \
  --file "$REPO_ROOT/office-container/Dockerfile" \
  --no-logs \
  "$REPO_ROOT/office-container" \
  --output none

if ! az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" --output none 2>/dev/null; then
  az containerapp sessionpool create \
    --name "$OFFICE_POOL_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --environment "$CONTAINER_ENV_NAME" \
    --location "$LOCATION" \
    --container-type CustomContainer \
    --image "$IMAGE" \
    --cpu 1.0 \
    --memory 2Gi \
    --target-port 8080 \
    --registry-server "$ACR_NAME.azurecr.io" \
    --registry-identity "$IDENTITY_ID" \
    --max-sessions 5 \
    --ready-sessions 1 \
    --cooldown-period 3600 \
    --network-status EgressDisabled \
    --probe-yaml "$REPO_ROOT/office-container/probes.yaml" \
    --no-wait \
    --output none
else
  az containerapp sessionpool update \
    --name "$OFFICE_POOL_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --image "$IMAGE" \
    --no-wait \
    --output none
fi
wait_for_pool "$OFFICE_POOL_NAME" "$RESOURCE_GROUP" "$IMAGE"
NETWORK_STATUS=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.sessionNetworkConfiguration.status \
  --output tsv)
READY_SESSIONS=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.scaleConfiguration.readySessionInstances \
  --output tsv)
PROBE_TYPES=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query 'properties.customContainerTemplate.containers[0].probes[].type' \
  --output tsv | sort | paste -sd, -)
[[ "$NETWORK_STATUS" == "EgressDisabled" ]] \
  || die "Office pool egress must be disabled"
[[ "$READY_SESSIONS" == "1" ]] \
  || die "Office pool must keep exactly one ready session for this lab"
[[ "$PROBE_TYPES" == "Liveness,Startup" ]] \
  || die "Office pool must have Startup and Liveness probes"

POOL_ID=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv)
CALLER_OBJECT_ID=$(az ad signed-in-user show --query id --output tsv)
ensure_role_assignment \
  "Azure ContainerApps Session Executor" \
  "$CALLER_OBJECT_ID" \
  User \
  "$POOL_ID"

ENDPOINT=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.poolManagementEndpoint \
  --output tsv)
SESSION_ID="office-$(python3 -c 'import uuid; print(uuid.uuid4())')"

http=
for attempt in $(seq 1 30); do
  TOKEN=$(az account get-access-token \
    --resource https://dynamicsessions.io \
    --query accessToken --output tsv)
  http=$(curl --silent --show-error --output "$WORK_DIR/health.json" \
    --write-out '%{http_code}' \
    "$ENDPOINT/health?identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN")
  [[ "$http" == "200" ]] && break
  if [[ "$http" == "403" || "$http" == "502" || "$http" == "503" ]]; then
    sleep 20
    continue
  fi
  die "Office health failed with HTTP $http: $(cat "$WORK_DIR/health.json")"
done
[[ "$http" == "200" ]] || die "Office health did not become ready"
jq -e '.status == "ok"' "$WORK_DIR/health.json" >/dev/null

http=$(curl --silent --show-error --output "$WORK_DIR/generate.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/generate?identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "title":"AI Workspace Office validation",
    "content":"Python and Office workloads use separate isolated pools.\nArtifacts require inspection and approval before promotion."
  }')
expect_2xx "$http" "Office generation" "$WORK_DIR/generate.json"
test "$(jq '.files | length' "$WORK_DIR/generate.json")" = "4"

for file_name in report.docx report.pdf report.pptx report.xlsx; do
  download_path=$(jq -r --arg name "$file_name" \
    '.files[] | select(.name == $name) | .downloadPath' \
    "$WORK_DIR/generate.json")
  expected_hash=$(jq -r --arg name "$file_name" \
    '.files[] | select(.name == $name) | .sha256' \
    "$WORK_DIR/generate.json")
  temporary_file="$WORK_DIR/$file_name.tmp"
  http=$(curl --silent --show-error --output "$temporary_file" \
    --write-out '%{http_code}' \
    "$ENDPOINT$download_path?identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN")
  expect_2xx "$http" "$file_name download" "$temporary_file"
  mv "$temporary_file" "$WORK_DIR/$file_name"
  actual_hash=$(sha256_file "$WORK_DIR/$file_name")
  [[ "$actual_hash" == "$expected_hash" ]] \
    || die "Hash mismatch: $file_name"
done

unzip -t "$WORK_DIR/report.docx" >/dev/null
unzip -t "$WORK_DIR/report.pptx" >/dev/null
unzip -t "$WORK_DIR/report.xlsx" >/dev/null
head -c 4 "$WORK_DIR/report.pdf" | grep -q '%PDF'

curl --fail-with-body --silent --show-error \
  --request POST \
  "$ENDPOINT/.management/getSession?api-version=2025-02-02-preview&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output "$WORK_DIR/session.json"

az monitor metrics list \
  --resource "$POOL_ID" \
  --metric PoolReadyPodCount PoolExecutingPodCount PoolPendingPodCount \
  --interval PT1M \
  --aggregation Average Maximum \
  --output json > "$WORK_DIR/metrics.json"
jq -e '
  [.value[].name.value]
  | contains([
      "PoolReadyPodCount",
      "PoolExecutingPodCount",
      "PoolPendingPodCount"
    ])
' "$WORK_DIR/metrics.json" >/dev/null

logs_found=false
for attempt in $(seq 1 12); do
  az monitor log-analytics query \
    --workspace "$LOG_WORKSPACE_ID" \
    --analytics-query \
      'search * | where TimeGenerated > ago(1h) | summarize Records=count() by $table' \
    --timespan PT1H \
    --output json > "$WORK_DIR/log-summary.json" || true
  if jq -e 'map(select((.Records | tonumber) > 0)) | length > 0' \
    "$WORK_DIR/log-summary.json" >/dev/null 2>&1; then
    logs_found=true
    break
  fi
  sleep 30
done
[[ "$logs_found" == "true" ]] \
  || die "No session logs were ingested into Log Analytics"

cat > "$WORK_DIR/validation.txt" <<EOF
pool=$OFFICE_POOL_NAME
image=$IMAGE
session=$SESSION_ID
formats=docx,pdf,pptx,xlsx
hashes=verified
EOF

log "Office validation passed."
log "Artifacts: $WORK_DIR"
log "Session: $SESSION_ID"
log "Image: $IMAGE"
