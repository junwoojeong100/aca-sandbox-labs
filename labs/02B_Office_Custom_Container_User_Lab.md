# 실습 2B: Office 생성·변환·편집 - 사용자

## 목표

사용자가 container, shell command, LibreOffice argument를 직접 다루지 않고 허용된 작업만 요청한다.

```text
사용자 문서 요청
  -> AI Workspace의 제한된 Office API
  -> 격리된 Custom Container session
  -> DOCX·PDF·PPTX·XLSX 생성 또는 허용된 변환·편집
  -> 형식·hash 검사
  -> 미리보기와 사용자 승인
```

예상 시간은 20~30분이다.

## 1. 사전 조건

관리자가 [실습 2A](02A_Office_Custom_Container_Admin_Lab.md)를 완료해 다음을 준비해야 한다.

- Office Custom Container pool
- backend의 Session Executor 역할
- Startup·Liveness probe와 `EgressDisabled`
- 사용자 대신 token과 session identifier를 관리하는 backend
- `/health`가 노출하는 허용 operation 계약

이 repository의 `office_gateway/`가 AI Workspace backend를 대리한다. Gateway만 Azure token, pool endpoint와 session identifier를 관리하며 사용자 API에는 public document job ID만 반환한다.

사용자는 Office container 화면이나 shell을 보지 않는다. 사용자 화면에는 문서 요청, 생성·변환·편집 진행 상태, 미리보기·Diff, 검사 결과, 다운로드와 승인 기능만 제공한다.

## 2. 사용자 Gateway 실행

다음 명령은 관리자 또는 실습 운영자가 repository root에서 실행한다.

```bash
export RESOURCE_GROUP="rg-ai-workspace-sandbox-lab"
export OFFICE_POOL_NAME="ai-workspace-office-sbx"
python3 -m office_gateway.server
```

Gateway는 기본적으로 `http://127.0.0.1:8090`에서 실행된다. Azure token과 session identifier는 이 프로세스 안에서만 사용한다.

다른 terminal에서 사용자 API 변수를 설정하고 health를 확인한다.

```bash
export OFFICE_USER_API="http://127.0.0.1:8090"
export DEMO_USER="user-demo"

curl --fail-with-body --silent --show-error \
  "$OFFICE_USER_API/health" | jq
```

> `X-Demo-User`는 localhost 실습에서 ownership 분리를 보여주기 위한 값일 뿐 인증 수단이 아니다. Production에서는 Entra access token을 검증하고 token의 tenant·object ID로 authorization한다.

## 3. 생성 요청

사용자가 요청할 수 있는 예:

```text
제목이 "2026년 분기 보고서"인 DOCX, PDF, PPTX, XLSX 초안을 만들어줘.
본문에는 매출 요약과 검토 전 문서라는 표시를 넣어줘.
```

backend는 이를 임의 shell이 아니라 다음과 같은 선언적 요청으로 변환한다.

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
  --output .work/office-user-create.json

cat .work/office-user-create.json | jq
export DOCUMENT_JOB_ID=$(jq -r '.id' .work/office-user-create.json)
```

통과 기준:

- 네 형식이 생성됨
- 파일마다 이름, 크기와 SHA-256이 반환됨
- public job ID와 파일 이름으로 사용자 다운로드 URL을 구성할 수 있음
- 미리보기 전에는 실제 업무 저장소로 복사되지 않음

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
  --output .work/office-user-report.pdf

file .work/office-user-report.pdf
```

## 4. 변환 요청

허용된 예:

```text
생성한 PPTX를 PDF로 변환해줘.
```

허용 목록 밖의 예:

```text
결과 파일을 실행 파일로 변환해줘.
```

첫 요청은 성공해야 하고 두 번째 요청은 HTTP 400과 허용 가능한 변환 목록을 반환해야 한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID/convert" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{"source":"report.pptx","target":"pdf"}' | jq
```

## 5. 선언적 편집 요청

허용된 예:

- DOCX placeholder 텍스트 교체
- XLSX 지정 cell 값 변경과 sheet 이름 변경
- PPTX placeholder 교체

거부돼야 하는 예:

- `runShell`, 임의 executable 또는 LibreOffice argument
- 허용 목록 밖 operation
- 기본 정책에서 차단한 spreadsheet 수식 주입

사용자에게는 "지원하지 않는 작업"과 허용 가능한 대안을 보여주고 내부 command나 stack trace는 노출하지 않는다.

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
      {"op":"replaceText","find":"검토 전 문서","replace":"검토 완료"}
    ]
  }' | jq
```

## 6. 결과 검토와 승인

사용자는 다음을 확인한다.

1. 파일 형식과 페이지·sheet·slide 미리보기
2. 원본 대비 Diff
3. macro, malware, DLP와 파일 크기 검사 결과
4. 승인 대상 artifact의 SHA-256
5. 최종 저장 위치

승인 전에는 artifact가 staging에만 있어야 한다. 승인 후에는 Sandbox와 분리된 Approval Service가 hash를 다시 확인하고 최소 권한 Connector로 복사한다.

실습 gateway는 파일을 다시 다운로드해 형식·macro·hash를 검사한 뒤 `.work/office-user/approved/`에 승격한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID/approve" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{}' | jq
```

## 7. 사용자 오류 경험

| 상황 | 사용자 응답 |
| --- | --- |
| 지원하지 않는 변환 | 허용된 source-target 조합 안내 |
| 지원하지 않는 편집 | `/health` 계약의 허용 operation 안내 |
| 만료된 job | 다시 생성하거나 입력 파일을 재업로드하도록 안내 |
| session 한도 초과 | 잠시 후 재시도하도록 안내 |
| 문서 생성 실패 | 내부 경로를 제거한 오류 요약과 재시도 여부 |

## 8. 정리

사용자 job을 삭제하면 gateway가 대응하는 Custom Container session을 중지하고 메모리의 job mapping을 제거한다.

```bash
curl --fail-with-body --silent --show-error \
  --request DELETE \
  "$OFFICE_USER_API/api/document-jobs/$DOCUMENT_JOB_ID" \
  --header "X-Demo-User: $DEMO_USER"
```

사용자는 결과 보존 또는 삭제만 요청한다. Pool, ACR·Environment·Log Analytics 정리는 관리자가 [실습 2A §17](02A_Office_Custom_Container_Admin_Lab.md#17-정리)에서 수행한다.
