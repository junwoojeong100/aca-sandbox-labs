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

이 repository에서는 `agent.cli`가 AI Workspace API/UI를 대신한다. 명령은 실습 운영자가 실행하지만 `--request`, `--attach`, `--approve`만 사용자 행동에 해당한다.

## 2. 첨부파일 준비

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

## 3. 실제 LLM에 자연어 요청

관리자가 구성한 backend 환경에서 실행한다.

```bash
export STAGING_DIR="$PWD/.work/python-user/staging"
export APPROVED_DIR="$PWD/.work/python-user/approved"

python3 -m agent.cli \
  --request "첨부한 매출 CSV를 월별 합계로 집계하고 차트 PNG와 요약 JSON을 만들어줘" \
  --attach .work/python-user/sales.csv \
  --expect monthly_sales.png \
  --expect summary.json \
  --audit-dir .work/python-user/audit \
  > .work/python-user/result.json

jq '{
  classification,
  route,
  succeeded,
  attempts,
  plan,
  artifacts: [.artifacts[].name],
  promotions
}' .work/python-user/result.json
```

통과 기준:

- `classification`은 `A`, `route`는 `python-pool`
- `succeeded`는 `true`
- `plan`에 실제 LLM이 만든 작업 계획이 있음
- `monthly_sales.png`, `summary.json`이 staging에 있음
- 승인하지 않았으므로 `promotions[].promoted`는 `false`
- 사용자 응답에는 token, endpoint, session identifier가 없음

## 4. 결과 검토와 승인

검사 결과와 미리보기를 확인한 뒤 동일 요청을 명시적으로 승인한다.

```bash
python3 -m agent.cli \
  --request "첨부한 매출 CSV를 월별 합계로 집계하고 차트 PNG와 요약 JSON을 만들어줘" \
  --attach .work/python-user/sales.csv \
  --expect monthly_sales.png \
  --expect summary.json \
  --approve \
  --approver "user-demo" \
  --audit-dir .work/python-user/audit \
  > .work/python-user/approved-result.json

jq '.promotions' .work/python-user/approved-result.json
test -s .work/python-user/approved/monthly_sales.png
test -s .work/python-user/approved/summary.json
```

승인은 Sandbox 안의 코드를 다시 실행할 권한이 아니라, 검사된 artifact hash를 최종 위치로 승격할 권한이다.

## 5. 오류 복구와 정책 거부

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

## 6. 사용자에게 보여야 하는 정보

| 표시 | 숨김 |
| --- | --- |
| 작업 계획, 성공 여부, 시도 횟수 | session identifier |
| 결과 파일 이름·크기·hash | Entra token과 내부 endpoint |
| 정책 거부 이유와 안전한 오류 요약 | 생성 코드의 내부 경로와 credential |
| 승인·승격 상태 | 다른 tenant의 데이터 |

## 7. 정리

orchestrator는 성공·실패와 관계없이 실행 session을 삭제한다. 사용자 실습의 로컬 파일만 삭제하려면 다음을 실행한다.

```bash
rm -rf .work/python-user
```

Azure pool과 모델 배포의 정리는 관리자가 [실습 1A](01A_Python_Code_Interpreter_Admin_Lab.md)에서 수행한다.
