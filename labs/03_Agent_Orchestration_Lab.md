# 실습 3: LLM Agent 오케스트레이션

## 목표

앞의 두 실습은 Sandbox 자체를 검증했다. 이 실습은 그 위에서 **AI가 요청을 받아 작업하고, 만족하는 결과물만 실제 작업에 반영하는** 흐름 전체를 검증한다.

```text
사용자 자연어 요청
  -> 정책 엔진 (A~E 분류)
  -> LLM 계획·코드 생성
  -> 생성 코드 정책 검사
  -> 격리 session 실행
  -> 실패 시 오류 전달 -> 코드 수정 -> 재실행 (최대 2회)
  -> 산출물 회수와 검사 (magic bytes, macro, hash)
  -> Artifact Staging
  -> 사용자 승인
  -> 승인 후에만 승격
  -> session 삭제
```

검증 항목:

- 자연어 요청이 결정론적 정책으로 분류되는지
- LLM이 만든 코드가 실행 **전에** 검사되는지
- 실행 오류가 LLM에 되돌아가 자동 수정·재실행되는지
- 재시도 한도가 지켜지는지
- session identifier가 사용자 응답에 노출되지 않는지
- 승인 없이는 산출물이 승격되지 않는지
- 작업이 끝나면 session이 삭제되는지

예상 시간은 30~45분이다.

## 1. 사전 조건

- [실습 1](01_Python_Code_Interpreter_Lab.md)로 Python pool이 생성돼 있고 Session Executor 역할이 부여된 상태
- Python 3.10 이상 (표준 라이브러리만 사용한다. 추가 package 설치가 필요 없다)
- Azure CLI 로그인
- repository root에서 명령 실행

실습 2의 Office pool은 필요 없다. 이 실습에서 `B` 분류는 Office 경로로 라우팅해야 한다는 정책 결정만 검증한다.

LLM은 두 가지 모드를 지원한다.

| 모드 | 설정 | 용도 |
| --- | --- | --- |
| `stub` (기본값) | 없음 | Azure OpenAI 배포 없이 전체 흐름과 안전장치를 검증한다 |
| `azure-openai` | endpoint와 deployment | 실제 모델로 코드를 생성한다 |

`stub`은 실제 모델이 아니라 결정론적 planner다. 오케스트레이션과 통제 장치를 CI에서도 재현하기 위한 것이며, 모델 품질을 대체하지 않는다.

### 권장 Fast Path

```bash
bash scripts/agent-lab.sh
```

이 스크립트는 정상 요청, 오류 복구, 정책 거부, 승인 게이트를 순서대로 실행하고 결과를 `.work/agent/`에 저장한다.
기본 Resource Group과 Python pool 이름은 실습 1의 기본값과 같다. 실습 1에서 이름을 재정의했다면 동일한 `RESOURCE_GROUP`과 `PYTHON_POOL_NAME`을 설정한 뒤 실행한다.

## 2. 구성 요소

| 파일 | 역할 | 대응하는 아키텍처 절 |
| --- | --- | --- |
| `agent/policy.py` | 요청 분류 A~E와 생성 코드 검사 | 4.2 정책 엔진 |
| `agent/broker.py` | token, identifier 생성, 실행·업로드·다운로드·삭제 | 4.3 Session Broker |
| `agent/llm.py` | Azure OpenAI 호출과 stub planner | 4.1 AI Workspace Agent |
| `agent/staging.py` | 형식 검사, hash, 승인 게이트 | 4.6 Artifact Staging과 Approval Service |
| `agent/orchestrator.py` | 전체 흐름과 재시도 루프 | 9 실행, 오류 수정과 재시도 |
| `agent/cli.py` | 실습용 진입점 | - |

`agent/`는 구조와 trust boundary를 설명하기 위한 reference implementation이다. Production에서는 인증, tenant 저장소, malware/DLP 검사, 승인 UI, connector를 실제 서비스로 대체한다.

## 3. 정상 경로 실행

샘플 데이터를 만든다.

```bash
mkdir -p .work/agent

cat > .work/agent/sales.csv <<'CSV'
month,product,amount
2026-01,Notebook,120
2026-01,Pen,80
2026-02,Notebook,150
2026-02,Pen,90
2026-03,Notebook,130
2026-03,Pen,110
CSV
```

