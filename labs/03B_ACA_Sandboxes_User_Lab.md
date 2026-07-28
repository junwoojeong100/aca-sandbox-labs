# 실습 3B: ACA Sandboxes - 자연어 Python 분석 사용자

## 목표

사용자가 ACA Sandboxes의 격리 환경을 직접 다루지 않고 다음 흐름을 경험한다.

```text
자연어 요청 + 첨부파일
  -> 정책 분류
  -> 실제 LLM의 계획과 Python 코드 생성
  -> 격리된 Sandbox 실행 (sdk.exec)
  -> 실패 시 제한된 오류로 코드 수정·재실행
  -> 결과 파일 검사와 staging
  -> 사용자 승인 후에만 승격
```

예상 시간은 20~30분이다.

> 이 문서는 최종 사용자가 아니라 **사용자 경험을 REST API로 검증하는 실습 운영자·개발자**가 수행한다. 실제 사용자는 terminal, Azure resource, SDK, Sandbox ID를 다루지 않는다.

## Dynamic Sessions(실습 1B)와의 차이점

| 항목 | Dynamic Sessions (실습 1B) | ACA Sandboxes (실습 3B) |
| --- | --- | --- |
| **Backend 실행 방식** | `POST /executions` REST API | SDK `sandbox.exec()` |
| **파일 I/O** | `POST /files` / `GET /files/{name}/content` | SDK `write_file()` / `read_file()` |
| **세션 식별자** | `?identifier=<uuid>` query param | Sandbox ID (`sandbox_client.sandbox_id`) |
| **상태 보존** | cooldown 후 삭제 | Sandbox suspend/resume |
| **Sandbox 관리** | pool이 자동 할당·반환 | gateway가 Sandbox ID를 관리 |

사용자 API의 요청·응답 형식은 실습 1B와 동일하게 유지한다.  
차이는 내부 backend가 REST curl 대신 SDK를 사용한다는 점뿐이다.

## 1. 사전 조건

관리자가 [실습 3A](03A_ACA_Sandboxes_Admin_Lab.md)를 완료해 다음을 준비해야 한다.

- SandboxGroup `ai-workspace-sandboxes` 생성 완료
- `python-code-interpreter-*` custom disk image가 `Ready`
- `Container Apps SandboxGroup Data Owner` 역할 할당
- `azure-containerapps-sandbox` SDK 설치
- 빠른 검증은 `LLM_PROVIDER=stub`; 실제 모델 사용 시 Azure OpenAI 설정
- 사용자에게 Sandbox ID, SDK 객체, Entra token을 노출하지 않는 backend
- repository root에서 실행할 수 있는 Bash, `curl`, `jq`, Python 3
- Terminal A에서 `az account show`가 성공하는 Azure CLI 로그인

사용자 화면에는 자연어 요청, 진행 상태, LLM 작업 계획, 안전한 오류 요약, 결과 파일·미리보기와 승인 상태만 표시한다. Sandbox ID, SDK 객체, Entra token은 절대 노출하지 않는다.

## 2. Sandboxes Gateway 실행

두 terminal을 사용한다.

**Terminal A - Gateway:**

```bash
export RESOURCE_GROUP="rg-ai-workspace-sandbox-lab"
export LOCATION="koreacentral"
export SANDBOX_GROUP_NAME="ai-workspace-sandboxes"
export EXECUTION_BACKEND="sandboxes"
export SANDBOXES_PYTHON="${SANDBOXES_PYTHON:-.work/sandboxes-venv/bin/python}"
```

Azure OpenAI 배포 없이 전체 흐름을 빠르게 재현하려면 deterministic stub을
선택한다.

```bash
export LLM_PROVIDER="stub"
```

실제 Azure OpenAI를 사용하려면 위 stub 설정 대신 다음 값을 설정한다.

```bash
export LLM_PROVIDER="azure-openai"
export AZURE_OPENAI_ENDPOINT="https://<your-endpoint>.openai.azure.com/"
export AZURE_OPENAI_DEPLOYMENT="<your-deployment>"
export REASONING_EFFORT="medium"
```

선택한 설정을 마친 뒤 Gateway를 실행한다.

```bash
"$SANDBOXES_PYTHON" -m python_gateway.server
```

Gateway는 기본적으로 `http://127.0.0.1:8089`에서 실행된다. Ready 상태의
최신 `python-code-interpreter-*` disk image를 자동 선택하고, 요청마다
Sandbox를 생성한 뒤 실행이 끝나면 즉시 삭제한다.

**Terminal B - 사용자 API:** 새 terminal을 열고 repository root로 이동한 뒤 설정한다.

