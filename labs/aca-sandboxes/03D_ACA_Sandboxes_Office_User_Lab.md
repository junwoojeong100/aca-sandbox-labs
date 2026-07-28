# 실습 3D: ACA Sandboxes Office - 사용자

## 목표

사용자가 LibreOffice, LibreOffice argument, Python 코드를 직접 다루지 않고 허용된 작업만 요청한다.

```text
사용자 문서 요청
  -> AI Workspace의 제한된 Office API
  -> 격리된 Sandbox (exec 기반, HTTP 서버 없음)
  -> DOCX·PDF·PPTX·XLSX 생성 또는 허용된 변환·편집
  -> 형식·hash 검사
  -> 미리보기와 사용자 승인
```

예상 시간은 20~30분이다.

> 이 문서는 최종 사용자가 아니라 **사용자 경험을 REST API로 검증하는 실습 운영자·개발자**가 수행한다.
> 실제 사용자는 terminal, Sandbox ID, Python SDK를 다루지 않는다.

## 사용자 API 경계

| 사용자에게 제공 | Backend에만 보관 |
| --- | --- |
| 공개 document job ID | Sandbox ID와 SDK 객체 |
| 문서 작업과 상태 | SandboxGroup endpoint와 Entra credential |
| 생성 Artifact 이름, 크기, hash | Disk image ID와 내부 exec 명령 |
| 승인과 승격 상태 | Lifecycle 및 owner mapping |

Gateway는 SDK `sandbox.exec()`, `write_file()`, `read_file()`를 사용하지만
사용자 API에는 data-plane 세부 정보를 노출하지 않는다.

## 1. 사전 조건

관리자가 [실습 3C](03C_ACA_Sandboxes_Office_Admin_Lab.md)를 완료해 다음을 준비해야 한다.

- SandboxGroup `ai-workspace-sandboxes` 생성 완료
- Office 도구 포함 disk image 등록 완료
- `Container Apps SandboxGroup Data Owner` 역할 할당
- `azure-containerapps-sandbox` SDK 설치
- 사용자에게 Sandbox ID, SDK 객체, Entra token을 노출하지 않는 backend
- repository root에서 실행 가능한 Bash, `curl`, `jq`, `file`, Python 3

## 2. Sandboxes Office Gateway 실행

두 terminal을 사용한다.

**Terminal A - Gateway 실행:**

```bash
export RESOURCE_GROUP="rg-ai-workspace-aca-sandboxes-lab"
export LOCATION="koreacentral"
export SANDBOX_GROUP_NAME="ai-workspace-sandboxes"
export ACA_PYTHON="${ACA_PYTHON:-.work/aca-sandboxes/venv/bin/python}"
export ACA_EXECUTION_TIMEOUT_SECONDS="${ACA_EXECUTION_TIMEOUT_SECONDS:-900}"

"$ACA_PYTHON" -m aca_sandboxes.office_gateway
```

`ACA_EXECUTION_TIMEOUT_SECONDS=900`은 platform timeout이 아니라 reference
application이 각 `exec` request에 적용하는 limit이다.

Gateway는 기본적으로 `http://127.0.0.1:8090`에서 실행된다. 문서 job마다
Ready 상태의 최신 `office-*` disk image로 Sandbox를 생성한다.
health 응답에 `"backend": "aca-sandboxes"`가 표시돼야 한다.

**Terminal B - 사용자 API:**

```bash
mkdir -p .work/aca-sandboxes/office-user

export OFFICE_USER_API="http://127.0.0.1:8090"
export DEMO_USER="user-demo"

curl --fail-with-body --silent --show-error \
  "$OFFICE_USER_API/health" | jq
```

응답 예:

```json
{
  "status": "ok",
  "backend": "aca-sandboxes"
}
```

허용 작업 계약은 generate 네 형식, 고정 conversion matrix,
`renameSheet`·`replaceText`·`setCell`이다. 목록 밖의 작업은 Sandbox
runner에서 거부한다.

## 3. 생성 요청

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_USER_API/api/document-jobs" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{
    "title": "2026년 분기 보고서",
    "content": "매출 요약\n\n검토 전 문서"
  }' \
  --output .work/aca-sandboxes/office-user/create.json

