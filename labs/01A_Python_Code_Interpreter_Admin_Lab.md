# 실습 1A: Python Code Interpreter 및 LLM Backend - 관리자

## 목표

Azure Container Apps Dynamic Sessions의 PythonLTS pool에서 다음을 실제 검증한다.

- 격리된 Python 코드 실행
- 사전 설치 라이브러리 확인
- CSV 파일 업로드
- 데이터 분석과 PNG·JSON 생성
- 결과 파일 목록 조회와 다운로드
- **세션 간 파일 격리**
- **실행 오류 분석, 코드 수정, 재실행**
- `EgressDisabled` 외부 통신 차단
- **실행 시간과 메모리 한도**
- **session 삭제와 임시 파일 자동 정리**

예상 시간은 Python Sandbox 60~75분, LLM backend 구성 20~40분이다.

이 문서는 관리자가 Pool, RBAC, network, quota, 실행 한도와 LLM backend를 구성·검증하는 절차다. 자연어 요청과 승인 중심의 사용자 실습은 [실습 1B](01B_Python_Code_Interpreter_User_Lab.md)에서 수행한다.

Fast Path와 수동 절차는 같은 리소스를 만드는 **대체 경로**다. 처음 수행한다면 Fast Path만 실행하고, 실패 원인을 찾거나 개별 Azure 명령을 학습할 때만 2~15절의 수동 절차를 사용한다.

## 1. 사전 조건

- Bash 또는 Azure Cloud Shell
- Azure CLI 2.79.0 이상
- `curl`, `jq`, Python 3, `unzip`, `file`
- `shasum` 또는 `sha256sum`
- 대상 subscription의 Contributor
- role assignment를 위한 Owner 또는 User Access Administrator
- Dynamic Sessions 지원 리전과 SessionPools quota

로컬 Bash에서는 먼저 `az login`을 실행한다. Cloud Shell은 이미 로그인돼 있으므로 생략한다.

현재 subscription과 필수 도구를 확인한다.

```bash
az account show --query '{name:name,id:id,user:user.name}' --output table
command -v az curl jq python3 unzip file
```

### 권장 Fast Path

repository root에서 다음 명령을 실행한다. 첫 번째 스크립트는 로컬 도구, Azure CLI 버전과 로그인만 확인한다. 두 번째 스크립트가 extension, provider, quota, 리소스 생성과 RBAC 역할 할당을 처리하고 분석, egress와 hash 검증 결과를 `.work/python/`에 저장한다.

```bash
bash scripts/check-prereqs.sh
bash scripts/python-lab.sh
```

기본값은 현재 `az` subscription, `koreacentral`, `rg-ai-workspace-sandbox-lab`, `ai-workspace-python-sbx`다. 다른 값을 쓰려면 2절의 환경 변수를 명령 실행 전에 설정한다. quota가 없거나 역할을 할당할 권한이 부족하면 해당 Azure 명령에서 중단된다.

아래 절은 자동 스크립트가 수행하는 세부 명령을 설명한다.

실행 시간과 메모리 한도 검증은 약 4분이 걸리므로 기본적으로 건너뛴다. 포함하려면 다음과 같이 실행한다.

```bash
RUN_LIMIT_TESTS=yes bash scripts/python-lab.sh
```

Sandbox Fast Path가 끝나면 16절에서 실제 LLM을 연결하고, 이어서 [실습 1B](01B_Python_Code_Interpreter_User_Lab.md)에서 자연어 요청부터 재시도와 승인까지의 사용자 흐름을 검증한다.

## 2. 변수 설정

```bash
export SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}"
export RESOURCE_GROUP="rg-ai-workspace-sandbox-lab"
export LOCATION="koreacentral"
export PYTHON_POOL_NAME="ai-workspace-python-sbx"
export PYTHON_API_VERSION="${PYTHON_API_VERSION:-2025-10-02-preview}"
export SESSION_API_VERSION="${SESSION_API_VERSION:-2025-02-02-preview}"
export REPO_ROOT="$PWD"
export LAB_WORK_DIR="$PWD/.work/python-manual"

az account set --subscription "$SUBSCRIPTION_ID"
az account show --query '{subscription:id,user:user.name}' --output json
mkdir -p "$LAB_WORK_DIR"
cd "$LAB_WORK_DIR"
```

