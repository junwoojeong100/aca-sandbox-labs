# AI Workspace 격리형 Sandbox 권장 아키텍처 및 실습 가이드

## 목표

AI Workspace 사용자가 파일과 자연어 요청을 제출하면 Azure Container Apps Dynamic Sessions의 격리 세션에서 분석 또는 문서 생성 작업을 수행하고, **검사와 사용자 승인 후에만** 실제 업무 저장소에 반영하는 흐름을 체험한다.

이 가이드는 AI Workspace의 LLM과 Agent가 요청 분석, 실행 계획 수립, 코드 생성 및 수정·재실행을 담당하고, Dynamic Sessions가 신뢰할 수 없는 코드와 파일을 격리 실행하는 구조를 다룬다.

권장 기준선은 다음과 같다.

- Python Code Interpreter 기본 풀
- Office 전용 Custom Container 풀
- 기본 egress 차단
- 승인 전 실제 시스템 반영 금지

### 먼저 결정할 3가지

1. **실행 경로:** 일반 분석·계산은 Python pool, Office 생성·변환은 Custom Container pool로 분리한다.
2. **네트워크:** 기본 pool은 `EgressDisabled`로 두고, 외부 통신은 별도의 통제 egress pool과 승인 절차로만 허용한다.
3. **반영 경계:** Sandbox는 실제 업무 시스템에 직접 쓰지 않으며, 검사와 사용자 승인 후 승인 서비스만 반영한다.

## 1. 실습 개요

| 항목 | 내용 |
| --- | --- |
| 대상 | AI Workspace 사용자, 운영자, 보안 담당자 |
| 빠른 실습 예상 시간 | 45~60분 |
| 고급 구현 가이드 검토 시간 | 2~4시간 (실제 시스템 구현 및 Office 이미지 준비 시간 제외) |
| 사용자 역할 | 요청과 파일 제출, 결과 검토, 승인 또는 거부 |
| AI Workspace 백엔드 역할 | 정책 라우팅, 세션 식별자와 Entra 토큰 관리, 산출물 검사 및 승격 |
| 핵심 보안 원칙 | 사용자는 Session Endpoint, Entra 토큰, 내부 session identifier에 직접 접근하지 않는다. |

> 세션은 서로 격리되지만, 같은 세션에서 실행되는 코드는 해당 세션의 파일과 환경 변수를 볼 수 있다. 프로덕션 자격 증명, 광범위한 인터넷 접근, 영구 공유 볼륨을 범용 Sandbox에 제공하지 않는다.

### 이 가이드를 사용하는 방법

이 가이드는 다음 두 단계로 구성한다.

1. **빠른 실습(필수):** 3~4.4절을 순서대로 수행한다. Python pool 생성, 샘플 CSV 업로드, 분석 실행, 결과 다운로드와 Azure 리소스 정리까지를 완료한다. 이 단계에서는 AI Workspace 백엔드 대신 실습자의 Entra identity를 사용한다.
2. **고급 구현 가이드(선택):** 4.5절과 5~12절을 참고한다. 실제 AI Workspace 백엔드, Office Custom Container, 승인 서비스, 통제 egress, 재시도 정책을 설계·구현할 때 사용하는 체크리스트이며, 복사해 실행하는 완결형 실습은 아니다.

빠른 실습에서 만든 토큰과 session identifier는 **로컬 터미널에서만** 사용한다. 실제 AI Workspace에서는 백엔드가 해당 값을 생성·보관하고 브라우저에 전달하지 않는다.

