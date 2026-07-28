#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

require_base_commands

SUBSCRIPTION_ID=${SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}
RESOURCE_GROUP=${RESOURCE_GROUP:-rg-ai-workspace-dynamic-sessions-lab}
LOCATION=${LOCATION:-koreacentral}
PYTHON_POOL_NAME=${PYTHON_POOL_NAME:-ai-workspace-python-sbx}
WORK_DIR="$WORK_ROOT/dynamic-sessions/python"
mkdir -p "$WORK_DIR"

az account set --subscription "$SUBSCRIPTION_ID"
az extension add --name containerapp --upgrade --allow-preview true --yes --output none
az extension add --name quota --upgrade --yes --output none
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.Quota --wait

QUOTA_SCOPE="/subscriptions/$SUBSCRIPTION_ID/providers/Microsoft.App/locations/$LOCATION"
NEEDS_SESSION_POOL=0
az containerapp sessionpool show \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output none 2>/dev/null || NEEDS_SESSION_POOL=1
check_regional_quota \
  SessionPools \
  "Dynamic Sessions pool" \
  "$QUOTA_SCOPE" \
  "$NEEDS_SESSION_POOL"

az group create --name "$RESOURCE_GROUP" --location "$LOCATION" \
  --tags purpose=ai-workspace-sandbox-lab --output none

if ! az containerapp sessionpool show \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output none 2>/dev/null; then
  az containerapp sessionpool create \
    --name "$PYTHON_POOL_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --container-type PythonLTS \
    --max-sessions 10 \
    --cooldown-period 3600 \
    --network-status EgressDisabled \
    --output none
fi
wait_for_pool "$PYTHON_POOL_NAME" "$RESOURCE_GROUP"
NETWORK_STATUS=$(az containerapp sessionpool show \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.sessionNetworkConfiguration.status \
  --output tsv)
[[ "$NETWORK_STATUS" == "EgressDisabled" ]] \
  || die "Python pool egress must be disabled"

POOL_ID=$(az containerapp sessionpool show \
  --name "$PYTHON_POOL_NAME" \
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
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.poolManagementEndpoint \
  --output tsv)
SESSION_ID="python-$(python3 -c 'import uuid; print(uuid.uuid4())')"

cleanup_main_session() {
  local token
  token=$(az account get-access-token \
    --resource https://dynamicsessions.io \
    --query accessToken --output tsv 2>/dev/null) || {
      log "WARNING: Python validation session token을 얻지 못해 자동 정리를 건너뜁니다."
      return
    }
  curl --silent --show-error --output /dev/null \
    --request DELETE \
    "$ENDPOINT/session?api-version=$SESSION_API_VERSION&identifier=$SESSION_ID" \
    --header "Authorization: Bearer $token" \
    || log "WARNING: Python validation session 자동 정리에 실패했습니다: $SESSION_ID"
}
trap cleanup_main_session EXIT

cat > "$WORK_DIR/sales.csv" <<'CSV'
month,product,amount
2026-01,Notebook,120
2026-01,Pen,80
2026-02,Notebook,150
2026-02,Pen,90
2026-03,Notebook,130
2026-03,Pen,110
CSV

cat > "$WORK_DIR/analyze_sales.py" <<'PY'
import csv
import json
from collections import defaultdict

import matplotlib.pyplot as plt

monthly = defaultdict(float)
products = defaultdict(float)
with open("/mnt/data/sales.csv", newline="", encoding="utf-8") as source:
    for row in csv.DictReader(source):
        amount = float(row["amount"])
        monthly[row["month"]] += amount
        products[row["product"]] += amount

months = sorted(monthly)
plt.plot(months, [monthly[month] for month in months], marker="o")
plt.title("Monthly sales")
plt.xlabel("Month")
plt.ylabel("Sales")
plt.tight_layout()
plt.savefig("/mnt/data/monthly_sales.png")

with open("/mnt/data/summary.json", "w", encoding="utf-8") as output:
    json.dump(
        {
            "monthly_sales": dict(sorted(monthly.items())),
            "top_products": sorted(
                products.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:5],
        },
        output,
        ensure_ascii=False,
        indent=2,
    )
PY

