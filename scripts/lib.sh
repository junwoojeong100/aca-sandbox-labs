#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK_ROOT=${WORK_ROOT:-"$REPO_ROOT/.work"}

log() {
  printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

require_base_commands() {
  for command in az curl jq python3 unzip file git; do
    require_cmd "$command"
  done
  if ! command -v shasum >/dev/null 2>&1 \
    && ! command -v sha256sum >/dev/null 2>&1; then
    die "Required command not found: shasum or sha256sum"
  fi
}

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

expect_2xx() {
  case "$1" in
    2??) ;;
    *) die "$2 returned HTTP $1. Response: $(cat "$3")" ;;
  esac
}

ensure_role_assignment() {
  local role=$1
  local principal_id=$2
  local principal_type=$3
  local scope=$4
  local error_file
  error_file=$(mktemp)
  if ! az role assignment create \
    --role "$role" \
    --assignee-object-id "$principal_id" \
    --assignee-principal-type "$principal_type" \
    --scope "$scope" \
    --output none 2>"$error_file"; then
    if ! grep -q 'RoleAssignmentExists' "$error_file"; then
      cat "$error_file" >&2
      rm -f "$error_file"
      return 1
    fi
  fi
  rm -f "$error_file"
}

wait_for_pool() {
  local pool_name=$1
  local resource_group=$2
  local expected_image=${3:-}
  local attempt state image
  for attempt in $(seq 1 40); do
    state=$(az containerapp sessionpool show \
      --name "$pool_name" \
      --resource-group "$resource_group" \
      --query properties.provisioningState \
      --output tsv 2>/dev/null || true)
    image=$(az containerapp sessionpool show \
      --name "$pool_name" \
      --resource-group "$resource_group" \
      --query properties.customContainerTemplate.containers[0].image \
      --output tsv 2>/dev/null || true)
    if [[ "$state" == "Succeeded" ]] \
      && { [[ -z "$expected_image" ]] || [[ "$image" == "$expected_image" ]]; }; then
      return
    fi
    [[ "$state" != "Failed" ]] || die "Session pool provisioning failed: $pool_name"
    sleep 15
  done
  die "Timed out waiting for session pool: $pool_name"
}

