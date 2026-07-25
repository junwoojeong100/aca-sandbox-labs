# 실습 1B: 자연어 Python 분석 - 사용자

## 목표

사용자가 Python 코드나 Azure resource를 직접 다루지 않고 다음 흐름을 경험한다.

```text
자연어 요청 + 첨부파일
  -> 정책 분류
  -> 실제 LLM의 계획과 Python 코드 생성
  -> 격리된 Python session 실행
  -> 실패 시 제한된 오류로 코드 수정·재실행
  -> 결과 파일 검사와 staging
  -> 사용자 승인 후에만 승격
```

예상 시간은 20~30분이다.

## 1. 사전 조건

관리자가 [실습 1A](01A_Python_Code_Interpreter_Admin_Lab.md)를 완료해 다음을 준비해야 한다.

- Python session pool과 backend의 Session Executor 역할
- `LLM_PROVIDER=azure-openai`
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`
- 추론 identity의 `Cognitive Services OpenAI User` 역할
- 사용자에게 token, pool endpoint, session identifier를 노출하지 않는 backend

이 repository에서는 `python_gateway/`가 AI Workspace backend를 대리한다. Gateway만 LLM 설정, pool endpoint, Entra token과 session identifier를 관리하며 사용자 API에는 public analysis job ID만 반환한다.

사용자는 session terminal이나 desktop을 보지 않는다. 사용자 화면에는 자연어 요청, 진행 상태, LLM 작업 계획, 안전한 오류 요약, 결과 파일·미리보기와 승인 상태만 표시한다.

## 2. 사용자 Gateway 실행

다음 명령은 관리자 또는 실습 운영자가 repository root에서 실행한다. 실습 1A에서 설정한 실제 LLM 환경 변수를 사용한다.

```bash
export RESOURCE_GROUP="rg-ai-workspace-sandbox-lab"
export PYTHON_POOL_NAME="ai-workspace-python-sbx"
export LLM_PROVIDER="azure-openai"
export AZURE_OPENAI_ENDPOINT="https://<ACCOUNT>.openai.azure.com"
export AZURE_OPENAI_DEPLOYMENT="<DEPLOYMENT_NAME>"
export REASONING_EFFORT="medium"

python3 -m python_gateway.server
```

Gateway는 기본적으로 `http://127.0.0.1:8089`에서 실행된다. 다른 terminal에서 사용자 API 변수를 설정한다.

```bash
export PYTHON_USER_API="http://127.0.0.1:8089"
export DEMO_USER="user-demo"

curl --fail-with-body --silent --show-error \
  "$PYTHON_USER_API/health" | jq
```

> `X-Demo-User`는 localhost 실습에서 ownership 분리를 보여주기 위한 값일 뿐 인증 수단이 아니다. Production에서는 Entra access token을 검증하고 token의 tenant·object ID로 authorization한다.

## 3. 첨부파일 준비

```bash
mkdir -p .work/python-user

cat > .work/python-user/sales.csv <<'CSV'
month,product,amount
2026-01,Notebook,120
2026-01,Pen,80
2026-02,Notebook,150
2026-02,Pen,90
2026-03,Notebook,130
2026-03,Pen,110
CSV
```

## 4. 실제 LLM에 자연어 요청

자연어, 첨부파일과 필수 결과 파일 이름을 multipart request로 전달한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_USER_API/api/analysis-jobs" \
  --header "X-Demo-User: $DEMO_USER" \
  --form "request=첨부한 매출 CSV를 월별 합계로 집계하고 차트 PNG와 요약 JSON을 만들어줘" \
  --form "file=@.work/python-user/sales.csv;filename=sales.csv" \
  --form "expected=monthly_sales.png" \
  --form "expected=summary.json" \
  --output .work/python-user/create.json

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
}' .work/python-user/create.json

export ANALYSIS_JOB_ID=$(jq -r '.id' .work/python-user/create.json)
```

통과 기준:

- `status`는 `completed`
- `classification`은 `A`, `route`는 `python-pool`
- `succeeded`는 `true`
- `plan`에 실제 LLM이 만든 작업 계획이 있음
- `monthly_sales.png`, `summary.json`이 staging에 있음
- 승인하지 않았으므로 `promotions[].promoted`는 `false`
- 사용자 응답에는 token, endpoint, session identifier가 없음

상태를 다시 조회한다.

```bash
curl --fail-with-body --silent --show-error \
  "$PYTHON_USER_API/api/analysis-jobs/$ANALYSIS_JOB_ID" \
  --header "X-Demo-User: $DEMO_USER" | jq
```

결과 파일을 다운로드한다.

```bash
curl --fail-with-body --silent --show-error \
  "$PYTHON_USER_API/api/analysis-jobs/$ANALYSIS_JOB_ID/files/summary.json" \
  --header "X-Demo-User: $DEMO_USER" \
  --output .work/python-user/summary.json

jq . .work/python-user/summary.json
```

## 5. 결과 검토와 승인

검사 결과와 미리보기를 확인한 뒤 동일 analysis job을 승인한다.

```bash
curl --fail-with-body --silent --show-error \
  --request POST \
  "$PYTHON_USER_API/api/analysis-jobs/$ANALYSIS_JOB_ID/approve" \
  --header "X-Demo-User: $DEMO_USER" \
  --header "Content-Type: application/json" \
  --data '{}' \
  --output .work/python-user/approved.json

jq '.status, .promotions' .work/python-user/approved.json
```

승인은 코드를 다시 실행하지 않는다. 최초 실행에서 staging한 artifact의 hash를 다시 확인하고 같은 파일만 `.work/python-user-api/approved/`로 승격한다.

## 6. 오류 복구와 정책 거부

재시도와 정책 분기는 실제 모델 품질과 무관하게 재현할 수 있도록 deterministic stub으로 검증한다.

```bash
LLM_PROVIDER=stub bash scripts/agent-lab.sh
```

이 스크립트는 다음 사용자 경험을 확인한다.

- 잘못 생성된 코드가 1회 실패한 뒤 수정·재실행됨
- 재시도 한도를 넘으면 오류 요약을 반환하고 중단함
- 인터넷 접근 요청은 통제 egress 경로로 분류됨
- 운영 시스템 직접 변경 요청은 session을 만들기 전에 거부됨
- Office 요청은 실습 2 경로로 분류됨

## 7. 사용자에게 보여야 하는 정보

| 표시 | 숨김 |
| --- | --- |
| 작업 계획, 성공 여부, 시도 횟수 | session identifier |
| 결과 파일 이름·크기·hash | Entra token과 내부 endpoint |
| 정책 거부 이유와 안전한 오류 요약 | 생성 코드의 내부 경로와 credential |
| 승인·승격 상태 | 다른 tenant의 데이터 |

## 8. 정리

orchestrator는 성공·실패와 관계없이 실행 session을 삭제한다. 사용자 job 삭제는 gateway의 메모리 mapping과 staging 파일을 제거한다.

```bash
curl --fail-with-body --silent --show-error \
  --request DELETE \
  "$PYTHON_USER_API/api/analysis-jobs/$ANALYSIS_JOB_ID" \
  --header "X-Demo-User: $DEMO_USER"
```

Azure pool과 모델 배포의 정리는 관리자가 [실습 1A](01A_Python_Code_Interpreter_Admin_Lab.md)에서 수행한다.
