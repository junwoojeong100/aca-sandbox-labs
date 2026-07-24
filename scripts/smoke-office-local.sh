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
docker logs "$CONTAINER_NAME" > "$WORK_DIR/container.log" 2>&1
if grep -q 'must-not-appear-in-logs' "$WORK_DIR/container.log"; then
  die "Session identifier leaked into container logs"
fi
log "Local Office smoke test passed."
