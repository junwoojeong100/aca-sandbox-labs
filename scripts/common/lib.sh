#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
WORK_ROOT=${WORK_ROOT:-"$REPO_ROOT/.work"}
PYTHON_API_VERSION=${PYTHON_API_VERSION:-2025-10-02-preview}
SESSION_API_VERSION=${SESSION_API_VERSION:-2025-02-02-preview}
QUOTA_PORTAL_URL="https://portal.azure.com/#view/Microsoft_Azure_Capacity/QuotaMenuBlade/~/myQuotas"

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
  local error_file existing_role

  existing_role=$(az role assignment list \
    --scope "$scope" \
    --assignee-object-id "$principal_id" \
    --query "[?roleDefinitionName=='$role'].roleDefinitionName | [0]" \
    --output tsv 2>/dev/null || true)
  if [[ "$existing_role" == "$role" ]]; then
    log "Role assignment already exists: $role"
    return
  fi

  error_file=$(mktemp)
  if ! az role assignment create \
    --role "$role" \
    --assignee-object-id "$principal_id" \
    --assignee-principal-type "$principal_type" \
    --scope "$scope" \
    --output none 2>"$error_file"; then
    if ! grep -q 'RoleAssignmentExists' "$error_file"; then
      cat "$error_file" >&2
      if grep -Eqi 'AuthorizationFailed|Forbidden|does not have authorization' \
        "$error_file"; then
        printf '%s\n' \
          "ERROR: 역할을 할당할 권한이 없습니다." \
          "현재 identity에 Owner 또는 User Access Administrator가 필요합니다." \
          "권한을 받을 수 없다면 관리자가 다음 역할을 대신 할당해야 합니다: $role" \
          "Scope: $scope" >&2
      fi
      rm -f "$error_file"
      return 1
    fi
  fi
  rm -f "$error_file"
}

get_caller_object_id() {
  if [[ -n "${CALLER_OBJECT_ID:-}" ]]; then
    printf '%s\n' "$CALLER_OBJECT_ID"
    return
  fi

  az ad signed-in-user show --query id --output tsv 2>/dev/null \
    || die "로그인 사용자의 object ID를 확인하지 못했습니다. Service Principal을 사용한다면 CALLER_OBJECT_ID와 CALLER_PRINCIPAL_TYPE을 설정하세요."
}

check_regional_quota() {
  local resource_name=$1
  local display_name=$2
  local scope=$3
  local required=${4:-1}
  local error_file limit usage available
  error_file=$(mktemp)

  if ! limit=$(az quota show \
    --resource-name "$resource_name" \
    --scope "$scope" \
    --query properties.limit.value \
    --output tsv 2>"$error_file"); then
    log "WARNING: $display_name quota를 CLI에서 읽지 못했습니다."
    cat "$error_file" >&2
    log "Azure Portal > My quotas에서 Provider를 Azure Container Apps로 선택해 확인하세요."
    log "Quota portal: $QUOTA_PORTAL_URL"
    rm -f "$error_file"
    return
  fi
  rm -f "$error_file"

  usage=$(az quota usage show \
    --resource-name "$resource_name" \
    --scope "$scope" \
    --query properties.usages.value \
    --output tsv 2>/dev/null || true)

  if [[ ! "$limit" =~ ^[0-9]+$ ]] || [[ ! "$usage" =~ ^[0-9]+$ ]]; then
    log "WARNING: $display_name quota 값을 해석하지 못했습니다 (limit=$limit, usage=$usage)."
    log "Quota portal: $QUOTA_PORTAL_URL"
    return
  fi

  available=$((limit - usage))
  log "$display_name quota: limit=$limit, usage=$usage, available=$available"
  if (( available < required )); then
    die "$display_name quota가 부족합니다. 필요한 추가 수량=$required, 사용 가능=$available. Azure Portal에서 Azure Container Apps regional quota 증가를 요청하세요: $QUOTA_PORTAL_URL"
  fi
}