자연어 요청을 실행한다.

```bash
python3 -m agent.cli \
  --request "첨부한 매출 CSV를 월별로 집계하고 차트를 만들어줘" \
  --attach .work/agent/sales.csv \
  --expect monthly_sales.png \
  --expect summary.json
```

응답 예시다.

```json
{
  "requestId": "req-b6f5ec8a01a977fa",
  "classification": "A",
  "route": "python-pool",
  "allowed": true,
  "succeeded": true,
  "attempts": 1,
  "plan": "sales.csv를 월별로 집계하고 차트와 요약 JSON을 만든다.",
  "stdout": "generated monthly_sales.png and summary.json\n",
  "artifacts": [
    { "name": "monthly_sales.png", "size": 22040, "sha256": "77426a1c..." },
    { "name": "summary.json", "size": 198, "sha256": "152d1212..." }
  ],
  "promotions": [
    { "name": "monthly_sales.png", "promoted": false, "reason": "승인되지 않음" },
    { "name": "summary.json", "promoted": false, "reason": "승인되지 않음" }
  ]
}
```

통과 기준:

- `classification: A`, `route: python-pool`
- `succeeded: true`
- artifact마다 `sha256`이 있다
- **`--approve`를 주지 않았으므로 `promoted: false`다**
- 응답 어디에도 session identifier가 없다

산출물은 staging에만 있다.

```bash
find .work/agent/staging -type f
test ! -d .work/agent/approved || find .work/agent/approved -type f
```

## 4. 승인 후 승격

사용자가 결과를 확인하고 만족했을 때만 승격한다.

```bash
python3 -m agent.cli \
  --request "첨부한 매출 CSV를 월별로 집계하고 차트를 만들어줘" \
  --attach .work/agent/sales.csv \
  --expect monthly_sales.png \
  --expect summary.json \
  --approve --approver "$(whoami)"

find .work/agent/approved -type f
```

`promotions[].promoted`가 `true`가 되고 `approved/`에 파일이 나타난다.

Approval Service는 승격 직전에 staging 파일의 SHA-256을 **다시 계산해** 등록 시점 hash와 비교한다. 다르면 승격을 중단한다. staging 이후 변조를 잡기 위한 통제다.

## 5. 오류 복구 루프

요청에 `오류`가 들어가면 stub planner가 1회차에 존재하지 않는 열을 참조하는 코드를 만든다. 실제 LLM에서도 같은 흐름으로 동작한다.

```bash
python3 -m agent.cli \
  --request "매출 CSV를 집계해줘 (오류 복구 시나리오)" \
  --attach .work/agent/sales.csv \
  --expect summary.json
```

`attempts`가 `2`이고 `succeeded`가 `true`다. 감사 로그에서 각 시도를 확인한다.

```bash
AUDIT=$(ls -t .work/agent/audit/*.json | head -1)

jq -c '.steps[]
  | select(.step == "code-generated" or .step == "execution" or .step == "session-deleted")
  | {step, attempt: .detail.attempt, status: .detail.status, http: .detail.httpStatus}' \
  "$AUDIT"
```

2026-07-25 한국 중부 실제 출력이다.

```json
{"step":"code-generated","attempt":1,"status":null,"http":null}
{"step":"execution","attempt":1,"status":"Failed","http":null}
{"step":"code-generated","attempt":2,"status":null,"http":null}
{"step":"execution","attempt":2,"status":"Succeeded","http":null}
{"step":"session-deleted","attempt":null,"status":null,"http":204}
```

읽는 법:

- 1회차 실행이 `Failed`가 되고 sanitize된 `stderr`가 LLM에 전달된다
- 2회차 코드가 다시 생성되고 실행이 `Succeeded`가 된다
- 성공·실패와 무관하게 마지막에 session이 삭제된다 (HTTP 204)
- 같은 session을 재사용하므로 첨부파일을 다시 업로드하지 않는다

재시도 한도는 `MAX_CODE_RETRIES`로 조정한다. 기본값 2는 아키텍처 문서 9절과 같다.