이 시점부터 15절까지의 수동 명령은 repository root가 아니라 `$LAB_WORK_DIR`에서 실행한다.

두 API version은 이 repository에서 검증한 값이다. Preview API 오류가 발생해도 임의로 바꾸지 말고 [공식 data-plane API 문서](https://learn.microsoft.com/rest/api/containerapps/)에서 현재 endpoint와 request shape를 확인한 뒤 환경 변수만 재정의한다.

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

새 Python pool을 만들려면 `SessionPools`의 `limit - usage`가 최소 1이어야 한다. 이미 같은 pool이 존재해 재사용하는 경우에는 추가 quota가 필요하지 않다.

CLI가 값을 반환하지 않거나 사용 가능 수량이 0이면 [Azure Portal My quotas](https://portal.azure.com/#view/Microsoft_Azure_Capacity/QuotaMenuBlade/~/myQuotas)에서 다음과 같이 확인한다.

1. Provider를 **Azure Container Apps**로 선택한다.
2. `LOCATION`과 같은 region을 선택한다.
3. `Session pools` 현재 사용량과 limit을 확인한다.
4. 부족하면 quota 증가를 요청하고 승인 후 다시 실행한다.

Regional quota 증가는 빠르게 승인될 수도 있지만 지원 검토가 필요하면 며칠 걸릴 수 있다.

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

`Contributor`는 resource를 만들 수 있지만 role assignment 권한은 없다. 다음 명령이 `AuthorizationFailed`로 실패하면 `Owner` 또는 `User Access Administrator`에게 이 절의 역할 할당을 요청한다.

```bash
export PYTHON_POOL_ID=$(az containerapp sessionpool show \
  --name "$PYTHON_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query id \
  --output tsv)

if [[ -z "${CALLER_OBJECT_ID:-}" ]]; then
  export CALLER_OBJECT_ID=$(az ad signed-in-user show \
    --query id \
    --output tsv)
  export CALLER_PRINCIPAL_TYPE="User"
fi
export CALLER_PRINCIPAL_TYPE="${CALLER_PRINCIPAL_TYPE:-User}"

az role assignment create \
  --role "Azure ContainerApps Session Executor" \
  --assignee-object-id "$CALLER_OBJECT_ID" \
  --assignee-principal-type "$CALLER_PRINCIPAL_TYPE" \
  --scope "$PYTHON_POOL_ID"
```

Service Principal로 실행한다면 미리 `CALLER_OBJECT_ID`를 principal object ID로, `CALLER_PRINCIPAL_TYPE=ServicePrincipal`로 설정한다.

역할 할당을 확인한다.

```bash
az role assignment list \
  --assignee "$CALLER_OBJECT_ID" \
  --scope "$PYTHON_POOL_ID" \
  --query "[?roleDefinitionName=='Azure ContainerApps Session Executor'].{role:roleDefinitionName,scope:scope}" \
  --output table
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

이 단계는 session에 SSH나 웹 terminal로 들어가는 것이 아니다. 이후 `curl` 요청이 REST API를 호출하며, 실습 운영자는 terminal에서 실행 상태·`stdout`·`stderr` JSON과 다운로드 파일을 확인한다. Azure Portal은 pool 상태와 metrics를 보여주지만 session 내부 화면은 제공하지 않는다.

## 8. 첫 Python 실행

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
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

> 2026-07-24 한국 중부의 기본 `PYTHON_API_VERSION` endpoint에서 execution 속성은 JSON 최상위에 있어야 했다. `properties`로 감싸면 `SessionPropertiesMissing`이 발생했다.

## 8.1 사전 설치 라이브러리 확인

`EgressDisabled` pool에서는 `pip install`을 할 수 없다. 어떤 라이브러리를 쓸 수 있는지 먼저 확인한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "import importlib, platform\nprint(\"python\", platform.python_version())\nfor name in [\"pandas\",\"numpy\",\"matplotlib\",\"scipy\",\"sklearn\",\"openpyxl\",\"docx\",\"pptx\",\"reportlab\",\"requests\"]:\n    try:\n        module = importlib.import_module(name)\n        print(name, getattr(module, \"__version__\", \"unknown\"))\n    except ImportError:\n        print(name, \"MISSING\")"
  }' \
  --output preinstalled.json

cat preinstalled.json
```

2026-07-25 한국 중부 `PythonLTS` pool의 실제 확인 결과다.

| 라이브러리 | 버전 |
| --- | --- |
| Python | 3.12.7 |
| pandas | 2.2.2 |
| numpy | 1.26.4 |
| matplotlib | 3.8.4 |
| scipy | 1.13.1 |
| scikit-learn | 1.5.1 |
| statsmodels | 0.14.6 |
| seaborn | 0.13.2 |
| sympy | 1.14.0 |
| pyarrow | 16.1.0 |
| networkx | 3.3 |
| openpyxl | 3.1.5 |
| python-docx | 1.2.0 |
| python-pptx | 1.0.2 |
| XlsxWriter | 3.2.9 |
| reportlab | 4.4.6 |
| Pillow | 11.3.0 |
| lxml | 6.1.1 |
| beautifulsoup4 | 4.12.3 |
| tabulate | 0.9.0 |

> 중요: `python-docx`, `python-pptx`, `openpyxl`은 **Python pool에도 이미 있다.** 따라서 단순 DOCX·XLSX·PPTX **생성**만 필요하면 Custom Container 없이도 가능하다.
> Custom Container가 필요한 이유는 **LibreOffice 기반 PDF 변환, Pandoc 변환, CJK 폰트 고정, 도구 버전 고정**이다. 실습 2에서 이 경계를 다룬다.

> `requests`는 설치돼 있지만 `EgressDisabled`에서 외부 호출은 실패한다. 라이브러리 존재와 network 허용은 별개다.

> 이 목록은 플랫폼 image가 갱신되면 바뀐다. Production에서는 이 확인 코드를 회귀 테스트에 넣고, 특정 버전을 고정해야 하면 Custom Container를 쓴다.


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
    "$PYTHON_ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
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
  "$PYTHON_ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
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
  "$PYTHON_ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output session-files.json \
  --write-out 'list files HTTP %{http_code}\n'

cat session-files.json

for FILE in monthly_sales.png summary.json; do
  curl --fail-with-body --silent --show-error \
    --output "$FILE" \
    "$PYTHON_ENDPOINT/files/$FILE/content?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
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
  "$PYTHON_ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
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

## 13.1 세션 간 격리 확인

"작업별 독립 세션" 요건을 직접 증명한다. 새 identifier로 두 번째 session을 만들고, 첫 session의 파일이 보이지 않는지 확인한다.

```bash
export SECOND_SESSION_ID="python-$(uuidgen | tr '[:upper:]' '[:lower:]')"

curl --fail-with-body --silent --show-error \
  "$PYTHON_ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$SECOND_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output second-session-files.json \
  --write-out 'second session list HTTP %{http_code}\n'

cat second-session-files.json

curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$SECOND_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "import os\nprint(\"files:\", sorted(os.listdir(\"/mnt/data\")))"
  }' \
  --output second-session-listdir.json

cat second-session-listdir.json
```

첫 session의 파일을 두 번째 session에서 직접 내려받아 본다.

```bash
curl --silent \
  --output /dev/null \
  --write-out 'cross-session download HTTP %{http_code}\n' \
  "$PYTHON_ENDPOINT/files/sales.csv/content?api-version=$PYTHON_API_VERSION&identifier=$SECOND_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
```

통과 기준:

- 두 번째 session의 파일 목록 `value`가 빈 배열
- `os.listdir("/mnt/data")` 결과가 `[]`
- 교차 다운로드가 HTTP **404**

2026-07-25 한국 중부 실제 확인 결과다.

| 확인 | 결과 |
| --- | --- |
| session A `files` 목록 | `sales.csv` 존재 |
| session B `files` 목록 | `{"value": []}` |
| session B `os.listdir("/mnt/data")` | `[]` |
| session B에서 `sales.csv` 다운로드 | HTTP 404 |

각 session은 Hyper-V로 격리되며 `/mnt/data`도 공유되지 않는다. identifier를 아는 것 자체가 접근 권한이므로, identifier는 backend에서만 생성·보관하고 사용자에게 노출하지 않는다.

두 번째 session은 바로 정리한다.

```bash
curl --fail-with-body --silent --show-error \
  --request DELETE \
  "$PYTHON_ENDPOINT/session?api-version=$SESSION_API_VERSION&identifier=$SECOND_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output /dev/null \
  --write-out 'delete second session HTTP %{http_code}\n'
```

## 13.2 오류 발생, 코드 수정, 재실행

"실행 오류 발생 시 코드 수정과 재실행" 요건을 검증한다. 존재하지 않는 열을 참조해 일부러 실패시킨다.

```bash
curl --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "import csv\nwith open(\"/mnt/data/sales.csv\", newline=\"\", encoding=\"utf-8\") as source:\n    total = sum(float(row[\"sales_amount\"]) for row in csv.DictReader(source))\nprint(total)"
  }' \
  --output failed-execution.json

jq '{status: .status, stderr: .result.stderr}' failed-execution.json
```

`status`가 `Failed`이고 `result.stderr`에 `KeyError: 'sales_amount'`가 나온다.

이제 Agent가 하는 일을 손으로 따라한다. 오류에서 열 이름을 확인하고 코드를 고쳐 다시 실행한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "import csv\nwith open(\"/mnt/data/sales.csv\", newline=\"\", encoding=\"utf-8\") as source:\n    rows = list(csv.DictReader(source))\nprint(\"columns:\", list(rows[0]))\nprint(\"total:\", sum(float(row[\"amount\"]) for row in rows))"
  }' \
  --output fixed-execution.json

jq '{status: .status, stdout: .result.stdout}' fixed-execution.json
```

통과 기준:

- 첫 실행 `status: Failed`, `stderr`에 `KeyError`
- 두 번째 실행 `status: Succeeded`, `total: 680.0`
- 같은 session identifier에서 상태(`/mnt/data`의 `sales.csv`)가 유지되므로 재업로드가 필요 없다

운영 시 주의사항:

- 재실행은 코드 hash가 바뀐 경우에만 한다. 같은 코드를 반복 실행하지 않는다.
- 재시도는 기본 2회로 제한하고, 동일 오류가 반복되면 사용자에게 오류 요약을 돌려준다.
- Agent에 전달하는 `stderr`에서 내부 경로, 다른 tenant 데이터, 비밀 정보를 제거한다.
- HTTP transport 재시도(429·5xx)와 코드 오류 재시도를 분리한다. 코드 오류를 network retry로 숨기지 않는다.

이 루프의 자동화 구현은 [실습 1B](01B_Python_Code_Interpreter_User_Lab.md)에서 다룬다.

## 13.3 실행 시간과 메모리 한도

"실행 시간, CPU·메모리 제한" 요건을 확인한다. 이 절은 약 4분이 걸린다.

```bash
time curl --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "import time\nfor _ in range(300):\n    time.sleep(1)\nprint(\"NO_TIMEOUT\")"
  }' \
  --output timeout-validation.json \
  --write-out '\ntimeout test HTTP %{http_code}\n'