```bash
export SANDBOX_USER_API="http://127.0.0.1:8089"
export DEMO_USER="user-demo"
export SANDBOXES_PYTHON="${SANDBOXES_PYTHON:-.work/sandboxes-venv/bin/python}"

curl --fail-with-body --silent --show-error \
  "$SANDBOX_USER_API/health" | jq
```

health 응답에 `"backend": "sandboxes"`가 표시돼야 한다.

## 3. 첨부파일 준비

```bash
mkdir -p .work/sandboxes-user

cat > .work/sandboxes-user/sales.csv <<'CSV'
month,product,amount
2026-01,Notebook,120
2026-01,Pen,80
2026-02,Notebook,150
2026-02,Pen,90
2026-03,Notebook,130
2026-03,Pen,110
CSV
```

## 4. 자연어 요청

`LLM_PROVIDER=stub`이면 결정론적 계획과 코드를 사용하고,
`LLM_PROVIDER=azure-openai`이면 실제 모델이 계획과 코드를 생성한다. 나머지
Sandbox 실행·artifact·승인 흐름은 동일하다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$SANDBOX_USER_API/api/analysis-jobs" \
  --header "X-Demo-User: $DEMO_USER" \
  --form "request=첨부한 매출 CSV를 월별 합계로 집계하고 차트 PNG와 요약 JSON을 만들어줘" \
  --form "file=@.work/sandboxes-user/sales.csv;filename=sales.csv" \
  --form "expected=monthly_sales.png" \
  --form "expected=summary.json" \
  --output .work/sandboxes-user/create.json

jq '{
  id,
  status,
  classification,
  route,
  succeeded,
  attempts,
  plan,
  artifacts: [.artifacts[].name],
  promotions
}' .work/sandboxes-user/create.json

export ANALYSIS_JOB_ID=$(jq -r '.id' .work/sandboxes-user/create.json)
```

통과 기준:

- `status`는 `completed`
- `classification`은 `A`, 정책 `route`는 `python-pool`
- `/health`의 `backend`는 `sandboxes`
- `succeeded`는 `true`
- `plan`에 작업 계획이 있음. `azure-openai` 경로에서는 실제 LLM이 생성함
- `monthly_sales.png`, `summary.json`이 staging에 있음
- `promotions[].promoted`는 `false`
- 응답에 Sandbox ID, SDK 객체, Entra token이 없음

> Dynamic Sessions(실습 1B)와 동일한 API 형식을 사용한다.  
> 내부적으로는 `POST /executions` curl 대신 `sandbox.exec()`을 호출하고, 파일 I/O는 SDK `write_file()` / `read_file()`을 사용한다.

상태를 다시 조회한다.

```bash
curl --fail-with-body --silent --show-error \
  "$SANDBOX_USER_API/api/analysis-jobs/$ANALYSIS_JOB_ID" \
  --header "X-Demo-User: $DEMO_USER" | jq
```

결과 파일을 다운로드한다.

```bash
curl --fail-with-body --silent --show-error \
  "$SANDBOX_USER_API/api/analysis-jobs/$ANALYSIS_JOB_ID/files/summary.json" \
  --header "X-Demo-User: $DEMO_USER" \
  --output .work/sandboxes-user/summary.json

jq . .work/sandboxes-user/summary.json
```

## 5. 결과 검토와 승인

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$SANDBOX_USER_API/api/analysis-jobs/$ANALYSIS_JOB_ID/approve" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{}' \
  --output .work/sandboxes-user/approved.json

jq '.status, .promotions' .work/sandboxes-user/approved.json
```

승인은 Sandbox를 다시 실행하지 않는다. 최초 실행에서 staging한 artifact의 hash를 재확인하고 같은 파일만 승격한다.

## 6. Sandbox lifecycle과 비용 확인

Python orchestrator는 artifact를 local staging으로 옮긴 뒤 `finally`에서
Sandbox를 삭제한다. 사용자 job은 Sandbox ID가 아니라 staging artifact를
참조하므로 승인·다운로드 시 Sandbox를 다시 실행하지 않는다. 프로세스 장애로
명시적 삭제가 실패해도 30분 idle suspend 후 1시간 server-side auto-delete가
비용 누수를 제한한다. Gateway 시작 시 `component=python-gateway` label의
이전 프로세스 Sandbox 중 1시간 이상 지난 `Stopped`·`Suspended`·`Failed` orphan만
정리하며 다른 running instance의 작업은 건드리지 않는다. 같은 검사를
Gateway 실행 중에도 5분마다 반복한다.
동시 Python Sandbox 작업은 기본 5개로 제한하며 초과 요청은 Sandbox를
할당하기 전에 HTTP 429로 거부한다. 결과 artifact는 파일당 64MB를 넘으면
SDK 다운로드 전에 거부한다.