```bash
MAX_CODE_RETRIES=0 python3 -m agent.cli \
  --request "매출 CSV를 집계해줘 (오류 복구 시나리오)" \
  --attach .work/agent/sales.csv \
  --expect summary.json
```

한도가 0이면 1회 실행 후 중단되고 종료 코드가 1이 된다.

## 6. 정책 거부 경로

정책 엔진은 LLM을 호출하기 **전에** 판단한다. 거부된 요청은 session을 아예 할당하지 않으므로 비용도 발생하지 않는다.

```bash
# E: 운영 시스템 직접 변경
python3 -m agent.cli --request "production database 의 사용자 테이블을 지워줘" || true

# D: 인터넷 접근 필요
python3 -m agent.cli --request "https://example.com 에서 데이터를 받아서 정리해줘" || true

# C: 실행 시간 초과 예상
python3 -m agent.cli --request "전체 로그를 재처리해줘" --estimated-seconds 900 || true

# B: Office 경로로 분기
python3 -m agent.cli --request "결과를 pptx 보고서로 만들어줘" || true
```

기대 결과다.

| 요청 | classification | route | allowed |
| --- | --- | --- | --- |
| production database 변경 | `E` | `deny` | `false` |
| 외부 URL 수집 | `D` | `controlled-egress` | `false` |
| 900초 예상 작업 | `C` | `async-compute` | `false` |
| pptx 보고서 | `B` | `office-pool` | `true` |
| 일반 CSV 분석 | `A` | `python-pool` | `true` |

`B` 경로는 이 CLI가 실행하지 않고 Office pool로 라우팅해야 한다는 사실만 알린다. Office 경로는 [실습 2](02_Office_Custom_Container_Lab.md)에서 다룬다.

## 7. 생성 코드 검사

LLM이 shell 실행이나 외부 통신 코드를 만들면 실행 전에 걸러진다.

```bash
python3 - <<'PY'
from agent import policy

print(policy.inspect_code("import subprocess\nsubprocess.run(['id'])"))
print(policy.inspect_code("import requests\nrequests.get('https://example.com')"))
print(policy.inspect_code("import csv\nprint('ok')"))
PY
```

출력:

```text
['subprocess 호출']
['외부 HTTP client']
[]
```

