#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

for command in docker curl jq unzip; do
  require_cmd "$command"
done

IMAGE_NAME=${IMAGE_NAME:-office-sandbox:local-smoke}
CONTAINER_NAME="office-sandbox-smoke-$$"
WORK_DIR="$WORK_ROOT/office-local"
mkdir -p "$WORK_DIR"

docker build --tag "$IMAGE_NAME" "$REPO_ROOT/office-container"
docker run --detach --rm \
  --name "$CONTAINER_NAME" \
  --publish 127.0.0.1::8080 \
  "$IMAGE_NAME" >/dev/null

HOST_PORT=$(docker port "$CONTAINER_NAME" 8080/tcp | awk -F: '{print $NF}')
BASE_URL="http://127.0.0.1:$HOST_PORT"

cleanup() {
  docker rm --force "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for attempt in $(seq 1 30); do
  if curl --fail --silent "$BASE_URL/health?identifier=must-not-appear-in-logs" \
    --output "$WORK_DIR/health.json"; then
    break
  fi
  sleep 2
done
jq -e '.status == "ok"' "$WORK_DIR/health.json" >/dev/null
jq -e '
  .limits.maxRequestBytes == 1048576
  and .limits.maxJobs == 20
  and .limits.maxStorageBytes == 268435456
' "$WORK_DIR/health.json" >/dev/null

http=$(curl --silent --output "$WORK_DIR/unsupported-media.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$BASE_URL/generate" \
  --data '{"title":"x","content":"y"}')
[[ "$http" == "415" ]] || die "Expected HTTP 415, received $http"

http=$(curl --silent --output "$WORK_DIR/invalid-json-shape.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$BASE_URL/generate" \
  --header 'Content-Type: application/json' \
  --data '[]')
[[ "$http" == "400" ]] || die "Expected HTTP 400, received $http"

curl --fail --silent --show-error \
  --request POST \
  "$BASE_URL/generate" \
  --header 'Content-Type: application/json' \
  --data '{"title":"Local smoke test","content":"Four-format document generation"}' \
  --output "$WORK_DIR/generate.json"

test "$(jq '.files | length' "$WORK_DIR/generate.json")" = "4"
for file_name in report.docx report.pdf report.pptx report.xlsx; do
  download_path=$(jq -r --arg name "$file_name" \
    '.files[] | select(.name == $name) | .downloadPath' \
    "$WORK_DIR/generate.json")
  expected_hash=$(jq -r --arg name "$file_name" \
    '.files[] | select(.name == $name) | .sha256' \
    "$WORK_DIR/generate.json")
  curl --fail --silent --show-error \
    "$BASE_URL$download_path" \
    --output "$WORK_DIR/$file_name"
  actual_hash=$(sha256_file "$WORK_DIR/$file_name")
  [[ "$actual_hash" == "$expected_hash" ]] \
    || die "Hash mismatch: $file_name"
done

unzip -t "$WORK_DIR/report.docx" >/dev/null
unzip -t "$WORK_DIR/report.pptx" >/dev/null
unzip -t "$WORK_DIR/report.xlsx" >/dev/null
head -c 4 "$WORK_DIR/report.pdf" | grep -q '%PDF'

JOB_ID=$(jq -r '.jobId' "$WORK_DIR/generate.json")

# 허용 목록에 없는 변환은 거부한다.
http=$(curl --silent --output "$WORK_DIR/convert-denied.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$BASE_URL/convert" \
  --header 'Content-Type: application/json' \
  --data "{\"jobId\":\"$JOB_ID\",\"source\":\"report.docx\",\"target\":\"exe\"}")
[[ "$http" == "400" ]] || die "Expected HTTP 400 for denied conversion, received $http"

# 허용된 PPTX -> PDF 변환.
curl --fail --silent --show-error \
  --request POST \
  "$BASE_URL/convert" \
  --header 'Content-Type: application/json' \
  --data "{\"jobId\":\"$JOB_ID\",\"source\":\"report.pptx\",\"target\":\"pdf\"}" \
  --output "$WORK_DIR/convert.json"
converted_path=$(jq -r '.files[0].downloadPath' "$WORK_DIR/convert.json")
converted_hash=$(jq -r '.files[0].sha256' "$WORK_DIR/convert.json")
curl --fail --silent --show-error "$BASE_URL$converted_path" \
  --output "$WORK_DIR/report.pptx.pdf"
[[ "$(sha256_file "$WORK_DIR/report.pptx.pdf")" == "$converted_hash" ]] \
  || die "Hash mismatch: report.pptx.pdf"
head -c 4 "$WORK_DIR/report.pptx.pdf" | grep -q '%PDF'

# 허용 목록에 없는 편집 operation은 거부한다.
http=$(curl --silent --output "$WORK_DIR/edit-denied.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$BASE_URL/edit" \
  --header 'Content-Type: application/json' \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[{\"op\":\"runShell\",\"cmd\":\"id\"}]}")
[[ "$http" == "400" ]] || die "Expected HTTP 400 for denied edit, received $http"

# 수식 주입은 거부한다.
http=$(curl --silent --output "$WORK_DIR/edit-formula.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$BASE_URL/edit" \
  --header 'Content-Type: application/json' \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[{\"op\":\"setCell\",\"cell\":\"B2\",\"value\":\"=1+1\"}]}")
[[ "$http" == "400" ]] || die "Expected HTTP 400 for formula value, received $http"

# 허용된 선언적 편집.
curl --fail --silent --show-error \
  --request POST \
  "$BASE_URL/edit" \
  --header 'Content-Type: application/json' \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[
    {\"op\":\"setCell\",\"cell\":\"B2\",\"value\":\"approved-draft\"},
    {\"op\":\"renameSheet\",\"name\":\"Final\"},
    {\"op\":\"replaceText\",\"find\":\"Local smoke test\",\"replace\":\"Edited title\"}
  ]}" \
  --output "$WORK_DIR/edit.json"
test "$(jq '.applied' "$WORK_DIR/edit.json")" = "3"
test "$(jq '.files | length' "$WORK_DIR/edit.json")" = "2"
for file_name in report.edited.docx report.edited.xlsx; do
  download_path=$(jq -r --arg name "$file_name" \
    '.files[] | select(.name == $name) | .downloadPath' "$WORK_DIR/edit.json")
  expected_hash=$(jq -r --arg name "$file_name" \
    '.files[] | select(.name == $name) | .sha256' "$WORK_DIR/edit.json")
  curl --fail --silent --show-error "$BASE_URL$download_path" \
    --output "$WORK_DIR/$file_name"
  [[ "$(sha256_file "$WORK_DIR/$file_name")" == "$expected_hash" ]] \
    || die "Hash mismatch: $file_name"
  unzip -t "$WORK_DIR/$file_name" >/dev/null
done
python3 - "$WORK_DIR/report.edited.xlsx" "$WORK_DIR/report.edited.docx" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as workbook:
    workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
    assert 'name="Final"' in workbook_xml, "renameSheet was not applied"
    # openpyxl은 inline string으로 저장하므로 sheet XML에서 값을 확인한다.
    sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "approved-draft" in sheet_xml, "setCell was not applied"

with zipfile.ZipFile(sys.argv[2]) as document:
    body = document.read("word/document.xml").decode("utf-8")
    assert "Edited title" in body, "replaceText was not applied"
    assert "Local smoke test" not in body, "original text was not replaced"
print("edit assertions passed")
PY

# 존재하지 않는 job은 404다.
http=$(curl --silent --output "$WORK_DIR/convert-missing.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$BASE_URL/convert" \
  --header 'Content-Type: application/json' \
  --data '{"jobId":"deadbeef","source":"report.docx","target":"pdf"}')
[[ "$http" == "404" ]] || die "Expected HTTP 404 for missing job, received $http"

docker logs "$CONTAINER_NAME" > "$WORK_DIR/container.log" 2>&1
if grep -q 'must-not-appear-in-logs' "$WORK_DIR/container.log"; then
  die "Session identifier leaked into container logs"
fi
log "Local Office smoke test passed."