cat timeout-validation.json
```

메모리 한도도 같은 방식으로 확인한다.

```bash
curl --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/executions?api-version=$PYTHON_API_VERSION&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "blocks = []\nwhile True:\n    blocks.append(bytearray(100 * 1024 * 1024))\n    print(len(blocks) * 100, \"MB\", flush=True)"
  }' \
  --output memory-validation.json

cat memory-validation.json
```

통과 기준:

- 실행이 `NO_TIMEOUT`을 출력하지 않는다. 220초 한도에서 중단된다.
- 메모리 할당이 무한히 늘어나지 않고 실패로 끝난다.
- 두 경우 모두 session pool과 다른 session은 영향을 받지 않는다.

2026-07-25 한국 중부 실제 응답이다.

| 테스트 | `status` | `result.stderr` | 경과 |
| --- | --- | --- | --- |
| 300초 sleep | `Failed` | `Request timed out waiting for code execution to complete` | 221.5초 |
| 100MB씩 무한 할당 | `Failed` | `Execution aborted` | 즉시 |

한도는 pool 설정이 아니라 플랫폼이 강제한다. HTTP는 200이지만 실행 `status`가 `Failed`이므로, 호출부는 HTTP 코드만 보지 말고 반드시 `status`와 `result.stderr`를 함께 확인해야 한다.

220초를 넘을 가능성이 있는 작업은 정책 엔진에서 미리 분류해 별도 비동기 compute로 보낸다. 폭주하는 코드가 session을 죽여도 다른 session과 pool은 영향을 받지 않는다.

## 14. Session 정보

```bash
curl --fail-with-body --silent --show-error \
  "$PYTHON_ENDPOINT/session?api-version=$SESSION_API_VERSION&identifier=$PYTHON_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
