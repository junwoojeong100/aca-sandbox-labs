# 실습 2A: Office Custom Container Session Pool - 관리자

## 목표

LibreOffice, Pandoc, Poppler와 폰트를 포함한 비루트 Custom Container image를 빌드하고 Azure Container Apps Dynamic Sessions에 배포해 다음을 검증한다.

- ACR private image와 Managed Identity pull
- workload profiles-enabled Container Apps Environment
- Log Analytics 연결
- Startup·Liveness probe
- `EgressDisabled`
- `/health` API
- DOCX·PDF·PPTX·XLSX 생성과 다운로드
- **허용 목록 기반 형식 변환**
- **선언적 문서 편집**
- 파일 형식과 SHA-256 검증

예상 시간은 60~120분이며 ACR build 시간은 별도다.

> Custom Container pool은 ready session을 최소 1개 요구할 수 있다. 유지되는 동안 비용이 발생한다. 이 실습은 Container Apps Environment, ACR, Log Analytics도 만들며 이들은 pool을 삭제해도 남는다. 비용 상세는 [권장 아키텍처 10.5절](../docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md#105-비용-모델)을 참고하고, 실습 후에는 17절의 정리 절차를 검토한다.

이 문서는 관리자가 Custom Container, ACR, identity, environment, pool, monitoring과 비용 통제를 구성·검증하는 절차다. 생성·변환·편집 중심의 사용자 실습은 [실습 2B](02B_Office_Custom_Container_User_Lab.md)에서 수행한다.

## 0. Custom Container가 필요한 경우

[실습 1A의 8.1절](01A_Python_Code_Interpreter_Admin_Lab.md#81-사전-설치-라이브러리-확인)에서 확인했듯 **`python-docx`, `python-pptx`, `openpyxl`, `reportlab`은 Python pool에도 이미 설치돼 있다.**

| 필요한 작업 | 권장 pool |
| --- | --- |
| DOCX·XLSX·PPTX 생성만 | Python pool로 충분하다 |
| reportlab으로 단순 PDF 생성 | Python pool로 충분하다 |
| **기존 Office 문서 -> PDF 변환** | Custom Container (LibreOffice 필요) |
| **Markdown·HTML -> DOCX 변환** | Custom Container image에 Pandoc 필요. 현재 reference API에는 미노출 |
| **PDF 텍스트 추출·페이지 조작** | Custom Container (Poppler 필요) |
| **CJK 폰트 렌더링 고정** | Custom Container |
| **도구 버전을 image digest로 고정** | Custom Container |

즉 Custom Container의 가치는 "Office 파일을 만든다"가 아니라 **"변환 fidelity와 도구 버전을 통제한다"** 에 있다. 이 구분을 먼저 정리해야 불필요한 ready session 비용을 피할 수 있다.

이 실습의 reference API는 네 형식의 **생성**, 허용 목록 기반 Office→PDF·TXT **변환**, 선언적 **편집**을 검증한다. Image에는 Pandoc이 포함돼 있지만 Markdown·HTML 변환 endpoint는 구현하지 않았다. 임의 shell을 노출하지 않고 [권장 아키텍처의 Office 작업 API](../docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md#47-office-작업-api)가 정의한 경계를 따른다.

## 1. 사전 조건

- Bash 또는 Azure Cloud Shell과 Azure CLI 2.79.0 이상
- Azure CLI 로그인
- 대상 subscription의 Contributor
- role assignment를 위한 Owner 또는 User Access Administrator
- repository root에서 명령 실행
- `office-container/`의 Dockerfile, server.py, probes.yaml
- `curl`, `jq`, Python 3, `unzip`, `file`
- `shasum` 또는 `sha256sum`

현재 subscription과 필수 도구를 확인한다.

```bash
az account show --query '{name:name,id:id,user:user.name}' --output table
command -v az curl jq python3 unzip file
```

Fast Path는 필요한 extension과 provider를 직접 준비하므로 실습 1이나 Python pool이 필요 없다. 수동 명령을 따라갈 때만 [실습 1A의 CLI와 provider 준비](01A_Python_Code_Interpreter_Admin_Lab.md#3-cli와-provider-준비)를 먼저 완료한다.

Fast Path와 수동 절차는 같은 리소스를 만드는 **대체 경로**다. 처음 수행한다면 Fast Path만 실행하고, 실패 원인을 찾거나 개별 Azure 명령을 학습할 때만 2~16절의 수동 절차를 사용한다.

### 권장 Fast Path

repository root에서 다음 명령을 실행하면 ACR, identity, workspace, environment, pool, 네 형식과 hash, session, logs와 metrics를 자동 검증한다. 결과는 `.work/office/`에 저장한다.

```bash
bash scripts/check-prereqs.sh
bash scripts/office-lab.sh
```

기본값은 현재 `az` subscription, `koreacentral`과 공통 실습 Resource Group이다. ACR 이름은 subscription ID에서 생성하며 이미 사용 중이면 `ACR_NAME`을 Azure 전체에서 고유한 값으로 설정해 다시 실행한다. 다른 기본값은 2절의 환경 변수로 재정의할 수 있다.

아래 절은 자동 스크립트가 수행하는 수동 명령을 설명한다.

## 2. 변수 설정

ACR 이름은 Azure 전체에서 고유한 영문 소문자와 숫자 조합이어야 한다.

```bash
export SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}"
export RESOURCE_GROUP="rg-ai-workspace-sandbox-lab"
export LOCATION="koreacentral"
export ACR_NAME="${ACR_NAME:-aiws$(printf '%s' "$SUBSCRIPTION_ID" | tr -d '-' | cut -c1-20)}"
export IDENTITY_NAME="id-ai-workspace-office-acr-pull"
export LOG_WORKSPACE_NAME="log-ai-workspace-sandbox"
export CONTAINER_ENV_NAME="env-ai-workspace-sandbox"
export OFFICE_POOL_NAME="ai-workspace-office-sbx"
export SESSION_API_VERSION="${SESSION_API_VERSION:-2025-02-02-preview}"
export IMAGE_REPOSITORY="office-sandbox"
export IMAGE_TAG="$(date -u +%Y%m%d%H%M%S)"
export IMAGE="$ACR_NAME.azurecr.io/$IMAGE_REPOSITORY:$IMAGE_TAG"
export REPO_ROOT="$PWD"
export LAB_WORK_DIR="$PWD/.work/office-manual"

az account set --subscription "$SUBSCRIPTION_ID"
```

기본 ACR 이름은 subscription ID에서 생성한다. `az acr check-name --name "$ACR_NAME"`이 사용할 수 없다고 나오면 영문 소문자와 숫자로 된 다른 전역 고유 이름을 `ACR_NAME`에 지정한다.

`SESSION_API_VERSION`은 이 repository에서 검증한 management API 값이다. Preview 오류가 발생하면 [공식 session 문서](https://learn.microsoft.com/azure/container-apps/sessions-usage)에서 endpoint와 request shape를 확인한 뒤 이 환경 변수만 재정의한다.

## 3. Quota 확인

```bash
export QUOTA_SCOPE="/subscriptions/$SUBSCRIPTION_ID/providers/Microsoft.App/locations/$LOCATION"

az quota show \
  --resource-name ManagedEnvironmentCount \
  --scope "$QUOTA_SCOPE" \
  --query '{limit:properties.limit.value}' \
  --output json

az quota usage show \
  --resource-name ManagedEnvironmentCount \
  --scope "$QUOTA_SCOPE" \
  --query '{usage:properties.usages.value}' \
  --output json

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

새 Office 환경과 pool을 모두 만들려면 다음 두 quota의 `limit - usage`가 각각 최소 1이어야 한다.

| Quota | 필요한 경우 |
| --- | --- |
| `ManagedEnvironmentCount` | 같은 이름의 Container Apps Environment가 없을 때 |
| `SessionPools` | 같은 이름의 Office session pool이 없을 때 |

CLI가 값을 반환하지 않거나 사용 가능 수량이 0이면 [Azure Portal My quotas](https://portal.azure.com/#view/Microsoft_Azure_Capacity/QuotaMenuBlade/~/myQuotas)에서 Provider를 **Azure Container Apps**로 선택하고 `LOCATION`과 같은 region의 quota를 확인한다. 부족하면 증가를 요청하거나 기존 Environment·pool 재사용 여부를 결정한다.

## 4. ACR 생성

```bash
az provider register \
  --namespace Microsoft.ContainerRegistry \
  --wait

az acr check-name \
  --name "$ACR_NAME" \
  --output table

az acr create \
  --name "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Basic \
  --admin-enabled false \
  --tags purpose=ai-workspace-office-sandbox \
  --output none
```

통과 기준:

- `provisioningState: Succeeded`
- ACR admin user 비활성화

```bash
az acr show \
  --name "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{
    name:name,
    loginServer:loginServer,
    state:provisioningState,
    adminEnabled:adminUserEnabled
  }' \
  --output json
```

## 5. Image pull identity

```bash
az identity create \
  --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --tags purpose=ai-workspace-office-image-pull \
  --output none

export IDENTITY_ID=$(az identity show \
  --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query id \
  --output tsv)

export IDENTITY_PRINCIPAL_ID=$(az identity show \
  --name "$IDENTITY_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query principalId \
  --output tsv)

export ACR_ID=$(az acr show \
  --name "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query id \
  --output tsv)

az role assignment create \
  --role AcrPull \
  --assignee-object-id "$IDENTITY_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --scope "$ACR_ID"
```

이 identity는 image pull 전용이다. runtime에서 Azure token을 얻는 용도로 사용하지 않는다.

## 6. Log Analytics

```bash
az monitor log-analytics workspace create \
  --name "$LOG_WORKSPACE_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --retention-time 30 \
  --tags purpose=ai-workspace-sandbox-monitoring \
  --output none

export LOG_WORKSPACE_ID=$(az monitor log-analytics workspace show \
  --name "$LOG_WORKSPACE_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query customerId \
  --output tsv)

export LOG_WORKSPACE_KEY=$(az monitor log-analytics workspace get-shared-keys \
  --name "$LOG_WORKSPACE_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query primarySharedKey \
  --output tsv)
```

workspace key를 shell history, log, 문서에 기록하지 않는다.

## 7. Container Apps Environment

```bash
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

az containerapp env show \
  --name "$CONTAINER_ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{
    state:properties.provisioningState,
    workloadProfiles:properties.workloadProfiles,
    workspace:properties.appLogsConfiguration.logAnalyticsConfiguration.customerId
  }' \
  --output json
```

`state: Succeeded`와 Consumption workload profile을 확인한다.

## 8. Office image 빌드

Image 구성:

- Python 3.12 slim
- digest로 고정한 Python base image와 Debian snapshot
- LibreOffice Writer, Calc, Impress
- Pandoc
- Poppler
- OpenPyXL
- python-pptx
- Noto CJK와 Liberation fonts
- 비루트 UID 10001
- `/health`, `/generate`, `/convert`, `/edit`, `/files/{job}/{file}` API
- 요청 1MB, content 100,000자, session당 job 20개, 임시 저장공간 256MB, 편집 operation 50개 제한
- 1시간이 지난 job의 자동 정리와 streaming download
- 고정된 변환 matrix와 편집 operation 허용 목록

```bash
az acr build \
  --registry "$ACR_NAME" \
  --image "$IMAGE_REPOSITORY:$IMAGE_TAG" \
  --file "$REPO_ROOT/office-container/Dockerfile" \
  "$REPO_ROOT/office-container"
```

Image digest를 기록한다.

```bash
az acr repository show \
  --name "$ACR_NAME" \
  --image "$IMAGE_REPOSITORY:$IMAGE_TAG" \
  --query '{digest:digest,created:createdTime}' \
  --output json
```

Production에서는 고유 tag와 digest, SBOM, vulnerability scan 결과를 release 기록에 포함한다.

## 9. Office Custom Container pool

`--registry-identity`가 pool identity 연결을 처리하므로 같은 identity를 `--mi-user-assigned`로 중복 지정하지 않는다.

```bash
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
  --output none
```

CLI가 장기 작업 polling 중 timeout을 반환해도 Azure 작업이 성공했을 수 있다. 먼저 resource를 조회한다.

```bash
az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{
    state:properties.provisioningState,
    endpoint:properties.poolManagementEndpoint,
    image:properties.customContainerTemplate.containers[0].image,
    readySessions:properties.scaleConfiguration.readySessionInstances,
    probeTypes:properties.customContainerTemplate.containers[0].probes[].type,
    network:properties.sessionNetworkConfiguration.status
  }' \
  --output json
```

통과 기준:

- `state: Succeeded`
- image가 지정한 ACR image
- ready session 1
- Startup과 Liveness probe
- `EgressDisabled`

## 10. Session Executor 역할

`Contributor`는 resource를 만들 수 있지만 role assignment 권한은 없다. `AuthorizationFailed`가 나오면 `Owner` 또는 `User Access Administrator`에게 이 절의 역할 할당을 요청한다.

```bash
export OFFICE_POOL_ID=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
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
  --scope "$OFFICE_POOL_ID"
```

실제 AI Workspace에서는 사용자가 아니라 backend Managed Identity에 부여한다.

Service Principal로 실행한다면 미리 `CALLER_OBJECT_ID`를 principal object ID로, `CALLER_PRINCIPAL_TYPE=ServicePrincipal`로 설정한다.

역할 할당을 확인한다.

```bash
az role assignment list \
  --assignee "$CALLER_OBJECT_ID" \
  --scope "$OFFICE_POOL_ID" \
  --query "[?roleDefinitionName=='Azure ContainerApps Session Executor'].{role:roleDefinitionName,scope:scope}" \
  --output table
```

역할 전파는 수 분 걸릴 수 있다. `/health`가 403이면 30~60초 기다린 뒤 `TOKEN`을 다시 발급한다.

## 11. Health endpoint

이후 생성되는 응답과 문서는 `.work/office-manual/`에 저장한다.

```bash
mkdir -p "$LAB_WORK_DIR"
cd "$LAB_WORK_DIR"

export TOKEN=$(az account get-access-token \
  --resource https://dynamicsessions.io \
  --query accessToken \
  --output tsv)

export OFFICE_ENDPOINT=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.poolManagementEndpoint \
  --output tsv)

export OFFICE_SESSION_ID="office-$(uuidgen | tr '[:upper:]' '[:lower:]')"
```

이 시점부터 16절까지의 수동 명령은 repository root가 아니라 `$LAB_WORK_DIR`에서 실행한다.

Custom Container session에도 SSH·RDP나 Azure Portal terminal로 접속하지 않는다. Management endpoint 뒤의 path가 container HTTP API로 전달되며, 이 reference image는 HTML 화면 대신 JSON과 파일 stream만 반환한다.

```bash
curl --fail-with-body --silent --show-error \
  "$OFFICE_ENDPOINT/health?identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output health.json \
  --write-out 'health HTTP %{http_code}\n'

cat health.json
```

예상 응답:

```json
{
  "status": "ok",
  "tools": {
    "libreoffice": "LibreOffice ...",
    "openpyxl": "3.1.5",
    "pandoc": "pandoc ...",
    "pdftotext": "pdftotext version ...",
    "python-docx": "1.2.0",
    "python-pptx": "1.0.2"
  },
  "operations": {
    "generate": ["docx", "pdf", "pptx", "xlsx"],
    "convert": [
      { "source": "report.docx", "target": "pdf" },
      { "source": "report.docx", "target": "txt" },
      { "source": "report.pptx", "target": "pdf" },
      { "source": "report.xlsx", "target": "pdf" }
    ],
    "edit": ["renameSheet", "replaceText", "setCell"]
  },
  "limits": {
    "maxRequestBytes": 1048576,
    "maxJobs": 20,
    "maxStorageBytes": 268435456,
    "maxEditOperations": 50,
    "jobTtlSeconds": 3600
  }
}
```

`operations`가 AI Workspace Agent에 노출되는 계약이다. Agent는 이 목록 밖의 작업을 요청할 수 없고, shell command나 LibreOffice argument를 직접 전달할 수도 없다.

## 12. DOCX, PDF, PPTX와 XLSX 생성

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_ENDPOINT/generate?identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "title": "AI Workspace 격리형 Sandbox 검증 보고서",
    "content": "Azure Container Apps Dynamic Sessions의 Office Custom Container에서 생성했습니다.\n\n- Python 작업과 Office 작업 분리\n- 기본 egress 차단\n- 승인 전 실제 업무 시스템 반영 금지"
  }' \
  --output generate.json \
  --write-out 'generate HTTP %{http_code}\n'

cat generate.json
```

응답은 `jobId`와 DOCX, PDF, PPTX, XLSX 각각의 `name`, `size`, `sha256`, `downloadPath`를 포함한다.

## 13. 파일 다운로드와 검증

`jq`가 설치된 환경의 예:

```bash
sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

export DOCX_PATH=$(jq -r \
  '.files[] | select(.name == "report.docx") | .downloadPath' \
  generate.json)

export PDF_PATH=$(jq -r \
  '.files[] | select(.name == "report.pdf") | .downloadPath' \
  generate.json)

export PPTX_PATH=$(jq -r \
  '.files[] | select(.name == "report.pptx") | .downloadPath' \
  generate.json)

export XLSX_PATH=$(jq -r \
  '.files[] | select(.name == "report.xlsx") | .downloadPath' \
  generate.json)

for SPEC in \
  "report.docx:$DOCX_PATH" \
  "report.pdf:$PDF_PATH" \
  "report.pptx:$PPTX_PATH" \
  "report.xlsx:$XLSX_PATH"; do
  FILE_NAME=${SPEC%%:*}
  DOWNLOAD_PATH=${SPEC#*:}
  EXPECTED_HASH=$(jq -r --arg name "$FILE_NAME" \
    '.files[] | select(.name == $name) | .sha256' \
    generate.json)
  curl --fail-with-body --silent --show-error \
    "$OFFICE_ENDPOINT$DOWNLOAD_PATH?identifier=$OFFICE_SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --output "$FILE_NAME" \
    --write-out "$FILE_NAME download HTTP %{http_code}\n"
  ACTUAL_HASH=$(sha256_file "$FILE_NAME")
  test "$ACTUAL_HASH" = "$EXPECTED_HASH"
done

test -s report.docx
test -s report.pdf
test -s report.pptx
test -s report.xlsx
unzip -t report.docx
unzip -t report.pptx
unzip -t report.xlsx
head -c 4 report.pdf
file report.docx report.pdf report.pptx report.xlsx
```

통과 기준:

- generate HTTP 200
- 네 download HTTP 200
- DOCX, PPTX, XLSX ZIP 구조 정상
- PDF header `%PDF`
- 실제 SHA-256이 generate 응답과 일치

## 13.1 허용 목록 기반 형식 변환

"문서 변환" 요건을 검증한다. 변환은 임의 source-target 조합이 아니라 서버가 고정한 matrix로만 가능하다.

```bash
export JOB_ID=$(jq -r '.jobId' generate.json)
```

먼저 허용되지 않은 변환이 거부되는지 확인한다.

```bash
curl --silent \
  --request POST \
  "$OFFICE_ENDPOINT/convert?identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"source\":\"report.docx\",\"target\":\"exe\"}" \
  --output convert-denied.json \
  --write-out 'denied conversion HTTP %{http_code}\n'

cat convert-denied.json
```

HTTP 400과 허용 목록이 응답에 포함돼야 한다.

허용된 PPTX -> PDF 변환을 실행한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_ENDPOINT/convert?identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"source\":\"report.pptx\",\"target\":\"pdf\"}" \
  --output convert.json \
  --write-out 'convert HTTP %{http_code}\n'

CONVERTED_PATH=$(jq -r '.files[0].downloadPath' convert.json)
CONVERTED_HASH=$(jq -r '.files[0].sha256' convert.json)

curl --fail-with-body --silent --show-error \
  "$OFFICE_ENDPOINT$CONVERTED_PATH?identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --output report.pptx.pdf

test "$(sha256_file report.pptx.pdf)" = "$CONVERTED_HASH"
head -c 4 report.pptx.pdf
file report.pptx.pdf
```

통과 기준:

- 허용되지 않은 target은 HTTP 400
- 허용된 변환은 HTTP 200이고 PDF header가 `%PDF`
- 다운로드한 파일의 SHA-256이 응답과 일치

## 13.2 선언적 문서 편집

"문서 편집" 요건을 검증한다. 편집은 자유 형식 코드가 아니라 선언적 operation 목록으로만 지시한다.

허용되지 않은 operation이 거부되는지 먼저 확인한다.

```bash
curl --silent \
  --request POST \
  "$OFFICE_ENDPOINT/edit?identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[{\"op\":\"runShell\",\"cmd\":\"id\"}]}" \
  --output edit-denied.json \
  --write-out 'denied edit HTTP %{http_code}\n'

cat edit-denied.json
```

수식 주입도 거부된다. XLSX 수식은 다른 셀과 외부 자원을 참조할 수 있어 기본 차단한다.

```bash
curl --silent \
  --request POST \
  "$OFFICE_ENDPOINT/edit?identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[{\"op\":\"setCell\",\"cell\":\"B2\",\"value\":\"=1+1\"}]}" \
  --output edit-formula.json \
  --write-out 'formula edit HTTP %{http_code}\n'
```

허용된 편집을 적용한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_ENDPOINT/edit?identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[
    {\"op\":\"setCell\",\"cell\":\"B2\",\"value\":\"approved-draft\"},
    {\"op\":\"renameSheet\",\"name\":\"Final\"},
    {\"op\":\"replaceText\",\"find\":\"Dynamic Sessions\",\"replace\":\"검토 완료\"}
  ]}" \
  --output edit.json \
  --write-out 'edit HTTP %{http_code}\n'

cat edit.json
```

편집 결과를 내려받아 확인한다.

```bash
for FILE_NAME in report.edited.docx report.edited.xlsx; do
  DOWNLOAD_PATH=$(jq -r --arg name "$FILE_NAME" \
    '.files[] | select(.name == $name) | .downloadPath' edit.json)
  EXPECTED_HASH=$(jq -r --arg name "$FILE_NAME" \
    '.files[] | select(.name == $name) | .sha256' edit.json)
  curl --fail-with-body --silent --show-error \
    "$OFFICE_ENDPOINT$DOWNLOAD_PATH?identifier=$OFFICE_SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --output "$FILE_NAME"
  test "$(sha256_file "$FILE_NAME")" = "$EXPECTED_HASH"
  unzip -t "$FILE_NAME"
done
```

통과 기준:

| 요청 | 기대 결과 |
| --- | --- |
| `{"op":"runShell","cmd":"id"}` | HTTP 400, 허용 목록 반환 |
| `{"op":"setCell","value":"=1+1"}` | HTTP 400, 수식 거부 |
| 허용된 operation 3개 | HTTP 200, `applied: 3`, 파일 2개 |
| 존재하지 않는 `jobId` | HTTP 404 |
| 편집 결과 다운로드 | HTTP 200, hash 일치, ZIP 구조 정상 |

원본은 유지되고 편집 결과는 `report.edited.docx`, `report.edited.xlsx`로 따로 생성된다. 원본 보존은 Diff와 승인 심사에 필요하다.

설계 원칙:

- Agent가 전달할 수 있는 것은 **operation 이름과 데이터**뿐이다. 실행 방법은 서버가 정한다.
- cell 참조, sheet 이름, 문자열 길이를 정규식과 상한으로 검증한다.
- operation 수를 제한한다 (기본 50개).
- 새 편집 기능은 코드를 열어주는 것이 아니라 operation을 하나 추가하는 방식으로 확장한다.

## 14. Session 조회

Custom Container management API는 `POST`를 사용한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_ENDPOINT/.management/getSession?api-version=$SESSION_API_VERSION&identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
```

## 15. Monitoring

### 15.1 로그 수집 경로 두 가지

세션 로그는 **어떻게 수집하느냐에 따라 테이블 이름이 다르다.** 이 차이 때문에 KQL이 `SemanticError`로 실패하는 일이 흔하다.

| 수집 방법 | 설정 위치 | 얻는 테이블 |
| --- | --- | --- |
| Environment 로그 대상 (`--logs-destination log-analytics`) | 7절에서 이미 설정함 | `AppEnvSessionConsoleLogs_CL` **하나뿐** |
| Azure Monitor 진단 설정 (resource-specific) | Environment 리소스 | `AppEnvSessionConsoleLogs`, `AppEnvSessionLifecycleLogs`, `AppEnvSessionPoolEventLogs` |

> **7절 설정만으로는 lifecycle과 pool event 로그를 볼 수 없다.** console 로그만 `_CL` 테이블로 들어온다. 나머지 두 종류는 진단 설정을 따로 만들어야 한다.

먼저 현재 상태를 확인한다.

```bash
WORKSPACE_GUID=$(az monitor log-analytics workspace show \
  --name "$LOG_WORKSPACE_NAME" --resource-group "$RESOURCE_GROUP" \
  --query customerId --output tsv)

az monitor log-analytics query \
  --workspace "$WORKSPACE_GUID" \
  --analytics-query 'search * | where TimeGenerated > ago(1h) | summarize Records=count() by $table' \
  --output table
```

`AppEnvSessionConsoleLogs_CL`만 보이면 다음 절을 수행한다.

### 15.2 lifecycle과 pool event 로그 활성화

진단 설정은 session pool이 아니라 **Container Apps Environment** 리소스에 만든다. pool 리소스는 `AllMetrics`만 지원한다.

사용 가능한 카테고리를 먼저 확인한다.

```bash
ENV_ID=$(az containerapp env show \
  --name "$CONTAINER_ENV_NAME" --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv)

az monitor diagnostic-settings categories list \
  --resource "$ENV_ID" \
  --query "value[?contains(name,'Session')].name" --output tsv
```

실제 출력이다.

```text
AppEnvSessionConsoleLogs
AppEnvSessionPoolEventLogs
AppEnvSessionLifeCycleLogs
```

> ⚠️ 카테고리 이름과 테이블 이름의 대소문자가 다르다. 카테고리는 `AppEnvSession**LifeCycle**Logs`(대문자 C)인데 테이블은 `AppEnvSession**Lifecycle**Logs`(소문자 c)다. 카테고리 이름을 그대로 KQL에 쓰면 실패한다.

진단 설정을 만든다. **필요한 것은 카테고리 3개를 켜는 것뿐이다.**

```bash
WORKSPACE_ID=$(az monitor log-analytics workspace show \
  --name "$LOG_WORKSPACE_NAME" --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv)

az monitor diagnostic-settings create \
  --name session-diagnostics \
  --resource "$ENV_ID" \
  --workspace "$WORKSPACE_ID" \
  --logs '[
    {"category":"AppEnvSessionConsoleLogs","enabled":true},
    {"category":"AppEnvSessionPoolEventLogs","enabled":true},
    {"category":"AppEnvSessionLifeCycleLogs","enabled":true}
  ]' \
  --output none
```

> 이 세 카테고리는 `logAnalyticsDestinationType` 값과 무관하게 **항상 resource-specific 테이블로 들어간다.** 2026-07-25에 `logAnalyticsDestinationType`이 `null`(기본값)인 상태에서도 `AppEnvSessionLifecycleLogs`와 `AppEnvSessionPoolEventLogs`에 정상 수집되는 것을 확인했다. `AzureDiagnostics` 테이블은 만들어지지 않았다.
>
> 따라서 `--export-to-resource-specific true`를 붙일 필요가 없다. 참고로 `containerapp` 확장(1.3.0b4)이 설치된 환경에서는 이 플래그를 줘도 `logAnalyticsDestinationType`이 `null`로 남는데, 위 이유로 결과에는 영향이 없다.

설정을 확인한다. `containerapp` 확장이 `az monitor diagnostic-settings show` 출력을 가릴 수 있으므로 ARM REST로 확인한다.

```bash
az rest --method get \
  --url "https://management.azure.com${ENV_ID}/providers/Microsoft.Insights/diagnosticSettings?api-version=2021-05-01-preview" \
  --query "value[].{name:name,logs:properties.logs[?enabled].category}" \
  --output json
```

통과 기준: `logs`에 세 카테고리가 모두 있어야 한다.

설정 후 세션을 한 번 만들어 이벤트를 발생시킨다.

```bash
DIAG_SESSION_ID="office-$(uuidgen | tr '[:upper:]' '[:lower:]')"

curl --silent --output /dev/null --write-out 'health HTTP %{http_code}\n' \
  "$OFFICE_ENDPOINT/health?identifier=$DIAG_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"

curl --silent --output /dev/null --write-out 'stop HTTP %{http_code}\n' \
  --request POST \
  "$OFFICE_ENDPOINT/.management/stopSession?api-version=$SESSION_API_VERSION&identifier=$DIAG_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
```

> 첫 수집까지 **2~5분** 걸린다. 바로 조회하면 테이블이 없다고 나온다.

### 15.3 KQL

수집 경로에 맞는 테이블을 쓴다.

```kusto
// Environment 로그 대상으로 수집한 console 로그
AppEnvSessionConsoleLogs_CL
| where TimeGenerated > ago(1h)
| order by TimeGenerated desc
| take 100
```

```kusto
// 진단 설정(resource-specific)으로 수집한 session lifecycle
AppEnvSessionLifecycleLogs
| where TimeGenerated > ago(1h)
| project TimeGenerated, SessionPoolName, OperationName, Log
| order by TimeGenerated desc
```

```kusto
// 진단 설정(resource-specific)으로 수집한 pool event
AppEnvSessionPoolEventLogs
| where TimeGenerated > ago(1h)
| project TimeGenerated, SessionPoolName, OperationName, Log
| order by TimeGenerated desc
```

세 테이블의 주요 컬럼은 동일하다.

```text
TimeGenerated, SessionPoolName, OperationName, Log,
Level, Location, NodeName, PodName, _ResourceId
```

통과 기준:

```bash
az monitor log-analytics query \
  --workspace "$WORKSPACE_GUID" \
  --analytics-query 'search * | where TimeGenerated > ago(1h) | summarize Records=count() by $table' \
  --output table
```

세 테이블이 모두 보여야 한다. 2026-07-25 실제 출력이다.

```text
$table                       Records
---------------------------  -------
AppEnvSessionPoolEventLogs        14
AppEnvSessionLifecycleLogs         6
AppEnvSessionConsoleLogs_CL      278
```

### 15.4 Metrics

```bash
POOL_ID=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" --resource-group "$RESOURCE_GROUP" \
  --query id --output tsv)

az monitor metrics list \
  --resource "$POOL_ID" \
  --metric PoolReadyPodCount PoolExecutingPodCount PoolPendingPodCount \
  --interval PT1M --aggregation Average Maximum \
  --output table
```

확인할 metric:

- `PoolExecutingPodCount`
- `PoolPendingPodCount`
- `PoolReadyPodCount`

## 16. 문제 해결

| 증상 | 원인과 조치 |
| --- | --- |
| ACR build가 Dockerfile을 찾지 못함 | `--file "$PWD/office-container/Dockerfile"`처럼 절대 경로 사용 |
| Duplicate identity JSON property | `--registry-identity`와 동일 identity의 `--mi-user-assigned` 중복 제거 |
| `SessionPoolInvalidReadySessionInstances` | Custom pool ready sessions를 1 이상, max sessions 미만으로 설정 |
| CLI polling network timeout | `sessionpool show`로 Azure 측 provisioning 상태 확인 |
| Image pull 실패 | AcrPull scope, identity principal과 role propagation 확인 |
| Probe 실패 | image port 8080, `/health`, probe YAML 확인 |
| HTTP 403 | Session Executor 전파 후 token 재발급 |
| DOCX는 생성되고 PDF가 없음 | LibreOffice stderr, HOME 쓰기 권한, profile path 확인 |
| 글꼴이 깨짐 | 필요한 CJK·업무 font를 image에 포함하고 재빌드 |
| `/convert` HTTP 400 | 허용 변환 matrix에 없는 조합이다. 응답의 `allowed` 확인 |
| `/edit` HTTP 400 `Unsupported operation` | 허용 operation 목록 밖이다. `/health`의 `operations.edit` 확인 |
| `/edit` HTTP 400 `Formula values are not allowed` | 의도된 동작이다. 수식은 기본 차단한다 |
| `/convert` 또는 `/edit` HTTP 404 | `jobId`가 없거나 job TTL 1시간이 지났다 |
| HTTP 429 | 동시 session 한도(`--max-sessions`)에 걸렸다. 기존 session을 stop하거나 한도를 올린다 |
| HTTP 400 `SessionRequestValidationFailed` | `identifier`, `api-version`, endpoint와 method 확인. 응답의 `target`, `traceId` 기록 |
| HTTP 400 `SessionRequestNotSupported` | management endpoint와 container endpoint를 혼동하지 않았는지 확인 |
| KQL `SemanticError`로 테이블 없음 | 테이블 이름 또는 수집 경로 문제다. 15.1~15.2절 참고 |
| `AppEnvSessionConsoleLogs_CL`만 보임 | 진단 설정이 없다. 15.2절을 수행한다 |
| 진단 설정 후에도 테이블 없음 | 수집까지 2~5분 걸린다. 세션을 한 번 만든 뒤 다시 조회한다 |

## 17. 정리

> 현재 리소스를 보존해야 하면 이 절을 실행하지 않는다.

이 실습이 만드는 리소스와 비용 성격이다.

| 리소스 | pool 삭제 후에도 남는가 | 비용 성격 |
| --- | --- | --- |
| Custom Container session pool | 아니오 | ready session 1개가 상시 과금 |
| Container Apps Environment | 예 | workload profile 구성에 따라 고정비 발생 가능 |
| Azure Container Registry (Basic) | 예 | 저장소 고정비 |
| Log Analytics workspace | 예 | 수집량과 보존 기간 과금 |
| User-assigned Managed Identity | 예 | 무료 |

따라서 **pool만 지우면 비용이 다 사라지지 않는다.** 실습 전용 Resource Group을 썼다면 그룹 단위 삭제가 가장 확실하다.

Fast Path로 시작한 경우에도 동작하도록 repository root로 돌아가 기본 이름을 다시 설정하고 삭제 대상을 확인한다.

```bash
cd "${REPO_ROOT:-$PWD}"
test -f scripts/cleanup.sh || {
  echo "repository root에서 실행해야 합니다" >&2
  exit 1
}
export RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ai-workspace-sandbox-lab}"
export OFFICE_POOL_NAME="${OFFICE_POOL_NAME:-ai-workspace-office-sbx}"
az group show --name "$RESOURCE_GROUP" \
  --query '{name:name,location:location,id:id}' --output table
```

수동 경로에서 만든 Session 중지:

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_ENDPOINT/.management/stopSession?api-version=$SESSION_API_VERSION&identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
```

Office pool만 삭제 (Environment, ACR, Log Analytics는 유지):

```bash
az containerapp sessionpool delete \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --yes
```

전체 삭제:

```bash
CONFIRM_DELETE=yes bash scripts/cleanup.sh
```

또는 직접 실행한다.

```bash
az group delete --name "$RESOURCE_GROUP" --yes --no-wait
```

## 18. 실제 검증 기록

2026-07-24 한국 중부 리전:

- ACR Basic, admin user disabled
- user-assigned identity와 AcrPull
- workload profiles-enabled environment
- Log Analytics 연결
- image build 성공
- Custom Container pool `Succeeded`
- ready session 1
- Startup·Liveness probe
- `/health` HTTP 200
- LibreOffice 7.4.7.2, Pandoc 2.17.1.1, Poppler 22.12.0
- OpenPyXL 3.1.5, python-pptx 1.0.2
- DOCX, PDF, PPTX, XLSX generate HTTP 200
- 네 파일 다운로드, 형식과 SHA-256 검증 성공
- Custom session management API 성공
- `AppEnvSessionConsoleLogs_CL` ingestion 확인
- ready·executing·pending pool metric 조회 성공

2026-07-25 한국 중부 리전 추가 검증:

| 항목 | 결과 |
| --- | --- |
| `/health` operations 계약 노출 | generate 4종, convert 4조합, edit 3종 |
| python-docx 1.2.0 image 포함 | 확인 |
| 허용된 PPTX → PDF 변환 | HTTP 200, `%PDF` header, SHA-256 일치 |
| 허용 목록 밖 변환 (`target: exe`) | HTTP 400, 허용 목록 반환 |
| 선언적 편집 3개 operation | HTTP 200, `applied: 3`, 파일 2개 생성 |
| `runShell` operation 시도 | HTTP 400 |
| XLSX 수식 주입 (`=1+1`) | HTTP 400 |
| 존재하지 않는 `jobId` | HTTP 404 |
| 편집 결과 hash와 ZIP 구조 | 일치, 정상 |
| Clean 재빌드와 pool image 업데이트 | 성공 |
| 로컬 컨테이너 smoke (생성·변환·편집) | 통과 |

2026-07-25 문서 명령 그대로 실행한 검증(자동 스크립트 아님):

| 절 | 결과 |
| --- | --- |
| §11 `/health` | HTTP 200, `operations`에 generate·convert·edit 노출 |
| §12 생성 | HTTP 200, 네 파일 |
| §13.1 비허용 변환 | HTTP 400 `Conversion is not allowed` |
| §13.1 PPTX→PDF | HTTP 200, hash 일치, `PDF document, version 1.6, 2 pages` |
| §13.2 `runShell` | HTTP 400, 허용 목록 반환 |
| §13.2 수식 주입 | HTTP 400 `Formula values are not allowed` |
| §13.2 허용 편집 3개 | HTTP 200, `applied: 3`, 파일 2개, hash·ZIP 정상 |
| §13.2 없는 `jobId` | HTTP 404 |
| §14 `getSession` | HTTP 200 |
| §15 metrics | 세 metric 조회 성공 |
| §15 KQL 3종 | 진단 설정 후 세 테이블 모두 데이터 반환 |

> 이 검증에서 문서 오류 두 건을 발견해 고쳤다. 첫째, KQL 테이블 이름이 틀렸다(`AppEnvSessionPoolEvents`, `AppEnvSessionLifecycleLogs_CL`은 존재하지 않는다). 둘째, lifecycle·pool event 로그는 Environment 로그 대상 설정만으로는 수집되지 않고 **별도 진단 설정**이 필요하다는 사실이 빠져 있었다. 15.1~15.2절이 이를 다룬다.
