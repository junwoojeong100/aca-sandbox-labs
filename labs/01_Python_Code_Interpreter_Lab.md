# 실습 1: Python Code Interpreter Session Pool

## 목표

Azure Container Apps Dynamic Sessions의 PythonLTS pool에서 다음을 실제 검증한다.

- 격리된 Python 코드 실행
- CSV 파일 업로드
- 데이터 분석과 PNG·JSON 생성
- 결과 파일 목록 조회와 다운로드
- `EgressDisabled` 외부 통신 차단
- session 정보와 lifecycle 확인

예상 시간은 45~60분이다.

## 1. 사전 조건

- Bash 또는 Azure Cloud Shell
- Azure CLI 2.79.0 이상
- `curl`, `jq`, Python 3, `unzip`, `file`
- `shasum` 또는 `sha256sum`
- 대상 subscription의 Contributor
- role assignment를 위한 Owner 또는 User Access Administrator
- Dynamic Sessions 지원 리전과 SessionPools quota

로컬 Bash에서는 먼저 `az login`을 실행한다. Cloud Shell은 이미 로그인돼 있으므로 생략한다.

### 권장 Fast Path

repository root에서 다음 명령을 실행하면 사전 조건 확인부터 분석, egress와 hash 검증까지 자동 수행하고 결과를 `.work/python/`에 저장한다.

```bash
bash scripts/check-prereqs.sh
bash scripts/python-lab.sh
```

아래 절은 자동 스크립트가 수행하는 세부 명령을 설명한다.

## 2. 변수 설정

```bash
export SUBSCRIPTION_ID="<SUBSCRIPTION_ID>"
export RESOURCE_GROUP="rg-ai-workspace-sandbox-lab"
export LOCATION="koreacentral"
export PYTHON_POOL_NAME="ai-workspace-python-sbx"
export LAB_WORK_DIR="$PWD/.work/python-manual"

az account set --subscription "$SUBSCRIPTION_ID"
az account show --query '{subscription:id,user:user.name}' --output json
mkdir -p "$LAB_WORK_DIR"
cd "$LAB_WORK_DIR"
```

## 3. CLI와 provider 준비

```bash
az extension add \
  --name containerapp \
  --upgrade \
  --allow-preview true \
  --yes

az extension add \
  --name quota \
  --upgrade \
  --yes

az provider register \
  --namespace Microsoft.App \
  --wait

az provider register \
  --namespace Microsoft.Quota \
  --wait
```

## 4. 지원 리전과 quota 확인

[지원 리전](https://learn.microsoft.com/azure/container-apps/sessions#supported-regions)에서 `LOCATION`을 확인한다.

```bash
export QUOTA_SCOPE="/subscriptions/$SUBSCRIPTION_ID/providers/Microsoft.App/locations/$LOCATION"

az quota list \
  --scope "$QUOTA_SCOPE" \
  --output table

az quota usage list \
  --scope "$QUOTA_SCOPE" \
  --output table

az quota show \
  --resource-name SessionPools \
  --scope "$QUOTA_SCOPE" \
  --query '{limit:properties.limit.value}' \
  --output json

az quota usage show \
  --resource-name SessionPools \
  --scope "$QUOTA_SCOPE" \
  --query '{usage:properties.usages.value}' \
  --output json
```

`BadRequest`가 나오면 무제한으로 해석하지 말고 Azure service limits와 Portal의 My quotas를 확인한다.

## 5. Resource Group과 Python pool 생성

```bash
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --tags purpose=ai-workspace-sandbox-lab \
  --output table

az containerapp sessionpool create \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --container-type PythonLTS \
  --max-sessions 10 \
  --cooldown-period 3600 \
  --network-status EgressDisabled \
  --output none
```

생성 결과를 확인한다.

```bash
az containerapp sessionpool show \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{
    state:properties.provisioningState,
    endpoint:properties.poolManagementEndpoint,
    network:properties.sessionNetworkConfiguration.status,
    maxSessions:properties.scaleConfiguration.maxConcurrentSessions,
    cooldown:properties.dynamicPoolConfiguration.lifecycleConfiguration.cooldownPeriodInSeconds
  }' \
  --output yaml
```

통과 기준:

- `state: Succeeded`
- endpoint가 비어 있지 않음
- `network: EgressDisabled`
- `maxSessions: 10`
- `cooldown: 3600`

## 6. Session Executor 역할

pool을 생성한 사용자는 상위 범위의 Contributor 권한을 이미 가지고 있어야 한다. 데이터 평면 API 호출을 위해 pool 범위에 Session Executor를 추가한다.

```bash
export PYTHON_POOL_ID=$(az containerapp sessionpool show \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query id \
  --output tsv)

export CALLER_OBJECT_ID=$(az ad signed-in-user show \
  --query id \
  --output tsv)

az role assignment create \
  --role "Azure ContainerApps Session Executor" \
  --assignee-object-id "$CALLER_OBJECT_ID" \
  --assignee-principal-type User \
  --scope "$PYTHON_POOL_ID"
```

역할 전파는 수 분 걸릴 수 있다. API가 403을 반환하면 30~60초 기다린 뒤 token을 다시 발급한다.

## 7. Token, endpoint와 identifier

```bash
export TOKEN=$(az account get-access-token \
  --resource https://dynamicsessions.io \
  --query accessToken \
  --output tsv)

export PYTHON_ENDPOINT=$(az containerapp sessionpool show \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.poolManagementEndpoint \
  --output tsv)

export PYTHON_SESSION_ID="python-$(uuidgen | tr '[:upper:]' '[:lower:]')"
```

이 값들은 실습 터미널에서만 사용한다. 실제 AI Workspace에서는 backend Managed Identity와 Session Broker가 관리하며 browser나 사용자에게 전달하지 않는다.

## 8. 첫 Python 실행

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=2025-10-02-preview&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "print(\"AI Workspace Python sandbox validation passed\")"
  }' \
  --output first-execution.json \
  --write-out '\nexecute HTTP %{http_code}\n'

