#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")/../common" && pwd)/lib.sh"

for command in docker curl jq unzip; do
  require_cmd "$command"
done

IMAGE_NAME=${IMAGE_NAME:-office-sandbox:local-smoke}
CONTAINER_NAME="office-sandbox-smoke-$$"
WORK_DIR="$WORK_ROOT/dynamic-sessions/office-local"
mkdir -p "$WORK_DIR"

docker build \
  --file "$REPO_ROOT/dynamic_sessions/office_image/Dockerfile" \
  --tag "$IMAGE_NAME" \
  "$REPO_ROOT"
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
jq -e '.release == "dev"' "$WORK_DIR/health.json" >/dev/null
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

http=$(curl --silent --output "$WORK_DIR/invalid-control.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$BASE_URL/generate" \
  --header 'Content-Type: application/json' \
  --data '{"title":"invalid\u0000title","content":"body"}')
[[ "$http" == "400" ]] || die "Expected HTTP 400 for control character, received $http"

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
ORIGINAL_PDF_PATH=$(jq -r \
  '.files[] | select(.name == "report.pdf") | .downloadPath' \
  "$WORK_DIR/generate.json")
ORIGINAL_PDF_HASH=$(jq -r \
  '.files[] | select(.name == "report.pdf") | .sha256' \
  "$WORK_DIR/generate.json")

# Formula-like generation input must remain text in XLSX.
curl --fail --silent --show-error \
  --request POST \
  "$BASE_URL/generate" \
  --header 'Content-Type: application/json' \
  --data '{"title":"=1+1","content":"@SUM(1,1)"}' \
  --output "$WORK_DIR/formula-safe-generate.json"
FORMULA_XLSX_PATH=$(jq -r \
  '.files[] | select(.name == "report.xlsx") | .downloadPath' \
  "$WORK_DIR/formula-safe-generate.json")
curl --fail --silent --show-error "$BASE_URL$FORMULA_XLSX_PATH" \
  --output "$WORK_DIR/formula-safe.xlsx"
if unzip -p "$WORK_DIR/formula-safe.xlsx" xl/worksheets/sheet1.xml | grep -q '<f'; then
  die "Formula-like generation input created an XLSX formula"
fi

# Lines longer than Excel's cell limit must be split without data loss.
python3 - "$WORK_DIR/long-line-request.json" <<'PY'
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump({"title": "Long line", "content": "x" * 40000}, output)
PY
curl --fail --silent --show-error \
  --request POST \
  "$BASE_URL/generate" \
  --header 'Content-Type: application/json' \
  --data-binary "@$WORK_DIR/long-line-request.json" \
  --output "$WORK_DIR/long-line-generate.json"
LONG_XLSX_PATH=$(jq -r \
  '.files[] | select(.name == "report.xlsx") | .downloadPath' \
  "$WORK_DIR/long-line-generate.json")
curl --fail --silent --show-error "$BASE_URL$LONG_XLSX_PATH" \
  --output "$WORK_DIR/long-line.xlsx"
python3 - "$WORK_DIR/long-line.xlsx" <<'PY'
import sys
import xml.etree.ElementTree as ET
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
texts = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
chunks = [text for text in texts if text and set(text) == {"x"}]
assert sum(map(len, chunks)) == 40000
assert max(map(len, chunks)) <= 32767
PY

# Markdown-like file and URL syntax must remain literal text without embedded media.
curl --fail --silent --show-error \
  --request POST \
  "$BASE_URL/generate" \
  --header 'Content-Type: application/json' \
  --data '{"title":"Literal text","content":"![local](/etc/hostname)\n![remote](https://example.com/image.png)"}' \
  --output "$WORK_DIR/literal-generate.json"
LITERAL_DOCX_PATH=$(jq -r \
  '.files[] | select(.name == "report.docx") | .downloadPath' \
  "$WORK_DIR/literal-generate.json")
curl --fail --silent --show-error "$BASE_URL$LITERAL_DOCX_PATH" \
  --output "$WORK_DIR/literal.docx"
if unzip -l "$WORK_DIR/literal.docx" | grep -q 'word/media/'; then
  die "Markdown-like input embedded external or local media"
fi
unzip -p "$WORK_DIR/literal.docx" word/document.xml | grep -q '/etc/hostname'

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

# 변환이 같은 stem의 기존 report.pdf를 덮어쓰거나 이동하면 안 된다.
curl --fail --silent --show-error "$BASE_URL$ORIGINAL_PDF_PATH" \
  --output "$WORK_DIR/report.after-convert.pdf"
[[ "$(sha256_file "$WORK_DIR/report.after-convert.pdf")" == "$ORIGINAL_PDF_HASH" ]] \
  || die "Original report.pdf changed during PPTX conversion"

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

http=$(curl --silent --output "$WORK_DIR/edit-control.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$BASE_URL/edit" \
  --header 'Content-Type: application/json' \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[
    {\"op\":\"setCell\",\"cell\":\"B2\",\"value\":\"bad\\u0000value\"}
  ]}")
[[ "$http" == "400" ]] || die "Expected HTTP 400 for control character, received $http"

# If a later edit fails, earlier operations in the batch must roll back.
http=$(curl --silent --output "$WORK_DIR/edit-rollback.json" \
  --write-out '%{http_code}' \
  --request POST \
  "$BASE_URL/edit" \
  --header 'Content-Type: application/json' \
  --data "{\"jobId\":\"$JOB_ID\",\"operations\":[
    {\"op\":\"setCell\",\"cell\":\"B2\",\"value\":\"must-not-persist\"},
    {\"op\":\"replaceText\",\"find\":\"\",\"replace\":\"invalid\"}
  ]}")
[[ "$http" == "400" ]] || die "Expected HTTP 400 for failed edit batch, received $http"
http=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  "$BASE_URL/files/$JOB_ID/report.edited.xlsx")
[[ "$http" == "404" ]] || die "Failed edit batch left report.edited.xlsx behind"

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
test "$(jq '.files | length' "$WORK_DIR/edit.json")" = "3"
for file_name in \
  report.edited.docx \
  report.edited.pptx \
  report.edited.xlsx; do
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
python3 - \
  "$WORK_DIR/report.edited.xlsx" \
  "$WORK_DIR/report.edited.docx" \
  "$WORK_DIR/report.edited.pptx" <<'PY'
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

with zipfile.ZipFile(sys.argv[3]) as presentation:
    slides = "".join(
        presentation.read(name).decode("utf-8")
        for name in presentation.namelist()
        if name.startswith("ppt/slides/slide") and name.endswith(".xml")
    )
    assert "Edited title" in slides, "PPTX replaceText was not applied"
    assert "Local smoke test" not in slides, "PPTX original text was not replaced"
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