```

실제 응답에서는 `identifier`, `createdAt`, `lastAccessedAt`, `expireAt`을 확인할 수 있다. Preview API의 field 이름은 변경될 수 있다.

## 14.1 Session 종료와 임시 파일 자동 정리

"작업 완료 또는 세션 종료 시 실행 환경과 파일 자동 정리" 요건을 검증한다.

정리 경로는 두 가지다.

| 방식 | 트리거 | 용도 |
| --- | --- | --- |
| Timed lifecycle cooldown | 마지막 API 호출 이후 idle 시간 경과 | 기본 자동 정리. 허용 범위 300~3600초 |
| Delete session API | backend가 명시적으로 호출 | 작업 완료 즉시 용량 회수 |

cooldown은 최소 300초라 실습 중 관찰하기 번거롭다. Delete API로 동일한 결과를 즉시 확인한다.

먼저 정리 대상 파일을 하나 만든다.

```bash
export CLEANUP_SESSION_ID="python-$(uuidgen | tr '[:upper:]' '[:lower:]')"

printf 'a,b\n1,2\n' > cleanup-probe.csv

curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --form "file=@cleanup-probe.csv;filename=cleanup-probe.csv" \
  --output /dev/null \
  --write-out 'upload HTTP %{http_code}\n'

curl --fail-with-body --silent --show-error \
  "$PYTHON_ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output before-cleanup.json