http=
for attempt in $(seq 1 12); do
  TOKEN=$(az account get-access-token \
    --resource https://dynamicsessions.io \
    --query accessToken --output tsv)
  http=$(curl --silent --show-error \
    --output "$WORK_DIR/first-execution.json" \
    --write-out '%{http_code}' \
    --request POST \
    "$ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --header "Content-Type: application/json" \
    --data '{"codeInputType":"inline","executionType":"synchronous","code":"print(\"Python session ready\")"}')
  [[ "$http" == "200" ]] && break
  if [[ "$http" == "403" ]]; then
    sleep 15
    continue
  fi
  die "Initial Python execution failed with HTTP $http: $(cat "$WORK_DIR/first-execution.json")"
done
[[ "$http" == "200" ]] || die "Session Executor role did not become effective"
jq -e '.status == "Succeeded"' "$WORK_DIR/first-execution.json" >/dev/null

for spec in "sales.csv:sales.csv" "analyze_sales.py:analyze_sales.py"; do
  local_name=${spec%%:*}
  remote_name=${spec##*:}
  response="$WORK_DIR/upload-$remote_name.json"
  http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
    --request POST \
    "$ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --form "file=@$WORK_DIR/$local_name;filename=$remote_name")
  expect_2xx "$http" "$remote_name upload" "$response"
done

response="$WORK_DIR/analysis-execution.json"
http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"codeInputType":"inline","executionType":"synchronous","code":"exec(compile(open(\"/mnt/data/analyze_sales.py\", encoding=\"utf-8\").read(), \"analyze_sales.py\", \"exec\"))"}')
expect_2xx "$http" "Python analysis" "$response"
jq -e '.status == "Succeeded"' "$response" >/dev/null

for file_name in monthly_sales.png summary.json; do
  temporary_file="$WORK_DIR/$file_name.tmp"
  http=$(curl --silent --show-error --output "$temporary_file" \
    --write-out '%{http_code}' \
    "$ENDPOINT/files/$file_name/content?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN")
  expect_2xx "$http" "$file_name download" "$temporary_file"
  mv "$temporary_file" "$WORK_DIR/$file_name"
  test -s "$WORK_DIR/$file_name"
done

jq -e '
  .monthly_sales["2026-01"] == 200
  and .monthly_sales["2026-02"] == 240
  and .monthly_sales["2026-03"] == 240
' "$WORK_DIR/summary.json" >/dev/null

PNG_HASH=$(sha256_file "$WORK_DIR/monthly_sales.png")
JSON_HASH=$(sha256_file "$WORK_DIR/summary.json")

response="$WORK_DIR/egress-validation.json"
http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"codeInputType":"inline","executionType":"synchronous","code":"import urllib.request\ntry:\n    urllib.request.urlopen(\"https://example.com\", timeout=5)\n    print(\"UNEXPECTED_EGRESS_ALLOWED\")\nexcept Exception as exc:\n    print(\"EGRESS_BLOCKED\", type(exc).__name__)"}')
expect_2xx "$http" "Egress validation" "$response"
jq -e '
  .status == "Succeeded"
  and (.result.stdout | contains("EGRESS_BLOCKED"))
  and ((.result.stdout | contains("UNEXPECTED_EGRESS_ALLOWED")) | not)
' "$response" >/dev/null

curl --fail-with-body --silent --show-error \
  "$ENDPOINT/session?api-version=$SESSION_API_VERSION&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output "$WORK_DIR/session.json"

# 사전 설치 라이브러리 목록. EgressDisabled에서는 pip install을 할 수 없다.
response="$WORK_DIR/preinstalled.json"
http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"codeInputType":"inline","executionType":"synchronous","code":"import importlib, platform\nprint(\"python\", platform.python_version())\nfor name in [\"pandas\",\"numpy\",\"matplotlib\",\"scipy\",\"sklearn\",\"openpyxl\",\"docx\",\"pptx\",\"reportlab\"]:\n    try:\n        module = importlib.import_module(name)\n        print(name, getattr(module, \"__version__\", \"unknown\"))\n    except ImportError:\n        print(name, \"MISSING\")"}')
expect_2xx "$http" "Preinstalled library inventory" "$response"
jq -e '
  .status == "Succeeded"
  and ((.result.stdout | contains("MISSING")) | not)
' "$response" >/dev/null

# 세션 간 파일 격리. 두 번째 session에서 첫 session의 파일이 보이면 안 된다.
SECOND_SESSION_ID="python-$(python3 -c 'import uuid; print(uuid.uuid4())')"
response="$WORK_DIR/isolation-list.json"
http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
  "$ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$SECOND_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN")
expect_2xx "$http" "Second session file list" "$response"
jq -e '(.value | length) == 0' "$response" >/dev/null \
  || die "Session isolation failed: second session can see files"

http=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  "$ENDPOINT/files/sales.csv/content?api-version=$PYTHON_API_VERSION&identifier=$SECOND_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN")
[[ "$http" == "404" ]] \
  || die "Cross-session download must return 404 but returned $http"

curl --silent --show-error --output /dev/null \
  --request DELETE \
  "$ENDPOINT/session?api-version=$SESSION_API_VERSION&identifier=$SECOND_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"