cat first-execution.json
```

통과 기준:

- HTTP 200
- `status`가 `Succeeded`
- `result.stdout`에 validation 문구 포함

> 2026-07-24 한국 중부의 `2025-10-02-preview` endpoint에서 execution 속성은 JSON 최상위에 있어야 했다. `properties`로 감싸면 `SessionPropertiesMissing`이 발생했다.

## 9. 샘플 CSV와 분석 코드

```bash
cat > sales.csv <<'CSV'
month,product,amount
2026-01,Notebook,120
2026-01,Pen,80
2026-02,Notebook,150
2026-02,Pen,90
2026-03,Notebook,130
2026-03,Pen,110
CSV

cat > analyze_sales.py <<'PY'
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
```

## 10. 파일 업로드

```bash
for SPEC in "sales.csv:sales.csv" "analyze_sales.py:analyze_sales.py"; do
  LOCAL_NAME=${SPEC%%:*}
  REMOTE_NAME=${SPEC##*:}

  curl --fail-with-body --silent --show-error \
    --request POST \
    "$PYTHON_ENDPOINT/files?api-version=2025-10-02-preview&identifier=$PYTHON_SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --form "file=@$LOCAL_NAME;filename=$REMOTE_NAME" \
    --output /dev/null \
    --write-out "$REMOTE_NAME upload HTTP %{http_code}\n"
done
```

각 upload가 HTTP 200을 반환해야 한다.

## 11. 분석 실행

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=2025-10-02-preview&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "exec(compile(open(\"/mnt/data/analyze_sales.py\", encoding=\"utf-8\").read(), \"analyze_sales.py\", \"exec\"))"
  }' \
  --output analysis-execution.json \
  --write-out '\nanalysis HTTP %{http_code}\n'

cat analysis-execution.json
```

HTTP 200과 `status: Succeeded`를 확인한다.

## 12. 결과 목록과 다운로드

```bash
curl --fail-with-body --silent --show-error \
  "$PYTHON_ENDPOINT/files?api-version=2025-10-02-preview&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output session-files.json \
  --write-out 'list files HTTP %{http_code}\n'

cat session-files.json

for FILE in monthly_sales.png summary.json; do
  curl --fail-with-body --silent --show-error \
    --output "$FILE" \
    "$PYTHON_ENDPOINT/files/$FILE/content?api-version=2025-10-02-preview&identifier=$PYTHON_SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --write-out "$FILE download HTTP %{http_code}\n"
done

test -s monthly_sales.png
test -s summary.json
cat summary.json

if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 monthly_sales.png summary.json
else
  sha256sum monthly_sales.png summary.json
fi
```

예상 월별 합계:

```json
{
  "2026-01": 200.0,
  "2026-02": 240.0,
  "2026-03": 240.0
}
```

## 13. Egress 차단 확인

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=2025-10-02-preview&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "import urllib.request\ntry:\n    urllib.request.urlopen(\"https://example.com\", timeout=5)\n    print(\"UNEXPECTED_EGRESS_ALLOWED\")\nexcept Exception as exc:\n    print(\"EGRESS_BLOCKED\", type(exc).__name__)"
  }' \
  --output egress-validation.json

cat egress-validation.json
```

`result.stdout`에 `EGRESS_BLOCKED`가 있어야 하고 `UNEXPECTED_EGRESS_ALLOWED`는 없어야 한다.

## 14. Session 정보

```bash
curl --fail-with-body --silent --show-error \
  "$PYTHON_ENDPOINT/session?api-version=2025-02-02-preview&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
```

실제 응답에서는 `identifier`, `createdAt`, `lastAccessedAt`, `expireAt`을 확인할 수 있다. Preview API의 field 이름은 변경될 수 있다.

## 15. 문제 해결

| 증상 | 조치 |
| --- | --- |
| `QuotaExceeded` | quota 증가 또는 다른 지원 리전 |
| HTTP 401 | token audience 확인 후 재발급 |
| HTTP 403 | role scope 확인, 전파 대기 후 token 재발급 |
| HTTP 404 | endpoint, pool, Resource Group 확인 |
| HTTP 413 | 128MB 이하로 분할 |
| `SessionPropertiesMissing` | execution 속성의 `properties` 래퍼 제거 |
| `SessionWithIdentifierNotFound` | 새 identifier로 업로드부터 재실행 |
| Python import 오류 | library를 포함한 Custom Container 검토 |

## 16. 선택적 정리

> 현재 리소스를 보존해야 하면 이 절을 실행하지 않는다.
> Office 실습에서 같은 Resource Group을 사용할 예정이면 Python pool만 선택적으로 삭제하고 Resource Group은 유지한다.

Session만 즉시 삭제:

```bash
curl --fail-with-body --silent --show-error \
  --request DELETE \
  "$PYTHON_ENDPOINT/session?api-version=2025-02-02-preview&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output /dev/null \
  --write-out 'delete session HTTP %{http_code}\n'
```

Pool 삭제:

```bash
az containerapp sessionpool delete \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --yes
```

전용 Resource Group 삭제:

```bash
az group delete \
  --name "$RESOURCE_GROUP" \
  --yes \
  --no-wait
```

## 17. 실제 검증 기록

2026-07-24 한국 중부 리전:

- PythonLTS pool `Succeeded`
- code execution HTTP 200
- CSV와 Python 파일 upload HTTP 200
- PNG·JSON 생성과 download HTTP 200
- PNG 640x480 확인
- JSON 값과 SHA-256 확인
- egress 요청 `URLError`로 차단