jq '.value | length' before-cleanup.json
```

session을 삭제한다.

```bash
curl --fail-with-body --silent --show-error \
  --request DELETE \
  "$PYTHON_ENDPOINT/session?api-version=$SESSION_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output /dev/null \
  --write-out 'delete session HTTP %{http_code}\n'
```

같은 identifier로 다시 접근해 파일이 남아 있지 않은지 확인한다.

```bash
curl --fail-with-body --silent --show-error \
  "$PYTHON_ENDPOINT/files?api-version=$PYTHON_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output after-cleanup.json

cat after-cleanup.json

curl --silent \
  --output /dev/null \
  --write-out 'download after cleanup HTTP %{http_code}\n' \
  "$PYTHON_ENDPOINT/files/cleanup-probe.csv/content?api-version=$PYTHON_API_VERSION&identifier=$CLEANUP_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
```

2026-07-25 한국 중부 실제 확인 결과다.

| 단계 | 결과 |
| --- | --- |
| 삭제 전 파일 목록 | `cleanup-probe.csv` 1건 |
| `DELETE /session` | HTTP **204** |
| 삭제 후 파일 목록 | `{"value": []}` |
| 삭제 후 파일 다운로드 | HTTP **404** |

같은 identifier를 다시 써도 새로 할당된 빈 session이며 이전 파일은 복구되지 않는다. **session은 영구 저장소가 아니다.** 보존해야 하는 결과물은 정리 전에 Artifact Staging으로 명시적으로 옮긴다.

운영 권장:

- 작업이 끝나면 cooldown을 기다리지 말고 delete session API로 즉시 회수한다. 용량과 비용에 유리하다.
- Custom Container pool은 delete 대신 stop session management API를 쓴다.
- 정리 실패는 조용히 넘기지 말고 metric과 경보로 관측한다.

## 15. 문제 해결

| 증상 | 조치 |
| --- | --- |
| `QuotaExceeded` | quota 증가 또는 다른 지원 리전 |
| HTTP 401 | token audience 확인 후 재발급 |
| HTTP 403 | role scope 확인, 전파 대기 후 token 재발급 |
| HTTP 404 | endpoint, pool, Resource Group 확인 |
| HTTP 413 | 128MB 이하로 분할 |
| HTTP 200인데 `status: Failed` | `result.stderr` 확인. timeout과 메모리 초과가 여기로 온다 |
| `Request timed out waiting for code execution to complete` | 220초 한도 초과. 작업 분할 또는 비동기 compute |
| `Execution aborted` | 메모리 한도 초과. 데이터 분할 처리 |
| `SessionPropertiesMissing` | execution 속성의 `properties` 래퍼 제거 |
| `SessionRequestValidationFailed` | `identifier`, `api-version`, endpoint와 method 확인. 응답의 `target`, `traceId` 기록 |
| `SessionRequestNotSupported` | 현재 API version에서 endpoint 또는 HTTP method가 지원되는지 공식 data-plane API 문서 확인 |
| `SessionWithIdentifierNotFound` | 새 identifier로 업로드부터 재실행 |
| 다른 session의 파일이 안 보임 | 정상 동작이다. session은 서로 격리된다 |
| Python import 오류 | 사전 설치 목록 확인 후 필요하면 Custom Container 검토 |

## 16. LLM Agent backend 구성

Python pool 검증이 끝났으면 사용자가 자연어로 요청할 수 있도록 Agent backend와 실제 LLM을 연결한다. 사용자는 이 설정과 credential을 보지 않는다.

### 16.1 구성 요소

| 구성 요소 | 관리자 책임 |
| --- | --- |
| `agent/policy.py` | 위험 요청을 LLM과 session 할당 전에 분류 |
| `agent/broker.py` | token, endpoint와 identifier를 backend에서 관리 |
| `agent/llm.py` | 실제 모델 호출과 생성 코드 수신 |
| `agent/staging.py` | 파일 형식, macro, 경로와 hash 검사 |
| `agent/orchestrator.py` | 생성·실행·재시도·삭제·승인 흐름 연결 |

먼저 실제 모델 없이 통제 장치를 검증한다.

```bash
cd "${REPO_ROOT:-$PWD}"
test -f scripts/agent-lab.sh || {
  echo "repository root에서 실행해야 합니다" >&2
  exit 1
}
export RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ai-workspace-sandbox-lab}"
export SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}"

