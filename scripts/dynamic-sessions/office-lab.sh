#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

require_base_commands

SUBSCRIPTION_ID=${SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}
RESOURCE_GROUP=${RESOURCE_GROUP:-rg-ai-workspace-dynamic-sessions-lab}
LOCATION=${LOCATION:-koreacentral}
COMPACT_SUBSCRIPTION_ID=${SUBSCRIPTION_ID//-/}
ACR_NAME=${ACR_NAME:-aiwsds${COMPACT_SUBSCRIPTION_ID:0:20}}
IDENTITY_NAME=${IDENTITY_NAME:-id-ai-workspace-office-acr-pull}
LOG_WORKSPACE_NAME=${LOG_WORKSPACE_NAME:-log-ai-workspace-sandbox}
CONTAINER_ENV_NAME=${CONTAINER_ENV_NAME:-env-ai-workspace-sandbox}
OFFICE_POOL_NAME=${OFFICE_POOL_NAME:-ai-workspace-office-sbx}
IMAGE_REPOSITORY=${IMAGE_REPOSITORY:-office-sandbox}
IMAGE_TAG=${IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)}
IMAGE="$ACR_NAME.azurecr.io/$IMAGE_REPOSITORY:$IMAGE_TAG"
WORK_DIR="$WORK_ROOT/dynamic-sessions/office"
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
NEEDS_ENVIRONMENT=0
NEEDS_SESSION_POOL=0
az containerapp env show \
  --name "$CONTAINER_ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output none 2>/dev/null || NEEDS_ENVIRONMENT=1
az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output none 2>/dev/null || NEEDS_SESSION_POOL=1
check_regional_quota \
  ManagedEnvironmentCount \
  "Container Apps managed environment" \
  "$QUOTA_SCOPE" \
  "$NEEDS_ENVIRONMENT"
check_regional_quota \
  SessionPools \
  "Dynamic Sessions pool" \
  "$QUOTA_SCOPE" \
  "$NEEDS_SESSION_POOL"

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
  --file "$REPO_ROOT/dynamic_sessions/office_image/Dockerfile" \
  --build-arg "BUILD_VERSION=$IMAGE_TAG" \
  --no-logs \
  "$REPO_ROOT" \
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
    --probe-yaml "$REPO_ROOT/dynamic_sessions/office_image/probes.yaml" \
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
CALLER_OBJECT_ID=$(get_caller_object_id)
CALLER_PRINCIPAL_TYPE=${CALLER_PRINCIPAL_TYPE:-User}
ensure_role_assignment \
  "Azure ContainerApps Session Executor" \
  "$CALLER_OBJECT_ID" \
  "$CALLER_PRINCIPAL_TYPE" \
  "$POOL_ID"

ENDPOINT=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.poolManagementEndpoint \
  --output tsv)
SESSION_ID="office-$(python3 -c 'import uuid; print(uuid.uuid4())')"

stop_office_session() {
  local identifier=$1
  local token http
  for attempt in $(seq 1 6); do
    token=$(az account get-access-token \
      --resource https://dynamicsessions.io \
      --query accessToken --output tsv 2>/dev/null) || return 1
    http=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
      --request POST \
      "$ENDPOINT/.management/stopSession?api-version=$SESSION_API_VERSION&identifier=$identifier" \
      --header "Authorization: Bearer $token")
    if [[ "$http" == "200" || "$http" == "202" || "$http" == "204" || "$http" == "404" ]]; then
      sleep 5
      http=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        --request POST \
        "$ENDPOINT/.management/getSession?api-version=$SESSION_API_VERSION&identifier=$identifier" \
        --header "Authorization: Bearer $token")
      [[ "$http" == "400" || "$http" == "404" ]] && return 0
    fi
    sleep 5
  done
  return 1
}

trap 'stop_office_session "$SESSION_ID" || log "WARNING: Office validation session 자동 정리에 실패했습니다: $SESSION_ID"' EXIT

http=
for attempt in $(seq 1 30); do
  TOKEN=$(az account get-access-token \
    --resource https://dynamicsessions.io \
    --query accessToken --output tsv)
  http=$(curl --silent --show-error --output "$WORK_DIR/health.json" \
    --write-out '%{http_code}' \
    "$ENDPOINT/health?identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN")
  if [[ "$http" == "200" ]]; then
    release=$(jq -r '.release // "legacy"' "$WORK_DIR/health.json")
    if [[ "$release" == "$IMAGE_TAG" ]]; then
      break
    fi
    log "Allocated session uses release $release; waiting for $IMAGE_TAG."
    stop_office_session "$SESSION_ID" \
      || die "Previous Office release session could not be stopped: $SESSION_ID"
    curl --silent --show-error --output /dev/null \
      --request POST \
      "$ENDPOINT/.management/stopSession?api-version=$SESSION_API_VERSION&identifier=$SESSION_ID" \
      --header "Authorization: ******" || true
    SESSION_ID="office-$(python3 -c 'import uuid; print(uuid.uuid4())')"
    sleep 20
    continue
  fi
  if [[ "$http" == "403" || "$http" == "502" || "$http" == "503" ]]; then
    sleep 20
    continue
  fi
  die "Office health failed with HTTP $http: $(cat "$WORK_DIR/health.json")"
done
[[ "$http" == "200" ]] || die "Office health did not become ready"
jq -e '.status == "ok"' "$WORK_DIR/health.json" >/dev/null
jq -e --arg release "$IMAGE_TAG" '.release == $release' \
  "$WORK_DIR/health.json" >/dev/null

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

JOB_ID=$(jq -r '.jobId' "$WORK_DIR/generate.json")
ORIGINAL_PDF_PATH=$(jq -r \
  '.files[] | select(.name == "report.pdf") | .downloadPath' \
  "$WORK_DIR/generate.json")