> **Fast Track:** 빠른 실습만 수행하려면 [3.1 환경 선택](#31-실행-환경-선택) → [3.2 리전·Quota 확인](#32-리전과-quota-확인) → [3.3 Identity 확인](#33-identity와-권한-경계) → [4.1 Python pool 생성](#41-python-기본-풀) → [4.2 실행 확인](#42-격리-python-실행-확인) → [4.3 CSV 분석](#43-빠른-실습-csv-업로드-분석-결과-다운로드) → [4.4 정리](#44-빠른-실습-정리) 순서로 진행한다.

### 요구사항별 권장 대응

| AI Workspace 요구사항 | 권장 설계 |
| --- | --- |
| 자연어 요청 기반 코드 생성·실행 | Agent가 계획과 코드를 생성하고, Session Broker가 Python pool에 전달 |
| 실행 오류 수정·재실행 | `stderr`, 오류 유형, 실행 ID만 Agent에 반환하고 최대 재시도 횟수를 정책으로 제한 |
| 데이터 분석·계산·차트·파일 생성 | Python Code Interpreter pool에서 처리하고 산출물은 임시 저장소에 보관 |
| 첨부파일 분석·가공 | 업로드 전 파일 형식·크기·악성코드 검사를 수행하고 세션의 임시 파일 영역에만 제공 |
| Office 문서 생성·변환 | LibreOffice, Pandoc, Poppler, 폰트 버전을 고정한 Custom Container pool로 분리 |
| 요청별 독립 환경 | 서버가 사용자/요청/대화에 매핑한 예측 불가능한 identifier로 세션을 할당 |
| 자동 정리 | cooldown/TTL로 정리하고 작업 완료 시 Delete Session API로 조기 회수 |
| 실제 업무 반영 통제 | 검사, 미리보기/Diff, 명시적 승인, 감사 기록을 통과한 산출물만 Connector로 승격 |

## 2. 목표 아키텍처

```text
AI Workspace 사용자
  -> AI Workspace Agent / 정책 엔진
  -> Session Broker
       - 서버가 난수 identifier와 Entra 토큰 관리
       - Tenant별 동시성 및 재시도 제한
  -> [Python Code Interpreter Pool | Office Custom Container Pool]
  -> 임시 산출물 저장소
  -> 검사(형식, 크기, 악성코드, DLP) / 미리보기 / Diff
  -> 사용자 승인
  -> 승인된 Connector만 실제 저장소에 반영
```

### 처리 순서

1. 사용자가 자연어 요청과 파일을 제출한다.
2. 정책 엔진이 필요한 도구, 예상 실행 시간, 파일 크기, 네트워크 요구를 판단한다.
3. Session Broker가 허용된 session pool로 요청을 전달한다.
4. 격리 세션이 작업하고, 결과와 임시 파일은 세션 및 임시 산출물 영역에만 생성한다.
5. 오류가 나면 Agent는 제한된 오류 정보로 코드를 수정하고 정책상 허용된 횟수 안에서 재실행한다.
6. 검사와 미리보기를 통과한 결과를 사용자가 승인하거나 거부한다.
7. 승인 서비스가 산출물 hash 및 감사 기록을 남긴 뒤, 허용된 Connector로만 승격한다.
8. 세션은 작업 종료 또는 cooldown/TTL 만료 후 정리한다.

## 3. 사전 준비

### 3.1 실행 환경 선택

빠른 실습은 Bash 기반이며 다음 중 하나를 선택한다.

| 환경 | 먼저 할 일 |
| --- | --- |
| Azure Cloud Shell | Portal에서 Bash Cloud Shell을 연다. 이미 로그인되어 있으므로 `az login`과 `az upgrade`를 실행하지 않는다. |
| 로컬 Bash | Azure CLI 2.79.0 이상을 설치한 뒤 `az login`을 실행한다. 기존 CLI를 직접 업그레이드하려는 경우에만 `az upgrade`를 실행한다. |

두 환경 모두 다음 공통 설정을 실행한다. `<...>` 값 네 개만 실제 값으로 바꾼다. 실습 전용 Resource Group을 사용하면 마지막 정리가 간단하다.

```bash
export SUBSCRIPTION_ID="<SUBSCRIPTION_ID>"
export RESOURCE_GROUP="<RESOURCE_GROUP>"
export LOCATION="<LOCATION>"
export SESSION_POOL_NAME="ai-workspace-python-sbx"

az account set --subscription "$SUBSCRIPTION_ID"
az extension add --name containerapp --upgrade --allow-preview true -y
az provider register --namespace Microsoft.App --wait
az account show --query '{subscription:id, user:user.name}' --output json
```

`az account show`의 subscription ID가 `SUBSCRIPTION_ID`와 같아야 한다. 실습에는 개인정보, 운영 비밀, 프로덕션 credential이 포함되지 않은 샘플 데이터만 사용한다.

### 3.2 리전과 Quota 확인

먼저 [Dynamic Sessions 지원 리전](https://learn.microsoft.com/azure/container-apps/sessions#supported-regions)에서 `LOCATION`을 확인한다. 리전 가용성은 바뀔 수 있으므로 Azure Portal의 **Container Apps Session Pools > Create > Location** 목록도 함께 확인한다.

Quota는 Azure CLI로 먼저 조회한다. Quota resource name과 ARM resource type은 일대일로 대응하지 않으므로 특정 이름을 가정하지 말고 전체 목록에서 session 또는 Container Apps 관련 항목을 확인한다.

```bash
az extension add --name quota --upgrade -y
az provider register --namespace Microsoft.Quota --wait

export QUOTA_SCOPE="/subscriptions/$SUBSCRIPTION_ID/providers/Microsoft.App/locations/$LOCATION"

az quota list --scope "$QUOTA_SCOPE" --output table
az quota usage list --scope "$QUOTA_SCOPE" --output table
```

두 명령이 quota limit과 usage를 반환하면 사용 가능한 여유가 있는지 확인한다. `BadRequest` 또는 지원되지 않는 provider 오류가 나오면 “무제한”으로 해석하지 말고 [Azure subscription and service limits](https://learn.microsoft.com/azure/azure-resource-manager/management/azure-subscription-service-limits)와 Portal의 **My quotas**에서 확인한다. 배포 시 `QuotaExceeded`가 발생하면 quota 증가를 요청하거나 다른 지원 리전을 선택한다.

### 3.3 Identity와 권한 경계

빠른 실습에서는 현재 `az login`한 identity에 session pool 범위의 역할을 부여한다. 실제 환경에서는 이 identity 대신 AI Workspace 백엔드의 Managed Identity principal ID를 사용한다. 세션 관리 API 토큰의 audience는 `https://dynamicsessions.io`이며, 토큰과 내부 identifier를 브라우저나 최종 사용자에게 전달하지 않는다.

빠른 실습 identity에는 두 종류의 유효 권한이 필요하다. `Contributor`는 Resource Group에서 session pool을 생성·삭제할 때 사용하며, `Azure ContainerApps Session Executor`는 pool의 데이터 평면 API를 호출할 때 사용한다. pool을 직접 생성할 수 있는 사용자는 일반적으로 상위 범위에서 `Contributor`를 이미 상속받으므로, 다음 절에서는 session pool 범위에 `Session Executor`만 추가한다. 역할을 추가하려면 대상 범위의 Owner 또는 User Access Administrator 권한이 필요하다.

이 실습의 사용자 직접 토큰·endpoint 사용은 학습을 위한 예외다. 실제 AI Workspace에서는 반드시 백엔드 Managed Identity가 호출하고 최종 사용자는 토큰, endpoint, identifier를 볼 수 없어야 한다. API/역할 요구 사항은 Preview 변경의 영향을 받을 수 있으므로 production 배포 전 공식 문서와 실제 환경에서 다시 확인한다.

## 4. Session Pool 구성

### 4.1 Python 기본 풀

CSV 분석, 계산, 차트 생성처럼 일반 Python 작업은 Code Interpreter pool로 보낸다. 기본 Sandbox는 인터넷 egress를 차단한다.

```bash
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output table

az containerapp sessionpool create \
  --name "$SESSION_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --container-type PythonLTS \
  --max-sessions 10 \
  --cooldown-period 300 \
  --network-status EgressDisabled
```

Session pool의 `max-sessions`는 5~600 범위다. 처음에는 `max-sessions=10`, cooldown 300초를 기준으로 시작하고, Tenant별 동시성 상한과 준비 세션 수는 부하 패턴 및 quota 검증 결과에 따라 조정한다.

생성 결과를 확인한다.

```bash
az containerapp sessionpool show \
  --name "$SESSION_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{
    provisioningState:properties.provisioningState,
    endpoint:properties.poolManagementEndpoint,
    networkStatus:properties.sessionNetworkConfiguration.status
  }' \
  --output yaml
```

**통과 기준:** `provisioningState`는 `Succeeded`, `endpoint`는 비어 있지 않고, `networkStatus`는 `EgressDisabled`여야 한다.

pool을 만든 다음, 빠른 실습을 실행할 identity에 필요한 역할을 부여하고 관리 endpoint를 가져온다. 실제 AI Workspace에서는 `CALLER_OBJECT_ID`에 백엔드 Managed Identity의 principal ID를 넣는다.

```bash
export SESSION_POOL_RESOURCE_ID=$(az containerapp sessionpool show \
  --name "$SESSION_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv)
export CALLER_OBJECT_ID=$(az ad signed-in-user show --query id --output tsv)

az role assignment create \
  --role "Azure ContainerApps Session Executor" \
  --assignee-object-id "$CALLER_OBJECT_ID" \
  --assignee-principal-type User \
  --scope "$SESSION_POOL_RESOURCE_ID"

az role assignment list \
  --assignee-object-id "$CALLER_OBJECT_ID" \
  --scope "$SESSION_POOL_RESOURCE_ID" \
  --include-inherited \
  --query '[].{role:roleDefinitionName, scope:scope}' \
  --output table

export POOL_MANAGEMENT_ENDPOINT=$(az containerapp sessionpool show \
  --name "$SESSION_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.poolManagementEndpoint --output tsv)
echo "$POOL_MANAGEMENT_ENDPOINT"
```

역할 목록에서 상속된 `Contributor`와 pool 범위의 `Azure ContainerApps Session Executor`를 확인한다. 역할 할당은 전파에 수 분이 걸릴 수 있다. 역할이 표시돼도 다음 API가 HTTP 403을 반환하면 30~60초 후 token을 다시 발급해 재시도한다. `RoleAssignmentExists`는 동일 역할이 이미 있다는 의미다.

### 4.2 격리 Python 실행 확인

관리 API의 `executions` endpoint를 호출해 세션을 만들고 Python 코드를 실행한다. 지정한 `identifier`의 세션이 없으면 이 호출이 새 세션을 할당한다. 다음 예제는 빠른 실습용으로만 로컬 토큰을 사용한다.

```bash
TOKEN=$(az account get-access-token \
  --resource https://dynamicsessions.io \
  --query accessToken --output tsv)
SESSION_ID="lab-$(uuidgen | tr '[:upper:]' '[:lower:]')"

curl --fail-with-body --silent --show-error \
  --request POST \
  "$POOL_MANAGEMENT_ENDPOINT/executions?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "codeInputType": "inline",
    "executionType": "synchronous",
    "code": "print(\"AI Workspace Dynamic Sessions validation passed\")"
  }' \
  --output first-execution.json \
  --write-out '\nexecute HTTP %{http_code}\n'

cat first-execution.json
```

**통과 기준:** 터미널에 `execute HTTP 200`이 표시되고 `first-execution.json`의 실행 결과에 `AI Workspace Dynamic Sessions validation passed`가 포함돼야 한다. 403이면 역할 전파를 기다린 뒤 `TOKEN`을 다시 발급하고, 404이면 `POOL_MANAGEMENT_ENDPOINT`와 pool 이름을 확인한다.

> 이 요청 형식은 2026-07-24 한국 중부 리전의 `2025-10-02-preview` endpoint에서 실제 검증했다. execution body의 `codeInputType`, `executionType`, `code`는 최상위 속성이다. `properties`로 감싸면 `SessionPropertiesMissing` 오류가 발생할 수 있다.

문서의 `Authorization: ******` 표기는 민감한 값을 숨긴 예시다. 실제 실행할 때는 모든 관리 API 호출에 `--header "Authorization: Bearer $TOKEN"`을 사용한다.

### 4.3 빠른 실습: CSV 업로드, 분석, 결과 다운로드

다음 샘플 파일은 월별 합계와 제품별 매출을 계산해 `monthly_sales.png`와 `summary.json`을 생성한다. 이 단계에서 생성하는 파일은 모두 로컬 또는 session의 `/mnt/data`에만 존재한다.

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
            "top_products": sorted(products.items(), key=lambda item: item[1], reverse=True)[:5],
        },
        output,
        ensure_ascii=False,
        indent=2,
    )
PY

for FILE in sales.csv analyze_sales.py; do
  curl --fail-with-body --silent --show-error \
    --request POST \
    "$POOL_MANAGEMENT_ENDPOINT/files?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --form "file=@$FILE" \
    --output /dev/null \
    --write-out "$FILE upload HTTP %{http_code}\n"
done

curl --fail-with-body --silent --show-error \
  --request POST \
  "$POOL_MANAGEMENT_ENDPOINT/executions?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
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

실행이 성공하면 다음 명령으로 생성 파일을 확인하고 로컬로 내려받는다.

```bash
curl --fail-with-body --silent --show-error \
  "$POOL_MANAGEMENT_ENDPOINT/files?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
  --output session-files.json \
  --write-out 'list files HTTP %{http_code}\n' \
  --header "Authorization: Bearer $TOKEN"

cat session-files.json

for FILE in monthly_sales.png summary.json; do
  curl --fail-with-body --silent --show-error \
    --output "$FILE" \
    "$POOL_MANAGEMENT_ENDPOINT/files/$FILE/content?api-version=2025-10-02-preview&identifier=$SESSION_ID" \
    --write-out "$FILE download HTTP %{http_code}\n" \
    --header "Authorization: Bearer $TOKEN"
done

test -s monthly_sales.png
test -s summary.json
cat summary.json
```

**통과 기준:** 각 upload, analysis, list, download 호출이 HTTP 2xx를 반환하고 파일 목록에 `sales.csv`, `analyze_sales.py`, `monthly_sales.png`, `summary.json`이 포함돼야 한다. `test -s`가 오류 없이 끝나고 `summary.json`의 월별 합계가 2026-01: 200, 2026-02: 240, 2026-03: 240이면 성공이다.

#### 빠른 실습 문제 해결

| 증상 | 확인 및 조치 |
| --- | --- |
| `QuotaExceeded` 또는 pool 생성 실패 | 3.2절의 quota와 지원 리전을 다시 확인하고, quota 증가를 요청하거나 다른 지원 리전을 선택한다. |
| HTTP 401 | token audience가 `https://dynamicsessions.io`인지 확인하고 `TOKEN`을 다시 발급한다. |
| HTTP 403 | role scope와 caller object ID를 확인한다. 역할 할당 후 30~60초 기다리고 token을 다시 발급한다. |
| HTTP 404 | `POOL_MANAGEMENT_ENDPOINT`, pool 이름, Resource Group과 API 경로를 확인한다. |
| HTTP 413 | 파일이 128MB 제한을 넘었다. 파일을 분할하거나 별도 staging 경로를 사용한다. |
| `SessionPropertiesMissing` | execution JSON에서 `codeInputType`, `executionType`, `code`가 최상위 속성인지 확인하고 `properties` 래퍼를 제거한다. |
| `SessionWithIdentifierNotFound` | cooldown으로 세션이 정리됐을 수 있다. 새 `SESSION_ID`를 만들고 파일 업로드부터 다시 수행한다. |
| Python import 또는 실행 오류 | `analysis-execution.json`의 `stderr`를 확인한다. 기본 interpreter에 없는 라이브러리가 필요하면 Custom Container 경로로 전환한다. |

### 4.4 빠른 실습 정리

```bash
curl --fail-with-body --silent --show-error \
  --request DELETE \
  "$POOL_MANAGEMENT_ENDPOINT/session?api-version=2025-02-02-preview&identifier=$SESSION_ID" \
  --output /dev/null \
  --write-out 'delete session HTTP %{http_code}\n' \
  --header "Authorization: Bearer $TOKEN"
```

`delete session HTTP 204`가 표시되면 세션이 삭제된 것이다. 데이터 평면의 execution/file API와 session 삭제 API는 Preview 버전이 서로 다르므로 이 문서에서는 각각 `2025-10-02-preview`, `2025-02-02-preview`를 의도적으로 사용한다.

로컬 샘플과 응답 파일을 삭제한다.

```bash
rm -f \
  sales.csv analyze_sales.py monthly_sales.png summary.json \
  first-execution.json analysis-execution.json session-files.json
```

session pool도 삭제해 과금 가능한 리소스를 남기지 않는다.

```bash
az containerapp sessionpool delete \
  --name "$SESSION_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --yes

if az containerapp sessionpool show \
  --name "$SESSION_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output none 2>/dev/null; then
  echo "Session pool still exists"
else
  echo "Session pool cleanup confirmed"
fi
```

Resource Group이 이 실습만을 위해 만들어졌다면 다음 명령으로 전체 정리한다. 공유 Resource Group에는 실행하지 않는다. 삭제된 리소스 범위의 역할 할당도 함께 사라진다.

```bash
az group delete \
  --name "$RESOURCE_GROUP" \
  --yes \
  --no-wait
```

**정리 통과 기준:** `az containerapp sessionpool show`가 resource not found를 반환하거나, 전용 Resource Group 삭제가 시작돼야 한다.

### 4.5 Office 전용 Custom Container 풀 (고급 구현 가이드)

DOCX, XLSX, PPTX, PDF 생성과 변환은 LibreOffice, Pandoc, Poppler, 필요한 폰트와 라이브러리 버전을 고정한 Custom Container pool로 분리한다.

이 절은 빠른 실습 범위가 아니며 완결형 실행 명령을 제공하지 않는다. 시나리오 2를 구현하기 전에 다음 항목을 모두 준비한다.

- LibreOffice, Pandoc, Poppler 및 필요한 폰트를 포함하고 최소 권한의 비루트 사용자로 실행되는 version-pinned container image
- HTTP server와 변환 요청/상태 확인 API. Custom Container session pool은 노출한 target port로 요청을 전달한다.
- workload profiles가 활성화된 Container Apps environment
- 이미지 pull에만 쓰는 Managed Identity 또는 registry credential. 런타임 리소스 접근 identity와 분리한다.
- `/health` endpoint를 사용하는 startup/liveness probe, 이미지 취약점 검사, 고유한 image tag를 사용하는 업데이트 절차
- 세션의 런타임 Managed Identity 노출은 기본적으로 사용하지 않는다. 세션 안의 임의 코드가 identity 토큰을 얻을 수 있기 때문이다.
- 외부 통신이 필요하면 기본 풀 설정을 완화하지 않는다. VNet, UDR, Firewall, 목적지 허용 목록, DNS와 트래픽 로깅을 갖춘 별도 통제 egress pool을 사용한다.

## 5. 정책 기반 라우팅

LLM이 임의로 런타임을 선택하지 않도록, AI Workspace 정책 엔진의 허용 목록으로 실행 경로를 고정한다.

| 분류 | 판단 조건 | 실행 경로 | 운영 원칙 |
| --- | --- | --- | --- |
| A | Python 분석/계산, 220초 이하, 128MB 이하, 외부 통신 불필요 | Python 기본 풀 | 기본 허용 |
| B | DOCX/XLSX/PPTX/PDF 생성 또는 변환 | Office Custom Container 풀 | 고정 이미지와 도구만 허용 |
| C | 220초 초과, 128MB 초과, 대량 batch | Custom/비동기 경로 | 분할, staging, 상태 조회 |
| D | 광범위 egress, 관리자 명령, 운영 시스템 직접 변경 | 거부 또는 사람 승인 | 정책 예외 절차 |

`identifier`는 암호학적으로 생성한 예측 불가능한 값으로 사용한다. 사용자 ID나 순번을 identifier로 직접 쓰지 않고, 서버가 `tenant_id`, `user_id`, `conversation_id`와 내부 identifier를 매핑한다.

### 코드 수정 및 재실행 정책

1. 세션 실행 응답에서 `stdout`, `stderr`, 실행 시간, 오류 유형, `request_id`를 수집한다.
2. Agent에는 사용자 파일 원본, 토큰, 운영 자격 증명 없이 오류 정보와 작업 계획만 전달한다.
3. Agent가 수정한 코드는 동일 세션 또는 새 세션에서 재실행한다. 세션 재사용은 작업 연속성에만 사용하고, 다른 사용자·Tenant와 공유하지 않는다.
4. 재실행은 예를 들어 최대 2회로 제한하며, 초과하면 사용자의 검토 또는 사람 승인을 요청한다.
5. 모든 재실행은 코드 hash, 실행 ID, 결과, 결정 사유를 감사 로그에 남긴다.

## 6. 사용자 실습 시나리오

### 시나리오 1: 매출 CSV 분석과 차트 생성

빠른 실습에서는 4.3절의 샘플 파일과 명령을 그대로 사용한다. 다음 단계는 AI Workspace와 승인 서비스를 연결한 뒤 검증할 통합 시나리오다.

1. 사용자는 샘플 `sales.csv`를 업로드하고 다음 요청을 제출한다.

   ```text
   월별 매출 추이와 상위 5개 제품을 분석하고 차트를 만들어 줘.
   ```

2. AI Workspace는 확장자와 파일 크기를 검사하고 Python 기본 풀로 라우팅한다.
3. 세션은 CSV를 읽어 요약표와 PNG 차트를 생성한다.
4. 백엔드는 `stdout`, `stderr`, 파일 목록, SHA-256 hash, `request_id`를 임시 영역에 기록한다.
5. 사용자는 미리보기에서 결과를 확인하고 승인 또는 거부한다.
6. 승인한 경우에만 `reports/<tenant>/<request_id>/` 같은 최종 저장소 위치로 복사한다.

**빠른 실습 통과 기준:** `monthly_sales.png`와 `summary.json`이 session에서 생성·다운로드되고, 기본 pool의 egress 설정은 `EgressDisabled`이며, Delete Session API가 HTTP 204를 반환한다.

**통합 통과 기준:** 외부 통신 없이 분석이 완료되고, 승인 전 최종 저장소에는 결과 파일이 없다.

### 시나리오 2: Office 문서 생성

이 시나리오는 4.5절의 사전 조건을 모두 충족한 뒤 구현하는 고급 검증 시나리오다.

1. 사용자는 다음을 요청한다.

   ```text
   분석 결과를 경영진용 DOCX 보고서와 PDF로 만들어 줘.
   ```

2. 정책 엔진은 Office 도구 요구를 인식하고 Office Custom Container 풀로 보낸다.
3. 컨테이너가 DOCX와 PDF를 생성하고, 문서 미리보기와 메타데이터를 생성한다.
4. 승인 화면은 파일 형식, 크기, 정책 검사 결과, 미리보기, 이전 버전과의 Diff를 표시한다.
5. 사용자는 승인하거나 수정 요청을 보내 새 격리 작업을 수행한다.

**통과 기준:** Office 도구가 Python 기본 풀에 혼합되지 않고, 승인된 문서만 최종 저장소에 존재한다.

### 시나리오 3: egress 차단과 예외 처리

1. 사용자는 다음을 요청한다.

   ```text
   외부 URL에서 데이터를 내려받아 분석해 줘.
   ```

2. 기본 Python 풀은 `EgressDisabled`이므로 요청을 거부하거나, 정책상 허용된 통제 egress 풀의 사람 승인 대기 상태로 전환한다.
3. 승인되지 않으면 세션 실행과 실제 시스템 반영을 모두 수행하지 않는다.
4. 승인된 예외에는 허용 목적지, 승인자, 만료 시각, `request_id`를 감사 로그로 남긴다.

**통과 기준:** 기본 Sandbox에서 광범위 인터넷 접근이 불가능하고 정책 우회가 없다.

### 시나리오 4: 오류 수정과 제한된 재실행

1. 사용자는 “업로드한 CSV의 월별 매출을 차트로 만들어 줘”라고 요청한다.
2. 첫 실행에서 없는 열 이름으로 인한 오류가 발생하면, AI Workspace는 `stderr`와 허용된 파일 스키마를 바탕으로 수정 코드를 생성한다.
3. 정책 엔진이 재실행 횟수와 허용 Python 라이브러리를 확인한 뒤 실행한다.
4. 성공한 결과만 검사·미리보기·승인 단계로 이동한다. 재시도 상한을 넘기면 오류 요약을 사용자에게 반환하고 실행을 종료한다.

**통과 기준:** 오류 정보가 다른 Tenant나 운영 비밀을 포함하지 않으며, 정책상 허용된 횟수 안에서만 재실행된다.

## 7. 승인 게이트 체크리스트

승격 서비스는 세션과 분리하고, 다음 조건을 모두 만족할 때만 실제 반영을 실행한다.

- 산출물 파일 형식, 크기, hash, 생성 도구, `request_id`를 기록한다.
- malware 및 DLP 검사를 통과한다.
- 미리보기와 Diff를 제공한다.
- 사용자 또는 권한 있는 승인자의 명시적 승인이 있다.
- 최소 권한을 가진 승인된 Connector만 대상 시스템에 반영한다.
- 승인자, 시각, 정책 결과, 대상 위치, 최종 hash를 감사 로그에 기록한다.

실패 시 Agent에는 제한된 범위의 오류 원인, `stderr`, 정책 결과만 반환하고 재실행 횟수를 제한한다.

## 8. 세션 및 리소스 운영

| 운영 항목 | 권장 정책 |
| --- | --- |
| 세션 할당 | `identifier`의 첫 호출 시에만 세션을 할당하고, 사용자 또는 요청 단위로 서버가 매핑 |
| 동시성 | pool별 `max-sessions`, Tenant별 quota, 요청별 실행 횟수를 별도로 제한 |
| 준비 용량 | 콜드 스타트 허용 수준에 맞춰 `ready-sessions`를 조정하고, 비용과 할당 지연을 함께 관측 |
| 수명 | Timed lifecycle의 cooldown을 사용하고, 작업 완료 후 Delete Session API로 조기 회수 |
| CPU·메모리 | Custom Container는 이미지와 도구별 CPU·메모리 조합을 부하 테스트로 검증하고 pool을 분리 |
| 임시 파일 | 세션 파일 시스템과 임시 산출물 저장소는 승인 대상과 분리하고 TTL 기반 정리 정책 적용 |
| 비정상 세션 | timeout, 컨테이너 종료, 반복 실패를 감지해 세션을 종료하고 새 세션에서 제한적으로 재시도 |

## 9. 모니터링 및 감사 추적

모든 계층에서 다음 상관관계 ID를 일관되게 기록한다.

```text
request_id -> agent_plan_id -> session_identifier -> execution_id
           -> artifact_hash -> policy_decision -> approval_id
```

| 영역 | 수집 항목 | 활용 |
| --- | --- | --- |
| AI Workspace Agent | 요청 분류, 선택 pool, 코드 hash, 재실행 횟수, 오류 유형 | 정책 우회와 품질 문제 분석 |
| Session Broker | 할당 지연, 실행 시간, 성공률, 4xx/5xx, 세션 종료 | 용량·SLO·재시도 관리 |
| Custom Container pool | `stdout`/`stderr`, lifecycle, pool event, health probe | 이미지·도구·런타임 장애 진단 |
| Code Interpreter pool | 실행 응답의 `stdout`/`stderr`, 실행 시간, 생성 파일 목록 | 코드 실행 결과 및 오류 진단 |
| Approval Service | 검사 결과, artifact hash, 승인자, 시각, Connector 반영 결과 | 감사, 책임성, 사후 추적 |

Custom Container pool의 플랫폼 및 컨테이너 로그는 Azure Monitor와 Log Analytics로 전송한다. Code Interpreter의 실행 출력은 응답에서 수집해 AI Workspace의 `request_id`와 함께 보관한다.

권장 SLO는 pool별 할당 지연, 실행 성공률, 대기/실행 세션 수, TTL 초과, 세션 정리 지연, 재실행률, 승인율로 정의한다.

## 10. 주요 제약사항과 운영 사례

| 제약 또는 위험 | 설계 대응 |
| --- | --- |
| Code Interpreter 실행 시간 | 실행당 최대 220초를 넘는 작업은 분할, Custom Container, 비동기 작업으로 라우팅 |
| 파일 업로드 | 파일당 최대 128MB를 사전 검사하고, 초과 파일은 분할 또는 별도 staging 전략 사용 |
| Pool 동시성 | 현재 최소 5, 최대 600 세션 범위에서 quota와 Tenant별 상한을 검증 |
| API/Probe 상태 | Preview API는 버전을 고정하고 통합·회귀 테스트로 변경을 감시 |
| Custom Container 이미지 | 취약점 검사, 버전 고정, 비루트 실행, 허용 명령어, health probe 적용 |
| 네트워크 | 기본 `EgressDisabled`; 예외만 VNet, UDR, Firewall, 목적지 allowlist를 갖춘 별도 pool로 분리 |
| 실제 업무 시스템 접근 | Sandbox에는 접근 권한을 부여하지 않고, 승인 서비스의 최소 권한 Connector만 사용 |

Production 전에는 대상 리전 가용성, subscription/region quota, Custom Container 이미지의 CPU·메모리 조합, VNet/Firewall 경로를 실제 환경에서 검증한다.

## 11. 운영 및 보안 검증

| 검증 항목 | 통과 기준 |
| --- | --- |
| Tenant 격리 | Tenant A의 identifier 또는 파일로 Tenant B가 접근할 수 없다. |
| 토큰 비노출 | 브라우저, 클라이언트 로그, URL에 pool token 또는 내부 identifier가 없다. |
| 네트워크 | 기본 pool에서 외부 호출이 차단된다. |
| 승인 경계 | 승인 전 실제 저장소와 업무 시스템 변경이 0건이다. |
| 세션 정리 | 종료 API 또는 cooldown/TTL 후 세션과 임시 파일이 정리된다. |
| 관측성 | `request_id -> session identifier -> artifact hash -> approval decision` 흐름을 추적할 수 있다. |
| 재실행 통제 | 오류 수정과 재실행이 정책상 허용된 횟수와 명령어 범위를 넘지 않는다. |

## 12. 4주 PoC 권장 계획

1. **1주차 - 기반:** 두 pool, Managed Identity, Session Broker, 비노출 identifier를 구성하고 세션 격리를 검증한다.
2. **2주차 - 업무:** 이 문서의 Python 분석 및 Office 문서 생성 등 핵심 4개 시나리오를 실행한다.
3. **3주차 - 통제:** egress 차단/예외, 산출물 검사, 승인 게이트, 감사 추적을 검증한다.
4. **4주차 - 운영:** 동시 요청, 오류 재시도, cooldown/TTL 정리, quota 및 이미지 CPU/Memory 조합을 검증하고 Go/Adjust/Stop을 결정한다.

**PoC 완료 기준:** Tenant 간 파일 노출 0건, 정책 우회 0건, 세션 정리 누락 0건, 합의된 핵심 작업 성공률 달성.

## 공식 참고 자료

- [Dynamic Sessions 개요](https://learn.microsoft.com/azure/container-apps/sessions)
- [세션 사용, 보안, 인증](https://learn.microsoft.com/azure/container-apps/sessions-usage)
- [Code Interpreter 세션](https://learn.microsoft.com/azure/container-apps/sessions-code-interpreter)
- [Session pool 구성](https://learn.microsoft.com/azure/container-apps/session-pool)
- [Session pool Azure CLI](https://learn.microsoft.com/cli/azure/containerapp/sessionpool)
- [Azure Quotas 개요](https://learn.microsoft.com/azure/quotas/quotas-overview)