python3 -m unittest discover -s tests -v
LLM_PROVIDER=stub bash scripts/agent-lab.sh
```

### 16.2 Foundry(Azure OpenAI) 모델 배포

기존 배포가 있으면 이 절의 생성 명령을 건너뛰고 account endpoint와 deployment 이름만 확인한다. 실습용 배포는 가용 쿼타 전량이 아니라 작은 capacity부터 시작한다.

먼저 해당 모델의 가용 쿼타와 기존 배포를 확인한다.

```bash
export LLM_LOCATION="koreacentral"
export LLM_MODEL="gpt-5.6-terra"

az cognitiveservices usage list \
  --location "$LLM_LOCATION" \
  --query "[?name.value=='OpenAI.GlobalStandard.$LLM_MODEL'].{limit:limit,used:currentValue}" \
  --output table

az cognitiveservices account list --query '[].{name:name,rg:resourceGroup}' --output tsv \
| while read -r NAME GROUP; do
    az cognitiveservices account deployment list \
      --name "$NAME" \
      --resource-group "$GROUP" \
      --query "[?properties.model.name=='$LLM_MODEL'].{account:'$NAME',rg:'$GROUP',deployment:name,capacity:sku.capacity}" \
      --output tsv 2>/dev/null
  done
```

가용량이 10K TPM보다 적으면 새 배포를 만들지 않는다. 접근 권한이 있는 기존 배포를 재사용하거나 quota 증설 후 진행한다.

새 실습용 account와 deployment가 필요할 때만 다음을 실행한다. `LLM_ACCOUNT`는 Azure 전체에서 고유해야 한다.

```bash
export LLM_RESOURCE_GROUP="$RESOURCE_GROUP"
export LLM_ACCOUNT="${LLM_ACCOUNT:-aiwsllm$(printf '%s' "$SUBSCRIPTION_ID" | tr -d '-' | cut -c1-20)}"