ORIGINAL_PDF_HASH=$(jq -r \
  '.files[] | select(.name == "report.pdf") | .sha256' \
  "$WORK_DIR/generate.json")

# 허용 목록 밖 변환은 거부돼야 한다.
http=$(curl --silent --show-error --output "$WORK_DIR/convert-denied.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/convert?identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"source\":\"report.docx\",\"target\":\"exe\"}")
[[ "$http" == "400" ]] \
  || die "Disallowed conversion must return 400 but returned $http"

# 허용된 변환.
http=$(curl --silent --show-error --output "$WORK_DIR/convert.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/convert?identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"source\":\"report.pptx\",\"target\":\"pdf\"}")
expect_2xx "$http" "Office conversion" "$WORK_DIR/convert.json"
converted_path=$(jq -r '.files[0].downloadPath' "$WORK_DIR/convert.json")
converted_hash=$(jq -r '.files[0].sha256' "$WORK_DIR/convert.json")
http=$(curl --silent --show-error --output "$WORK_DIR/report.pptx.pdf" \
  --write-out '%{http_code}' \
  "$ENDPOINT$converted_path?identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN")
expect_2xx "$http" "Converted file download" "$WORK_DIR/report.pptx.pdf"
[[ "$(sha256_file "$WORK_DIR/report.pptx.pdf")" == "$converted_hash" ]] \
  || die "Hash mismatch: report.pptx.pdf"
head -c 4 "$WORK_DIR/report.pptx.pdf" | grep -q '%PDF'

for attempt in $(seq 1 3); do
  TOKEN=$(az account get-access-token \
    --resource https://dynamicsessions.io \
    --query accessToken --output tsv)
  http=$(curl --silent --show-error --output "$WORK_DIR/report.after-convert.pdf" \
    --write-out '%{http_code}' \
    "$ENDPOINT$ORIGINAL_PDF_PATH?identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN")
  [[ "$http" != "403" ]] && break
  sleep 15
done
expect_2xx "$http" "Original report.pdf download" "$WORK_DIR/report.after-convert.pdf"
[[ "$(sha256_file "$WORK_DIR/report.after-convert.pdf")" == "$ORIGINAL_PDF_HASH" ]] \
  || die "Original report.pdf changed during PPTX conversion"

# 허용 목록 밖 편집 operation과 수식 주입은 거부돼야 한다.
http=$(curl --silent --show-error --output "$WORK_DIR/edit-denied.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/edit?identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[{\"op\":\"runShell\",\"cmd\":\"id\"}]}")
[[ "$http" == "400" ]] \
  || die "Disallowed edit operation must return 400 but returned $http"

http=$(curl --silent --show-error --output "$WORK_DIR/edit-formula.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/edit?identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[{\"op\":\"setCell\",\"cell\":\"B2\",\"value\":\"=1+1\"}]}")
[[ "$http" == "400" ]] \
  || die "Formula injection must return 400 but returned $http"

# 허용된 선언적 편집.
http=$(curl --silent --show-error --output "$WORK_DIR/edit.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/edit?identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[
    {\"op\":\"setCell\",\"cell\":\"B2\",\"value\":\"approved-draft\"},
    {\"op\":\"renameSheet\",\"name\":\"Final\"},
    {\"op\":\"replaceText\",\"find\":\"separate isolated pools\",\"replace\":\"검토 완료\"}
  ]}")
expect_2xx "$http" "Office edit" "$WORK_DIR/edit.json"
jq -e '.applied == 3 and (.files | length) == 3' "$WORK_DIR/edit.json" >/dev/null \
  || die "Edit did not apply the expected operations"

for file_name in \
  report.edited.docx \
  report.edited.pptx \
  report.edited.xlsx; do
  download_path=$(jq -r --arg name "$file_name" \
    '.files[] | select(.name == $name) | .downloadPath' "$WORK_DIR/edit.json")
  expected_hash=$(jq -r --arg name "$file_name" \
    '.files[] | select(.name == $name) | .sha256' "$WORK_DIR/edit.json")
  http=$(curl --silent --show-error --output "$WORK_DIR/$file_name" \
    --write-out '%{http_code}' \
    "$ENDPOINT$download_path?identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN")
  expect_2xx "$http" "$file_name download" "$WORK_DIR/$file_name"
  [[ "$(sha256_file "$WORK_DIR/$file_name")" == "$expected_hash" ]] \
    || die "Hash mismatch: $file_name"
  unzip -t "$WORK_DIR/$file_name" >/dev/null
done

unzip -p "$WORK_DIR/report.edited.pptx" 'ppt/slides/slide*.xml' \
  | grep -q '검토 완료' \
  || die "PPTX replaceText was not applied"

# 존재하지 않는 job은 404다.
http=$(curl --silent --show-error --output /dev/null \
  --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/convert?identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"jobId":"deadbeef","source":"report.docx","target":"pdf"}')
[[ "$http" == "404" ]] \
  || die "Missing job must return 404 but returned $http"

curl --fail-with-body --silent --show-error \
  --request POST \
  "$ENDPOINT/.management/getSession?api-version=$SESSION_API_VERSION&identifier=$SESSION_ID" \
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

stop_office_session "$SESSION_ID" \
  || die "Office validation session cleanup failed: $SESSION_ID"
trap - EXIT

cat > "$WORK_DIR/validation.txt" <<EOF
pool=$OFFICE_POOL_NAME
image=$IMAGE
session=$SESSION_ID
formats=docx,pdf,pptx,xlsx
hashes=verified
convert_allowlist=enforced
edit_allowlist=enforced
session_cleanup=verified
EOF

log "Office validation passed."
log "Artifacts: $WORK_DIR"
log "Session: $SESSION_ID"
log "Image: $IMAGE"
