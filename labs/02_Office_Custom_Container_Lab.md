# 실습 2: Office Custom Container Session Pool

## 목표

LibreOffice, Pandoc, Poppler와 폰트를 포함한 비루트 Custom Container image를 빌드하고 Azure Container Apps Dynamic Sessions에 배포해 다음을 검증한다.

- ACR private image와 Managed Identity pull
- workload profiles-enabled Container Apps Environment
- Log Analytics 연결
- Startup·Liveness probe
- `EgressDisabled`
- `/health` API
- DOCX·PDF·PPTX·XLSX 생성과 다운로드
- 파일 형식과 SHA-256 검증

예상 시간은 60~120분이며 ACR build 시간은 별도다.

> Custom Container pool은 ready session을 최소 1개 요구할 수 있다. 유지되는 동안 비용이 발생할 수 있다.

이 실습의 reference API는 네 형식의 **생성**을 검증한다. 실제 편집·변환 기능은 임의 shell을 노출하지 말고 [권장 아키텍처의 Office 작업 API](../docs/AKeeON_Dynamic_Sessions_Reference_Architecture.md#47-office-작업-api)처럼 선언적 operation과 허용 변환 matrix로 확장한다.

## 1. 사전 조건

- [Python 실습](01_Python_Code_Interpreter_Lab.md)의 CLI와 provider 준비 완료
- 대상 subscription의 Contributor
- role assignment를 위한 Owner 또는 User Access Administrator
- repository root에서 명령 실행
- `office-container/`의 Dockerfile, server.py, probes.yaml

## 2. 변수 설정

ACR 이름은 Azure 전체에서 고유한 영문 소문자와 숫자 조합이어야 한다.

```bash
export SUBSCRIPTION_ID="<SUBSCRIPTION_ID>"
export RESOURCE_GROUP="rg-akeeon-sandbox-lab"
export LOCATION="koreacentral"
export ACR_NAME="<GLOBALLY_UNIQUE_ACR_NAME>"
export IDENTITY_NAME="id-akeeon-office-acr-pull"
export LOG_WORKSPACE_NAME="log-akeeon-sandbox"
export CONTAINER_ENV_NAME="env-akeeon-sandbox"
export OFFICE_POOL_NAME="akeeon-office-sbx"
export IMAGE_REPOSITORY="office-sandbox"
export IMAGE_TAG="20260724.2"
export IMAGE="$ACR_NAME.azurecr.io/$IMAGE_REPOSITORY:$IMAGE_TAG"

az account set --subscription "$SUBSCRIPTION_ID"
```

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
  --tags purpose=akeeon-office-sandbox \
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
  --tags purpose=akeeon-office-image-pull \
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
  --tags purpose=akeeon-sandbox-monitoring \
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
  --tags purpose=akeeon-office-sandbox \
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
- LibreOffice Writer, Calc, Impress
- Pandoc
- Poppler
- OpenPyXL
- python-pptx
- Noto CJK와 Liberation fonts
- 비루트 UID 10001
- `/health`, `/generate`, `/files/{job}/{file}` API

```bash
az acr build \
  --registry "$ACR_NAME" \
  --image "$IMAGE_REPOSITORY:$IMAGE_TAG" \
  --file "$PWD/office-container/Dockerfile" \
  "$PWD/office-container"
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
  --probe-yaml "$PWD/office-container/probes.yaml" \
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

```bash
export OFFICE_POOL_ID=$(az containerapp sessionpool show \
  --name "$OFFICE_POOL_NAME" \
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
  --scope "$OFFICE_POOL_ID"
```

실제 AKeeON에서는 사용자가 아니라 backend Managed Identity에 부여한다.

## 11. Health endpoint

```bash
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
    "pandoc": "pandoc ...",
    "pdftotext": "pdftotext version ..."
  }
}
```

## 12. DOCX, PDF, PPTX와 XLSX 생성

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_ENDPOINT/generate?identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "title": "AKeeON 격리형 Sandbox 검증 보고서",
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
  curl --fail-with-body --silent --show-error \
    "$OFFICE_ENDPOINT$DOWNLOAD_PATH?identifier=$OFFICE_SESSION_ID" \
    --header "Authorization: Bearer $TOKEN" \
    --output "$FILE_NAME" \
    --write-out "$FILE_NAME download HTTP %{http_code}\n"
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
shasum -a 256 report.docx report.pdf report.pptx report.xlsx
```

통과 기준:

- generate HTTP 200
- 네 download HTTP 200
- DOCX, PPTX, XLSX ZIP 구조 정상
- PDF header `%PDF`
- 실제 SHA-256이 generate 응답과 일치

## 14. Session 조회

Custom Container management API는 `POST`를 사용한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_ENDPOINT/.management/getSession?api-version=2025-02-02-preview&identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
```

## 15. Monitoring

Log Analytics에서 환경 구성에 따라 `_CL` suffix가 있는 table 또는 없는 table을 사용한다.

```kusto
AppEnvSessionConsoleLogs
| where TimeGenerated > ago(1h)
| order by TimeGenerated desc
| take 100
```

```kusto
AppEnvSessionLifecycleLogs
| where TimeGenerated > ago(1h)
| order by TimeGenerated desc
```

```kusto
AppEnvSessionPoolEvents
| where TimeGenerated > ago(1h)
| order by TimeGenerated desc
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

## 17. 선택적 정리

> 현재 리소스를 보존해야 하면 이 절을 실행하지 않는다.

Session 중지:

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_ENDPOINT/.management/stopSession?api-version=2025-02-02-preview&identifier=$OFFICE_SESSION_ID" \
  --header "Authorization: Bearer $TOKEN"
```

Office pool 삭제:

```bash
az containerapp sessionpool delete \
  --name "$OFFICE_POOL_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --yes
```

환경, ACR, identity, workspace까지 전용 Resource Group에 만들었다면 Resource Group 삭제로 전체 정리할 수 있다.

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