# 오류 -> 코드 수정 -> 재실행 루프.
response="$WORK_DIR/failed-execution.json"
http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"codeInputType":"inline","executionType":"synchronous","code":"import csv\nwith open(\"/mnt/data/sales.csv\", newline=\"\", encoding=\"utf-8\") as source:\n    print(sum(float(row[\"sales_amount\"]) for row in csv.DictReader(source)))"}')
expect_2xx "$http" "Deliberate failure" "$response"
jq -e '
  .status == "Failed"
  and (.result.stderr | contains("KeyError"))
' "$response" >/dev/null \
  || die "Expected a KeyError failure to demonstrate the retry loop"

response="$WORK_DIR/fixed-execution.json"
http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"codeInputType":"inline","executionType":"synchronous","code":"import csv\nwith open(\"/mnt/data/sales.csv\", newline=\"\", encoding=\"utf-8\") as source:\n    rows = list(csv.DictReader(source))\nprint(\"total:\", sum(float(row[\"amount\"]) for row in rows))"}')
expect_2xx "$http" "Corrected execution" "$response"
jq -e '
  .status == "Succeeded"
  and (.result.stdout | contains("total: 680.0"))
' "$response" >/dev/null \
  || die "Corrected execution did not produce the expected total"

# 실행 시간 한도. 기본은 건너뛰고 RUN_LIMIT_TESTS=yes일 때만 약 4분을 소모한다.
if [[ "${RUN_LIMIT_TESTS:-no}" == "yes" ]]; then
  response="$WORK_DIR/timeout-validation.json"
  http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
    --max-time 600 \
    --request POST \
    "$ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --header "Content-Type: application/json" \
    --data '{"codeInputType":"inline","executionType":"synchronous","code":"import time\nfor _ in range(300):\n    time.sleep(1)\nprint(\"NO_TIMEOUT\")"}')
  expect_2xx "$http" "Execution timeout probe" "$response"
  jq -e '
    .status == "Failed"
    and ((.result.stdout // "") | contains("NO_TIMEOUT") | not)
  ' "$response" >/dev/null \
    || die "Execution time limit was not enforced"

  response="$WORK_DIR/memory-validation.json"
  http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
    --max-time 600 \
    --request POST \
    "$ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --header "Content-Type: application/json" \
    --data '{"codeInputType":"inline","executionType":"synchronous","code":"blocks = []\nwhile True:\n    blocks.append(bytearray(100 * 1024 * 1024))"}')
  expect_2xx "$http" "Memory limit probe" "$response"
  jq -e '.status == "Failed"' "$response" >/dev/null \
    || die "Memory limit was not enforced"
  LIMITS_VERIFIED=yes
else
  log "Skipping execution limit probes. Set RUN_LIMIT_TESTS=yes to run them."
  LIMITS_VERIFIED=skipped
fi

# Session 삭제와 임시 파일 자동 정리.
CLEANUP_SESSION_ID="python-$(python3 -c 'import uuid; print(uuid.uuid4())')"
printf 'a,b\n1,2\n' > "$WORK_DIR/cleanup-probe.csv"
http=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --form "file=@$WORK_DIR/cleanup-probe.csv;filename=cleanup-probe.csv")
expect_2xx "$http" "Cleanup probe upload" /dev/null

response="$WORK_DIR/before-cleanup.json"
curl --silent --show-error --output "$response" \
  "$ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
jq -e '(.value | length) == 1' "$response" >/dev/null \
  || die "Cleanup probe file was not uploaded"

http=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --request DELETE \
  "$ENDPOINT/session?api-version=$SESSION_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN")
expect_2xx "$http" "Delete session" /dev/null

response="$WORK_DIR/after-cleanup.json"
curl --silent --show-error --output "$response" \
  "$ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
jq -e '(.value | length) == 0' "$response" >/dev/null \
  || die "Session files survived deletion"

http=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  "$ENDPOINT/files/cleanup-probe.csv/content?api-version=$PYTHON_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN")
[[ "$http" == "404" ]] \
  || die "Deleted session file download must return 404 but returned $http"

# 삭제 후 files 조회는 같은 identifier에 빈 session을 다시 할당할 수 있다.
# 검증을 위해 재생성된 빈 session도 즉시 삭제한다.
http=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --request DELETE \
  "$ENDPOINT/session?api-version=$SESSION_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN")
expect_2xx "$http" "Delete recreated cleanup session" /dev/null

cat > "$WORK_DIR/validation.txt" <<EOF
pool=$PYTHON_POOL_NAME
session=$SESSION_ID
png_sha256=$PNG_HASH
json_sha256=$JSON_HASH
egress=blocked
session_isolation=verified
error_retry_loop=verified
session_cleanup=verified
execution_limits=$LIMITS_VERIFIED
EOF

log "Python validation passed."
log "Artifacts: $WORK_DIR"
log "Session: $SESSION_ID"
