#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

require_base_commands

SUBSCRIPTION_ID=${SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}
RESOURCE_GROUP=${RESOURCE_GROUP:-rg-ai-workspace-sandbox-lab}
LOCATION=${LOCATION:-koreacentral}
PYTHON_POOL_NAME=${PYTHON_POOL_NAME:-ai-workspace-python-sbx}
WORK_DIR="$WORK_ROOT/python"
mkdir -p "$WORK_DIR"

az account set --subscription "$SUBSCRIPTION_ID"
az extension add --name containerapp --upgrade --allow-preview true --yes --output none
az extension add --name quota --upgrade --yes --output none
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.Quota --wait

QUOTA_SCOPE="/subscriptions/$SUBSCRIPTION_ID/providers/Microsoft.App/locations/$LOCATION"
az quota show --resource-name SessionPools --scope "$QUOTA_SCOPE" \
  --query '{limit:properties.limit.value}' --output json
az quota usage show --resource-name SessionPools --scope "$QUOTA_SCOPE" \
  --query '{usage:properties.usages.value}' --output json

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
CALLER_OBJECT_ID=$(az ad signed-in-user show --query id --output tsv)
ensure_role_assignment \
  "Azure ContainerApps Session Executor" \
  "$CALLER_OBJECT_ID" \
  User \
  "$POOL_ID"

ENDPOINT=$(az containerapp sessionpool show \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.poolManagementEndpoint \
  --output tsv)
SESSION_ID="python-$(python3 -c 'import uuid; print(uuid.uuid4())')"

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
    "$ENDPOINT/executions?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
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
    "$ENDPOINT/files?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --form "file=@$WORK_DIR/$local_name;filename=$remote_name")
  expect_2xx "$http" "$remote_name upload" "$response"
done

response="$WORK_DIR/analysis-execution.json"
http=$(curl --silent --show-error --output "$response" --write-out '%{http_code}' \
  --request POST \
  "$ENDPOINT/executions?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"codeInputType":"inline","executionType":"synchronous","code":"exec(compile(open(\"/mnt/data/analyze_sales.py\", encoding=\"utf-8\").read(), \"analyze_sales.py\", \"exec\"))"}')
expect_2xx "$http" "Python analysis" "$response"
jq -e '.status == "Succeeded"' "$response" >/dev/null

for file_name in monthly_sales.png summary.json; do
  temporary_file="$WORK_DIR/$file_name.tmp"
  http=$(curl --silent --show-error --output "$temporary_file" \
    --write-out '%{http_code}' \
    "$ENDPOINT/files/$file_name/content?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
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
  "$ENDPOINT/executions?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
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
  "$ENDPOINT/session?api-version=2025-02-02-preview&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output "$WORK_DIR/session.json"

cat > "$WORK_DIR/validation.txt" <<EOF
pool=$PYTHON_POOL_NAME
session=$SESSION_ID
png_sha256=$PNG_HASH
json_sha256=$JSON_HASH
egress=blocked
EOF

log "Python validation passed."
log "Artifacts: $WORK_DIR"
log "Session: $SESSION_ID"