> **중요한 한계.** 이 pattern 검사는 우회할 수 있다. Code Interpreter session 안에서는 `subprocess`로 임의 명령을 실행하는 것이 원래 가능하다.
> 따라서 이 검사는 "허용 명령어 제한"의 **주 방어선이 아니라 조기 필터**다. 실제 방어선은 다음이다.
>
> - session마다 Hyper-V 격리
> - `EgressDisabled`로 외부 통신 차단
> - 실행 시간과 메모리 한도
> - production credential과 connector 권한을 session에 주입하지 않음
> - 결과물이 승인 없이는 업무 시스템에 도달하지 못함
>
> 즉 "sandbox 안에서 무엇을 못 하게 하느냐"가 아니라 **"sandbox가 밖으로 무엇을 할 수 있느냐"** 로 통제한다. 자세한 내용은 [권장 아키텍처 5절](../docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md#5-trust-boundary와-위협-모델)을 참고한다.

## 8. Identifier 비노출 확인

session identifier는 감사 로그에만 남고 사용자 응답에는 없어야 한다.

먼저 실행 요청을 하나 만든다. 사용자 응답과 감사 로그를 분리해서 저장한다.

```bash
python3 -m agent.cli \
  --request "첨부한 매출 CSV를 월별로 집계해줘" \
  --attach .work/agent/sales.csv \
  --expect summary.json \
  > .work/agent/user-response.json
```

> `--audit-dir` 기본값은 `.work/agent/audit`이고, 감사 로그 경로는 **stderr**로 출력된다. 위 명령은 stdout만 파일로 보내므로 사용자 응답만 저장된다.

정책이 거부한 요청은 session을 할당하지 않아 `sessionIdentifier`가 빈 문자열이다. 따라서 **가장 최근 로그가 아니라 identifier가 실제로 있는 로그**를 골라야 한다.

```bash
AUDIT=""
for FILE in $(ls -t .work/agent/audit/*.json); do
  if [ -n "$(jq -r '.sessionIdentifier // empty' "$FILE")" ]; then
    AUDIT="$FILE"
    break
  fi
done

echo "감사 로그: $AUDIT"

IDENTIFIER=$(jq -r '.sessionIdentifier // empty' "$AUDIT")
test -n "$IDENTIFIER" \
  || { echo "identifier가 있는 감사 로그를 찾지 못했다"; false; }

echo "감사 로그의 identifier: $IDENTIFIER"
```

> ⚠️ `IDENTIFIER`가 비어 있는 상태로 다음 `grep`을 실행하면 빈 문자열이 모든 줄에 매치돼 **거짓 `LEAKED`** 가 나온다. 위처럼 반드시 비어 있지 않은지 먼저 확인한다.

이제 사용자 응답에 identifier가 없는지 확인한다.

```bash
if grep -q "$IDENTIFIER" .work/agent/user-response.json; then
  echo "LEAKED"
else
  echo "identifier not exposed to user"
fi
```

`identifier not exposed to user`가 나와야 한다.

감사 로그에는 있고 사용자 응답에는 없다는 것을 한 번에 확인할 수도 있다.

```bash
jq -r 'has("sessionIdentifier")' "$AUDIT"                      # true
jq -r 'has("sessionIdentifier")' .work/agent/user-response.json # false
```

identifier는 `secrets.token_hex(16)`으로 만든 128-bit 난수이며 사용자 ID나 대화 제목을 포함하지 않는다. identifier를 아는 것 자체가 session 접근 권한이므로 backend에서만 보관한다.

## 9. 실제 LLM 연결

Foundry(Azure OpenAI) 배포가 있으면 stub 대신 실제 모델을 쓴다. API key가 아니라 Entra token을 사용한다.

### 9.1 Foundry 리소스, 프로젝트, 모델 배포

```bash
export RG="rg-ai-workspace-sandbox-lab"
export LOCATION="koreacentral"
export ACCOUNT="<GLOBALLY_UNIQUE_NAME>"
export PROJECT="proj-ai-workspace-sandbox"
export MODEL="gpt-5.6-terra"

az cognitiveservices account create \
  --name "$ACCOUNT" --resource-group "$RG" \
  --kind AIServices --sku S0 --location "$LOCATION" \
  --custom-domain "$ACCOUNT" --assign-identity --yes --output none

az cognitiveservices account project create \
  --name "$ACCOUNT" --resource-group "$RG" \
  --project-name "$PROJECT" --location "$LOCATION" \
  --assign-identity --output none
```

배포 전에 남은 쿼타를 확인하고 그만큼 할당한다.

```bash
az cognitiveservices usage list --location "$LOCATION" \
  --query "[?name.value=='OpenAI.GlobalStandard.$MODEL'].{limit:limit,current:currentValue}" \
  --output table

AVAILABLE=$(az cognitiveservices usage list --location "$LOCATION" \
  --query "[?name.value=='OpenAI.GlobalStandard.$MODEL'] | [0].{a:limit,b:currentValue}" \
  --output tsv | awk '{printf "%d", $1-$2}')

echo "가용 쿼타: ${AVAILABLE}K TPM"
```

> **먼저 확인한다.** `AVAILABLE`이 `0`이면 이 구독의 해당 모델 쿼타를 이미 다 쓴 상태다. 이 실습을 한 번 수행한 뒤 다시 실행할 때 흔히 발생한다. 그대로 진행하면 다음 오류가 난다.
>
> ```text
> (InvalidCapacity) The specified capacity '0' of account deployment
> should be at least 1 and no more than 1000000.
> ```
>
> 임의로 값을 올려도 마찬가지다.
>
> ```text
> (InsufficientQuota) This operation require 10 new capacity in quota
> One Thousand Tokens Per Minute - gpt-5.6-terra - GlobalStandard,
> which is bigger than the current available capacity 0.
> ```
>
> 해결 방법은 [9.1.1절](#911-쿼타가-부족할-때)을 참고한다.

`AVAILABLE`이 1 이상이면 배포한다.

```bash
test "$AVAILABLE" -ge 1 || {
  echo "가용 쿼타가 없다. 9.1.1절을 참고한다."; false;
}

az cognitiveservices account deployment create \
  --name "$ACCOUNT" --resource-group "$RG" \
  --deployment-name "$MODEL" \
  --model-name "$MODEL" \
  --model-version 2026-07-09 \
  --model-format OpenAI \
  --sku-name GlobalStandard \
  --sku-capacity "$AVAILABLE" \
  --output none
```

`sku-capacity` 단위는 1,000 TPM이다. `250`이면 250K TPM이다.

배포 결과를 확인한다.

```bash
az cognitiveservices account deployment show \
  --name "$ACCOUNT" --resource-group "$RG" \
  --deployment-name "$MODEL" \
  --query "{deployment:name,model:properties.model.name,version:properties.model.version,sku:sku.name,capacity:sku.capacity,state:properties.provisioningState}" \
  --output json
```

통과 기준: `state`가 `Succeeded`이고 `capacity`가 요청한 값과 같다.

### 9.1.1 쿼타가 부족할 때

쿼타를 소비 중인 배포를 먼저 찾는다. 같은 구독의 **다른 리소스 그룹**에 있을 수 있다.

```bash
az cognitiveservices account list \
  --query "[?location=='$LOCATION'].{name:name,rg:resourceGroup}" --output tsv \
| while read -r NAME GROUP; do
    az cognitiveservices account deployment list \
      --name "$NAME" --resource-group "$GROUP" \
      --query "[?properties.model.name=='$MODEL'].{account:'$NAME',rg:'$GROUP',deployment:name,tpm:sku.capacity}" \
      --output tsv
  done
```

선택지는 세 가지다.

| 상황 | 조치 |
| --- | --- |
| 기존 배포가 실습용이고 불필요 | 삭제한 뒤 재배포한다 |
| 기존 배포가 필요하지만 크게 잡혀 있음 | capacity를 줄여 여유를 만든다 |
| 쿼타 자체를 늘려야 함 | 구독 쿼타 증설을 요청한다 |

```bash
# 불필요한 배포 삭제
az cognitiveservices account deployment delete \
  --name "$ACCOUNT" --resource-group "$RG" --deployment-name "<DEPLOYMENT>"

# 기존 배포 capacity 축소 (예: 250 -> 100)
az cognitiveservices account deployment create \
  --name "$ACCOUNT" --resource-group "$RG" \
  --deployment-name "<DEPLOYMENT>" \
  --model-name "$MODEL" --model-version 2026-07-09 --model-format OpenAI \
  --sku-name GlobalStandard --sku-capacity 100 --output none
```

다른 리전으로 피하는 방법은 **SKU에 따라 다르다.** 리전별 잔여량을 확인한다.

```bash
for REGION in koreacentral eastus2 swedencentral northcentralus; do
  printf '%-16s ' "$REGION"
  az cognitiveservices usage list --location "$REGION" \
    --query "[?name.value=='OpenAI.GlobalStandard.$MODEL'] | [0].{a:limit,b:currentValue}" \
    --output tsv 2>/dev/null | awk '{printf "limit=%d used=%d available=%d\n", $1, $2, $1-$2}'
done
```

2026-07-25 실측 결과다. koreacentral에 250을 배포한 직후 **모든 리전의 사용량이 동시에 올라갔다.**

```text
koreacentral     limit=1000 used=1000 available=0
eastus2          limit=1000 used=1000 available=0
swedencentral    limit=1000 used=1000 available=0
northcentralus   limit=1000 used=1000 available=0
```

> **`GlobalStandard` 쿼타는 리전별이 아니라 구독 전체가 공유하는 단일 풀이다.** 따라서 GlobalStandard로는 리전을 바꿔도 쿼타가 늘지 않는다. 이름 그대로 "Global"이다.
>
> 반면 `DataZoneStandard`, `Standard`, `ProvisionedManaged`는 **별도 쿼타 버킷**이다. 같은 시점 eastus2의 `OpenAI.DataZoneStandard.gpt-5.6-terra`는 limit 333, used 0으로 여유가 있었다. GlobalStandard가 막혔다면 다른 SKU를 검토한다.

```bash
# 이 모델의 모든 SKU 쿼타 버킷 확인
az cognitiveservices usage list --location "$LOCATION" \
  --query "[?contains(name.value,'$MODEL')].{quota:name.value,limit:limit,used:currentValue}" \
  --output table
```

> ⚠️ **가용 쿼타를 전부 할당하지 않는 것을 권장한다.** GlobalStandard가 구독 공유 풀이므로, 전량을 쓰면 **같은 구독의 다른 리전·다른 리소스 그룹에서 이 모델을 배포할 수 없게 된다.** 이 실습은 10K TPM(`--sku-capacity 10`)으로도 모든 절이 동작한다. 실제 처리량이 필요할 때만 크게 잡는다.

### 9.2 RBAC

추론 호출 identity에 Azure OpenAI 리소스 범위로 `Cognitive Services OpenAI User`를 부여한다.

```bash
ACCOUNT_ID=$(az cognitiveservices account show \
  --name "$ACCOUNT" --resource-group "$RG" --query id --output tsv)

az role assignment create \
  --role "Cognitive Services OpenAI User" \
  --assignee-object-id "$(az ad signed-in-user show --query id --output tsv)" \
  --assignee-principal-type User \
  --scope "$ACCOUNT_ID"
```

### 9.3 환경 변수와 실행

추론 endpoint는 계정 endpoint가 아니라 **`*.openai.azure.com`** 이다.

```bash
export LLM_PROVIDER=azure-openai
export AZURE_OPENAI_ENDPOINT="https://$ACCOUNT.openai.azure.com"
export AZURE_OPENAI_DEPLOYMENT="$MODEL"
export REASONING_EFFORT=medium

python3 -m agent.cli \
  --request "첨부한 매출 CSV를 월별 합계로 집계하고 차트 PNG와 요약 JSON을 만들어줘" \
  --attach .work/agent/sales.csv \
  --expect monthly_sales.png --expect summary.json
```

### 9.4 추론 모델 payload 규약

**gpt-5.x 계열 추론 모델은 기존 gpt-4o 계열과 payload 규약이 다르다.** 2026-07-25 실측 결과다.

| 파라미터 | gpt-4o 계열 | gpt-5.6-terra | 실제 오류 |
| --- | --- | --- | --- |
| 출력 길이 | `max_tokens` | **`max_completion_tokens`** | `'max_tokens' is not supported with this model` |
| `temperature` | 0~2 자유 | **기본값 1만 허용** | `'temperature' does not support 0.1` |
| `reasoning_effort` | 미지원 | **`low`·`medium`·`high` 등** | - |
| `response_format` | 지원 | 지원 | - |

`agent/llm.py`는 추론 모델 규약으로 먼저 호출하고, 서버가 `unsupported_parameter` 또는 `unsupported_value`를 돌려주면 기존 모델 규약으로 한 번만 재시도한다. 따라서 두 계열 모두에서 동작한다.

> `reasoning_effort`는 **배포 설정이 아니라 요청 파라미터**다. 같은 배포를 쓰면서 요청마다 `low`·`medium`·`high`를 바꿀 수 있다. `REASONING_EFFORT` 환경 변수로 조정하고, 빈 값이면 파라미터를 보내지 않는다.

> 추론 모델은 **추론 토큰도 출력 한도를 소비한다.** `MAX_OUTPUT_TOKENS` 기본값은 8000이다. 너무 작으면 `content`가 빈 응답이 오고 `finish_reason`이 `length`가 된다.

### 9.5 stderr 경고를 실패로 오해하지 않기

실제 모델 검증에서 발견한 중요한 함정이다.

정상 동작하는 Python 코드도 **경고를 `stderr`로 출력한다.** Code Interpreter 응답에서 `status`는 `Succeeded`인데 `result.stderr`에 4KB의 matplotlib 폰트 경고가 들어 있는 경우가 실제로 있었다.

```text
UserWarning: Could not infer format, so each element will be parsed individually,
falling back to `dateutil`.
findfont: Generic family 'sans-serif' not found ...
```

`stderr`가 비어 있는지로 성공을 판단하면 **성공한 코드를 계속 재시도하다 재시도 한도를 소진한다.** 반드시 `status`만으로 판단하고, `stderr`는 참고 정보로 다룬다.

> Python pool에는 CJK 폰트가 없다. matplotlib 차트 제목에 한글을 쓰면 폰트 경고가 나고 글자가 깨진다. 한글 라벨이 필요하면 폰트를 포함한 Custom Container를 쓰거나 라벨을 영문으로 만든다.

### 9.6 Production 전환

실제 AI Workspace 배포에서는 다음으로 바꾼다.

- Azure CLI token 대신 backend Managed Identity와 `DefaultAzureCredential`
- 사용자마다 별도 tenant·user 컨텍스트
- prompt injection 방어와 요청 크기 제한
- 모델 응답과 실행 결과를 correlation ID로 묶어 감사 저장소에 기록

LLM에는 다음을 절대 전달하지 않는다.

- session identifier, access token, pool management endpoint
- production credential과 connector 정보
- 다른 tenant의 데이터

## 10. Offline 테스트

Azure 없이 통제 장치를 검증한다. CI에서도 이 테스트가 돈다.

```bash
python3 -m unittest discover -s tests -v
```

38개 테스트가 정책 분류, 생성 코드 검사, 실행 성공 판정, 산출물 형식·macro·경로 검사, 승인 게이트, hash 변조 탐지, 재시도 한도, identifier 비노출을 확인한다.

## 11. 문제 해결

| 증상 | 조치 |
| --- | --- |
| `설정 오류: AZURE_OPENAI_ENDPOINT...` | `LLM_PROVIDER=azure-openai`인데 endpoint나 deployment가 없다 |
| `Session endpoint에 연결하지 못했다` | pool 이름과 Resource Group 확인, `RESOURCE_GROUP` 환경 변수 확인 |
| `Python 실행 실패 (HTTP 403)` | Session Executor 역할 전파를 기다린 뒤 재시도 |
| `Azure OpenAI 호출 실패 (HTTP 401)` | `Cognitive Services OpenAI User` 역할과 token audience 확인 |
| `Azure OpenAI 호출 실패 (HTTP 404)` | endpoint가 `*.openai.azure.com`인지, 배포 이름이 맞는지 확인 |
| `'max_tokens' is not supported` | 추론 모델이다. 9.4절 참고. 코드가 자동 재시도한다 |
| `'temperature' does not support 0.1` | 추론 모델이다. `temperature`를 보내지 않는다 |
| `응답 content가 비었다. finish_reason=length` | `MAX_OUTPUT_TOKENS`를 늘린다. 추론 토큰도 한도를 쓴다 |
| `LLM 응답에서 JSON을 찾지 못했다` | 모델이 JSON mode를 지원하는지 확인. system prompt 강화 |
| 성공한 코드가 계속 재시도됨 | `stderr` 경고를 실패로 판단하고 있다. 9.5절 참고 |
| 차트 한글이 깨짐 | Python pool에 CJK 폰트가 없다. 영문 라벨 또는 Custom Container 사용 |
| `허용되지 않은 확장자` | staging 허용 목록에 없는 파일이다. `agent/staging.py` 검토 |
| `hash가 staging 이후 변경됐다` | 정상 동작이다. 변조된 artifact는 승격되지 않는다 |
| `attempts`가 한도까지 갔는데 실패 | 오류 요약을 사용자에게 반환하고 중단하는 것이 설계된 동작이다 |

## 12. 정리

이 실습의 orchestrator는 새 Azure 리소스를 만들지 않는다. session은 실행마다 자동 삭제된다.

9절에서 Foundry 리소스를 만들었다면 다음이 남는다.

| 리소스 | 비용 성격 |
| --- | --- |
| AIServices 계정 (S0) | 유휴 시 과금 없음. 토큰 사용량 기준 |
| 프로젝트 | 무료 |
| GlobalStandard 모델 배포 | **토큰 사용량 기준. 유휴 시 과금 없음** |

단, 배포가 살아 있는 동안 **구독 쿼타(TPM)를 점유한다.** 다른 배포에 쿼타가 필요하면 삭제한다.

```bash
az cognitiveservices account deployment delete \
  --name "$ACCOUNT" --resource-group "$RG" --deployment-name "$MODEL"
```

로컬 산출물 정리:

```bash
rm -rf .work/agent
```

## 13. 실제 검증 기록

2026-07-25 한국 중부 리전, 기존 `ai-workspace-python-sbx` pool 대상.

### stub provider

| 항목 | 결과 |
| --- | --- |
| 자연어 요청 -> 분류 -> 실행 -> staging | 성공, 전체 16초 |
| 정책 분류 A/B/C/D/E | 기대대로 분기 |
| 오류 복구 루프 | 1회차 `Failed`, 2회차 `Succeeded` |
| Session 자동 삭제 | HTTP 204 |
| 승인 없는 승격 차단 | `promoted: false` 확인 |
| 승인 후 승격 | `approved/`에 파일 생성, hash 재검증 통과 |
| Identifier 사용자 응답 비노출 | 확인 |
| Offline 테스트 | 38개 통과 |

문서 명령을 그대로 실행한 검증이다.

| 절 | 결과 |
| --- | --- |
| §3 정상 경로 | `A`/`python-pool`/`succeeded`/`attempts:1`, staging 3파일, approved 없음 |
| §4 승인 후 승격 | `[true,true]`, `approved/`에 2파일 |
| §5 오류 복구 | `attempts: 2`, 감사 로그가 `Failed` → `Succeeded` → `session-deleted 204` |
| §5 재시도 한도 | `MAX_CODE_RETRIES=0`에서 `attempts: 1`, 종료 코드 1 |
| §6 정책 거부 | E/deny, D/controlled-egress, C/async-compute, B/office-pool 모두 일치 |
| §7 코드 검사 | `['subprocess 호출']`, `['외부 HTTP client']`, `[]` |
| §8 identifier 비노출 | `identifier not exposed to user` |
| §9 Foundry 생성·배포 | 리소스·프로젝트 `Succeeded`, 배포 capacity 250 |
| §9.1.1 쿼타 부족 처리 | 축소 명령으로 10K 회수 확인 |

> §8에서 문서 결함을 발견해 고쳤다. 이전 판은 가장 최근 감사 로그를 그대로 썼는데, §6의 정책 거부 요청은 session을 할당하지 않아 `sessionIdentifier`가 빈 문자열이다. 그 상태로 `grep -q ""`를 실행하면 모든 줄에 매치돼 **거짓 `LEAKED`** 가 나온다. 지금은 identifier가 있는 로그를 골라 쓰고 비어 있지 않은지 먼저 확인한다.

> §9.1도 고쳤다. 이 실습을 한 번 수행하면 쿼타가 소진되므로 재실행 시 `AVAILABLE=0`이 되어 `InvalidCapacity` 오류가 난다. 9.1.1절에 진단과 해결 절차를 추가했다.

### azure-openai provider (gpt-5.6-terra)

| 항목 | 결과 |
| --- | --- |
| Foundry 리소스와 프로젝트 생성 | `Succeeded` |
| `gpt-5.6-terra` 2026-07-09 GlobalStandard 배포 | `Succeeded`, capacity 250 (250K TPM, 가용 쿼타 전량) |
| Entra token 추론 호출 | HTTP 200 |
| `reasoning_effort: medium` | 정상 동작 |
| `max_tokens` 사용 | HTTP 400 `unsupported_parameter` -> 자동 재시도 로직으로 흡수 |
| `temperature: 0.1` 사용 | HTTP 400 `unsupported_value` -> 자동 재시도 로직으로 흡수 |
| 자연어 요청 -> 코드 생성 -> 실행 -> 승격 | 1회 시도 성공, 전체 17.8초 |
| 생성 결과 정확성 | 월별 합계 200/240/240, 총합 680.0 일치 |
| PNG 산출물 | magic bytes 확인, 63,848 bytes |
| 승인 후 승격과 hash 재검증 | 성공 |

> 이 검증 과정에서 실제 버그를 하나 발견해 고쳤다. matplotlib·pandas 경고가 `stderr`로 나오는 바람에, `status`가 `Succeeded`인데도 성공한 코드를 계속 재시도하다 한도를 소진했다. 9.5절과 `tests/test_agent.py`의 `ExecutionResultTests`가 이 회귀를 막는다. stub만으로는 재현되지 않는 종류의 결함이므로, 실제 모델 연결 검증을 생략하면 안 된다.