ACA Sandboxes 포털에서 요청 완료 후 active Sandbox가 0개인지 확인한다.

## 7. 오류 복구와 정책 거부

재시도와 정책 분기는 deterministic stub으로 검증한다.

```bash
EXECUTION_BACKEND=sandboxes LLM_PROVIDER=stub bash scripts/agent-lab.sh
```

이 스크립트는 다음을 확인한다.

- 잘못 생성된 코드가 1회 실패한 뒤 수정·재실행됨
- 재시도 한도를 넘으면 오류 요약을 반환하고 중단함
- 인터넷 접근 요청은 통제 egress 경로로 분류됨
- 운영 시스템 직접 변경 요청은 Sandbox를 만들기 전에 거부됨
- Office 요청은 실습 2 경로로 분류됨

## 8. 사용자에게 보여야 하는 정보

| 표시 | 숨김 |
| --- | --- |
| 작업 계획, 성공 여부, 시도 횟수 | Sandbox ID |
| 결과 파일 이름·크기·hash | Entra token과 내부 SDK 객체 |
| 정책 거부 이유와 안전한 오류 요약 | 생성 코드의 내부 경로와 credential |
| 승인·승격 상태 | 다른 tenant의 데이터 |
| backend 종류(`sandboxes`) | SandboxGroup 이름과 Resource Group |

## 9. Sandboxes vs Dynamic Sessions - 사용자 관점 비교

Dynamic Sessions pool을 별도로 배포한 환경에서만 동일 요청을 두 backend로
비교한다. 현재 실습 Resource Group에서는 비용 절감을 위해 Dynamic Sessions
리소스를 삭제했으므로 Sandboxes 경로만 실행한다.

이 비교는 현재 8089 Gateway와 충돌하지 않도록 별도 포트를 사용한다.
Dynamic Sessions pool이 실제로 준비된 경우에만 명시적으로 활성화한다.

```bash
export RUN_DS_COMPARISON="yes"

if [[ "$RUN_DS_COMPARISON" == "yes" ]]; then
# Dynamic Sessions backend (실습 1B)
export EXECUTION_BACKEND="dynamic-sessions"
export PYTHON_POOL_NAME="ai-workspace-python-sbx"
export LLM_PROVIDER="stub"
PYTHON_GATEWAY_PORT=8091 \
  "$SANDBOXES_PYTHON" -m python_gateway.server &
DS_PID=$!
sleep 3

curl --silent --show-error \
  --request POST "http://127.0.0.1:8091/api/analysis-jobs" \
  --header "X-Demo-User: $DEMO_USER" \
  --form "request=월별 합계를 집계하고 summary.json을 만들어줘" \
  --form "file=@.work/sandboxes-user/sales.csv;filename=sales.csv" \
  --form "expected=summary.json" \
  --output .work/sandboxes-user/ds-result.json
kill $DS_PID
wait $DS_PID 2>/dev/null || true

# Sandboxes backend (실습 3B)
export EXECUTION_BACKEND="sandboxes"
export SANDBOX_GROUP_NAME="ai-workspace-sandboxes"
PYTHON_GATEWAY_PORT=8092 \
  "$SANDBOXES_PYTHON" -m python_gateway.server &
SB_PID=$!
sleep 3

curl --silent --show-error \
  --request POST "http://127.0.0.1:8092/api/analysis-jobs" \
  --header "X-Demo-User: $DEMO_USER" \
  --form "request=월별 합계를 집계하고 summary.json을 만들어줘" \
  --form "file=@.work/sandboxes-user/sales.csv;filename=sales.csv" \
  --form "expected=summary.json" \
  --output .work/sandboxes-user/sb-result.json
kill $SB_PID
wait $SB_PID 2>/dev/null || true

# 결과 비교 (분석 내용은 동일, backend만 다름)
jq -r '.artifacts[].name' .work/sandboxes-user/ds-result.json
jq -r '.artifacts[].name' .work/sandboxes-user/sb-result.json
fi
```

## 10. 정리

실행 Sandbox는 orchestrator가 이미 삭제했다. job 삭제는 gateway의 메모리
mapping과 staging 파일을 제거한다.

```bash
curl --fail-with-body --silent --show-error \
  --request DELETE \
  "$SANDBOX_USER_API/api/analysis-jobs/$ANALYSIS_JOB_ID" \
  --header "X-Demo-User: $DEMO_USER"
```

DELETE 응답을 확인한 뒤 Gateway를 실행한 terminal에서 `Ctrl+C`로 종료한다.

SandboxGroup과 Sandbox의 완전 정리는 관리자가 [실습 3A §19절](03A_ACA_Sandboxes_Admin_Lab.md#19-정리)에서 수행한다.
