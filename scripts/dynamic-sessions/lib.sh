#!/usr/bin/env bash

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../common" && pwd)/lib.sh"

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
    [[ "$state" != "Failed" ]] \
      || die "Session pool provisioning failed: $pool_name"
    sleep 15
  done
  die "Timed out waiting for session pool: $pool_name"
}
