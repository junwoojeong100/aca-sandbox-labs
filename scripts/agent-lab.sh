#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
cd "$REPO_ROOT"

require_cmd az
require_cmd jq
require_cmd python3

WORK_DIR="$WORK_ROOT/agent"
mkdir -p "$WORK_DIR"

export STAGING_DIR="$WORK_DIR/staging"
export APPROVED_DIR="$WORK_DIR/approved"
AUDIT_DIR="$WORK_DIR/audit"

rm -rf "$STAGING_DIR" "$APPROVED_DIR" "$AUDIT_DIR"

cat > "$WORK_DIR/sales.csv" <<'CSV'
month,product,amount
2026-01,Notebook,120
2026-01,Pen,80
2026-02,Notebook,150
2026-02,Pen,90
2026-03,Notebook,130
2026-03,Pen,110
CSV

run_agent() {
  python3 -m agent.cli --audit-dir "$AUDIT_DIR" "$@"
}

log "1/6 offline 테스트"
python3 -m unittest discover -s tests >/dev/null

log "2/6 정책 거부 경로"
for spec in \
  "E:deny:production database 의 사용자 테이블을 지워줘" \
  "D:controlled-egress:https://example.com 에서 데이터를 받아줘" \
  "B:office-pool:결과를 pptx 보고서로 만들어줘"; do
  expected_class=${spec%%:*}
  remainder=${spec#*:}
  expected_route=${remainder%%:*}
  request=${remainder#*:}
  output="$WORK_DIR/policy-$expected_class.json"
  run_agent --request "$request" > "$output" || true
  jq -e --arg c "$expected_class" --arg r "$expected_route" \
    '.classification == $c and .route == $r' "$output" >/dev/null \
    || die "Policy routing mismatch for class $expected_class: $(cat "$output")"
done

output="$WORK_DIR/policy-C.json"
run_agent --request "전체 로그를 재처리해줘" --estimated-seconds 900 > "$output" || true
jq -e '.classification == "C" and .allowed == false' "$output" >/dev/null \
  || die "Long running request should be classified as C"

log "3/6 정상 경로. 승인 없이 실행하므로 승격되지 않아야 한다"
output="$WORK_DIR/run-unapproved.json"
run_agent \
  --request "첨부한 매출 CSV를 월별로 집계하고 차트를 만들어줘" \
  --attach "$WORK_DIR/sales.csv" \
  --expect monthly_sales.png \
  --expect summary.json > "$output"
jq -e '
  .classification == "A"
  and .route == "python-pool"
  and .succeeded == true
  and .attempts == 1
  and (.artifacts | length) == 2
  and (.artifacts | all(.sha256 | length == 64))
  and (.promotions | all(.promoted == false))
' "$output" >/dev/null || die "Unapproved run assertions failed: $(cat "$output")"
[[ ! -d "$APPROVED_DIR" ]] || [[ -z "$(ls -A "$APPROVED_DIR")" ]] \
  || die "Artifacts were promoted without approval"

log "4/6 승인 후 승격과 hash 재검증"
output="$WORK_DIR/run-approved.json"
run_agent \
  --request "첨부한 매출 CSV를 월별로 집계하고 차트를 만들어줘" \
  --attach "$WORK_DIR/sales.csv" \
  --expect monthly_sales.png \
  --expect summary.json \
  --approve --approver "lab-operator" > "$output"
jq -e '
  .succeeded == true
  and (.promotions | length) == 2
  and (.promotions | all(.promoted == true))
' "$output" >/dev/null || die "Approved run assertions failed: $(cat "$output")"
APPROVED_RUN_DIR=$(find "$APPROVED_DIR" -mindepth 1 -maxdepth 1 -type d | head -1)
test -n "$APPROVED_RUN_DIR"
test -s "$APPROVED_RUN_DIR/summary.json"
test -s "$APPROVED_RUN_DIR/monthly_sales.png"
python3 - "$APPROVED_RUN_DIR/monthly_sales.png" <<'PY'
import sys

with open(sys.argv[1], "rb") as source:
    assert source.read(8) == b"\x89PNG\r\n\x1a\n", "promoted file is not a PNG"
PY
jq -e '.monthly_sales["2026-01"] == 200' "$APPROVED_RUN_DIR/summary.json" >/dev/null

log "5/6 오류 -> 코드 수정 -> 재실행 루프"
output="$WORK_DIR/run-retry.json"
run_agent \
  --request "매출 CSV를 집계해줘 (오류 복구 시나리오)" \
  --attach "$WORK_DIR/sales.csv" \
  --expect summary.json > "$output"
jq -e '.succeeded == true and .attempts == 2' "$output" >/dev/null \
  || die "Retry loop assertions failed: $(cat "$output")"

RETRY_AUDIT=$(ls -t "$AUDIT_DIR"/*.json | head -1)
jq -e '
  ([.steps[] | select(.step == "execution") | .detail.status]
    | index("Failed") != null and index("Succeeded") != null)
  and ([.steps[] | select(.step == "session-deleted")] | length == 1)
' "$RETRY_AUDIT" >/dev/null || die "Audit log did not record the retry loop"

log "6/6 identifier 비노출과 재시도 한도"
IDENTIFIER=$(jq -r '.sessionIdentifier' "$RETRY_AUDIT")
[[ -n "$IDENTIFIER" && "$IDENTIFIER" != "null" ]] \
  || die "Audit log must record the session identifier"
if grep -rq "$IDENTIFIER" "$WORK_DIR"/run-*.json "$output"; then
  die "Session identifier leaked into a user-facing response"
fi

output="$WORK_DIR/run-retry-limit.json"
MAX_CODE_RETRIES=0 run_agent \
  --request "매출 CSV를 집계해줘 (오류 복구 시나리오)" \
  --attach "$WORK_DIR/sales.csv" \
  --expect summary.json > "$output" || true
jq -e '.succeeded == false and .attempts == 1' "$output" >/dev/null \
  || die "Retry limit was not enforced: $(cat "$output")"

cat > "$WORK_DIR/validation.txt" <<EOF
llm_provider=${LLM_PROVIDER:-stub}
policy_routing=verified
unapproved_promotion=blocked
approved_promotion=verified
retry_loop=verified
retry_limit=verified
identifier_exposure=none
session_cleanup=verified
EOF

log "Agent orchestration validation passed."
log "Artifacts: $WORK_DIR"