jq . .work/aca-sandboxes/office-user/create.json
export DOCUMENT_JOB_ID=$(jq -r '.id' .work/aca-sandboxes/office-user/create.json)
```

통과 기준:

- 네 형식이 생성됨 (DOCX, PDF, PPTX, XLSX)
- 파일마다 이름, 크기, SHA-256 반환
- public job ID만 노출, Sandbox ID는 없음
- 미리보기 전에 실제 업무 저장소로 복사되지 않음

상태를 다시 조회한다.

```bash
curl --fail-with-body --silent --show-error \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID" \
  --header "X-Demo-User: $DEMO_USER" | jq
```

결과 파일을 다운로드한다.

```bash
curl --fail-with-body --silent --show-error \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID/files/report.pdf" \
  --header "X-Demo-User: $DEMO_USER" \
  --output .work/aca-sandboxes/office-user/report.pdf

file .work/aca-sandboxes/office-user/report.pdf
```

## 4. 변환 요청

허용된 예:

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID/convert" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{"source":"report.pptx","target":"pdf"}' | jq
```

허용 목록 밖 변환 거부 확인:

```bash
curl --silent \
  --request POST \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID/convert" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{"source":"report.docx","target":"exe"}' | jq
```

HTTP 400과 허용 목록이 반환돼야 한다.

## 5. 선언적 편집 요청

허용된 편집:

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID/edit" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{
    "operations": [
      {"op":"setCell","cell":"B2","value":"approved-draft"},
      {"op":"renameSheet","name":"Final"},
      {"op":"replaceText","find":"검토 전","replace":"검토 완료"}
    ]
  }' | jq
```

거부돼야 하는 요청:

```bash
# runShell 거부
curl --silent \
  --request POST \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID/edit" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{"operations":[{"op":"runShell","cmd":"id"}]}' | jq

# 수식 주입 거부
curl --silent \
  --request POST \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID/edit" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{"operations":[{"op":"setCell","cell":"B2","value":"=1+1"}]}' | jq
```

두 요청 모두 HTTP 400으로 거부돼야 한다.
사용자에게는 "지원하지 않는 작업"과 허용 가능한 대안을 보여주고 내부 exec 명령이나 파일 경로는 노출하지 않는다.

## 6. 결과 검토와 승인

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID/approve" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{}' \
  --output .work/aca-sandboxes/office-user/approved.json

jq '. | {status, files}' .work/aca-sandboxes/office-user/approved.json
```

통과 기준:

- 승인 후 hash 재확인 후 staging에서 승격
- Sandbox ID, SDK 객체, 파일 경로가 응답에 없음

## 7. Sandbox lifecycle과 비용 확인

Office document job은 생성·변환·편집 중 같은 Sandbox를 사용한다. 5분 동안
작업이 없으면 memory suspend되고, 중단 후 1시간이 지나면 server-side
auto-delete된다. 승인 성공 직후에도 Sandbox를 삭제하고, 이후 다운로드는
local approved copy를 사용한다. 미승인 job은 DELETE 요청 시 즉시
Sandbox를 완전히 삭제한다. Gateway 시작 시 `component=office-gateway`
label의 이전 프로세스 Sandbox 중 1시간 이상 지난 `Stopped`·`Suspended`·`Failed`
orphan만 정리하며 같은 검사를 5분마다 반복한다. 명시적으로 삭제하지 않은
draft job도 1시간 TTL 후 in-memory mapping과 SDK client를 정리한다.
동시에 active 상태로 유지할 수 있는 Office document job은 기본 5개이며,
초과 요청은 Sandbox를 만들기 전에 HTTP 429로 거부한다.

## 8. 사용자에게 보여야 하는 정보

| 표시 | 숨김 |
| --- | --- |
| 작업 형식·진행 상태 | Sandbox ID |
| 결과 파일 이름·크기·hash | Entra token, SDK 객체 |
| 허용 작업 계약(`operations`) | 내부 exec 명령·파일 경로 |
| 정책 거부 이유와 허용 대안 | LibreOffice argument |
| 승인·승격 상태 | 다른 tenant의 문서 |
| backend 종류(`aca-sandboxes`) | SandboxGroup 이름·Resource Group |

## 9. 정리

job 삭제:

```bash
curl --fail-with-body --silent --show-error \
  --request DELETE \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID" \
  --header "X-Demo-User: $DEMO_USER"
```

DELETE 응답을 확인한 뒤 Gateway를 실행한 terminal에서 `Ctrl+C`로 종료한다.

Office Sandbox와 disk image의 완전 정리는 관리자가 [실습 3C §14절](03C_ACA_Sandboxes_Office_Admin_Lab.md#14-정리)에서 수행한다.