az cognitiveservices account create \
  --name "$LLM_ACCOUNT" \
  --resource-group "$LLM_RESOURCE_GROUP" \
  --kind AIServices \
  --sku S0 \
  --location "$LLM_LOCATION" \
  --custom-domain "$LLM_ACCOUNT" \
  --assign-identity \
  --yes \
  --output none

az cognitiveservices account deployment create \
  --name "$LLM_ACCOUNT" \
  --resource-group "$LLM_RESOURCE_GROUP" \
  --deployment-name "$LLM_MODEL" \
  --model-name "$LLM_MODEL" \
  --model-version 2026-07-09 \
  --model-format OpenAI \
  --sku-name GlobalStandard \
  --sku-capacity 10 \
  --output none
```

`sku-capacity 10`은 10K TPM이다. 배포가 실패하면 해당 모델·SKU의 subscription quota와 기존 배포 사용량을 먼저 확인한다.
기본 account 이름을 사용할 수 없으면 영문 소문자와 숫자로 된 다른 전역 고유 이름을 `LLM_ACCOUNT`에 지정한다.

### 16.3 추론 RBAC와 backend 환경

기존 배포를 재사용한다면 먼저 실제 account 이름, Resource Group과 deployment 이름으로 `LLM_ACCOUNT`, `LLM_RESOURCE_GROUP`, `LLM_MODEL`을 설정한다.

```bash
export LLM_ACCOUNT_ID=$(az cognitiveservices account show \
  --name "$LLM_ACCOUNT" \
  --resource-group "$LLM_RESOURCE_GROUP" \
  --query id \
  --output tsv)

az role assignment create \
  --role "Cognitive Services OpenAI User" \
  --assignee-object-id "$(az ad signed-in-user show --query id --output tsv)" \
  --assignee-principal-type User \
  --scope "$LLM_ACCOUNT_ID"

export LLM_PROVIDER="azure-openai"
export AZURE_OPENAI_ENDPOINT="https://$LLM_ACCOUNT.openai.azure.com"
export AZURE_OPENAI_DEPLOYMENT="$LLM_MODEL"
export REASONING_EFFORT="medium"
```

Production에서는 로그인한 사용자가 아니라 Agent backend의 Managed Identity에 두 역할을 부여한다.

- Python pool 범위: `Azure ContainerApps Session Executor`
- Azure OpenAI account 범위: `Cognitive Services OpenAI User`

구성이 끝나면 **리소스를 정리하기 전에** [실습 1B](01B_Python_Code_Interpreter_User_Lab.md)에서 실제 LLM의 자연어 요청, 코드 생성, 실행과 승인을 검증한다.

### 16.4 관리자 확인 사항

- 정책 엔진이 LLM 호출보다 먼저 실행됨
- 생성 코드가 실행 전에 검사됨
- 성공 판정은 `stderr`가 아니라 platform `status`를 사용함
- 필수 산출물 이름을 LLM에 전달하고 누락 시 재시도 또는 실패 처리함
- 재시도 횟수와 실행 timeout이 제한됨
- session identifier와 token이 사용자 응답에 없음
- 성공·실패와 관계없이 session이 삭제됨
- 승인하지 않은 artifact는 staging 밖으로 이동하지 않음

## 17. 정리

> 현재 리소스를 보존해야 하면 이 절을 실행하지 않는다.
> Office 실습에서 같은 Resource Group을 사용할 예정이면 Python pool만 선택적으로 삭제하고 Resource Group은 유지한다.
> 실습 1B를 수행할 예정이면 먼저 16절의 LLM 연결과 실습 1B 검증을 끝낸다.

session 단위 정리는 [14.1절](#141-session-종료와-임시-파일-자동-정리)에서 이미 검증했다. 이 절은 Azure 리소스 자체를 삭제한다.

Fast Path로 시작한 경우에도 동작하도록 기본 이름을 다시 설정하고 삭제 대상을 확인한다.

```bash
export RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ai-workspace-sandbox-lab}"
export PYTHON_POOL_NAME="${PYTHON_POOL_NAME:-ai-workspace-python-sbx}"
az group show --name "$RESOURCE_GROUP" \
  --query '{name:name,location:location,id:id}' --output table
```

수동 경로에서 만든 Session만 즉시 삭제:

```bash
curl --fail-with-body --silent --show-error \
  --request DELETE \
  "$PYTHON_ENDPOINT/session?api-version=$SESSION_API_VERSION&identifier=$PYTHON_SESSION_ID" \
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

## 18. 참고: 실제 검증 기록

2026-07-24 한국 중부 리전:

- PythonLTS pool `Succeeded`
- code execution HTTP 200
- CSV와 Python 파일 upload HTTP 200
- PNG·JSON 생성과 download HTTP 200
- PNG 640x480 확인
- JSON 값과 SHA-256 확인
- egress 요청 `URLError`로 차단

2026-07-25 한국 중부 리전 추가 검증:

| 항목 | 결과 |
| --- | --- |
| 사전 설치 라이브러리 | Python 3.12.7, pandas 2.2.2, numpy 1.26.4, matplotlib 3.8.4, scipy 1.13.1, scikit-learn 1.5.1, python-docx 1.2.0, python-pptx 1.0.2, openpyxl 3.1.5 등 확인 |
| 세션 간 파일 격리 | session B 목록 `{"value": []}`, 교차 다운로드 HTTP 404 |
| 오류 후 코드 수정 재실행 | 1회차 `Failed` (`KeyError`), 2회차 `Succeeded` |
| 실행 시간 한도 | 300초 sleep이 221.5초에 `Failed`, `Request timed out waiting for code execution to complete` |
| 메모리 한도 | 무한 할당이 `Failed`, `Execution aborted` |
| Session 삭제 | HTTP 204, 이후 목록 비어 있음, 파일 다운로드 HTTP 404 |
| Agent 오케스트레이션 | [실습 1B](01B_Python_Code_Interpreter_User_Lab.md) 참조 |

2026-07-25 이 문서의 명령을 **자동 스크립트가 아니라 문서에 적힌 그대로** 실행한 검증이다.

| 절 | 결과 |
| --- | --- |
| §8 첫 실행 | HTTP 200, `Succeeded` |
| §8.1 사전 설치 라이브러리 | 표의 버전과 일치 (python 3.12.7, pandas 2.2.2, docx 1.2.0 등) |
| §10 업로드 | 두 파일 HTTP 200 |
| §11 분석 실행 | HTTP 200, `Succeeded` |
| §12 다운로드 | HTTP 200, `{"2026-01":200.0,"2026-02":240.0,"2026-03":240.0}` |
| §13 egress | `EGRESS_BLOCKED URLError` |
| §13.1 격리 | 목록 `{"value":[]}`, `files: []`, 교차 다운로드 404, 삭제 204 |
| §13.2 오류 재실행 | `Failed`(`KeyError: 'sales_amount'`) → `Succeeded`(`total: 680.0`) |
| §14 session 조회 | `identifier`, `createdAt`, `lastAccessedAt`, `expireAt`, `etag` |
| §14.1 정리 | 업로드 200 → 삭제 204 → 목록 `{"value":[]}` → 다운로드 404 |
