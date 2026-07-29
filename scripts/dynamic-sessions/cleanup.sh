#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")/../common" && pwd)/lib.sh"

RESOURCE_GROUP=${RESOURCE_GROUP:-rg-ai-workspace-dynamic-sessions-lab}
[[ "${CONFIRM_DELETE:-}" == "yes" ]] \
  || die "Set CONFIRM_DELETE=yes to delete $RESOURCE_GROUP"

az group show --name "$RESOURCE_GROUP" --output none
az group delete --name "$RESOURCE_GROUP" --yes --no-wait

for attempt in $(seq 1 40); do
  if ! az group show --name "$RESOURCE_GROUP" --output none 2>/dev/null; then
    log "Deleted resource group: $RESOURCE_GROUP"
    exit 0
  fi
  state=$(az group show \
    --name "$RESOURCE_GROUP" \
    --query properties.provisioningState \
    --output tsv 2>/dev/null || true)
  log "Waiting for resource group deletion ($attempt/40, state=$state)"
  sleep 30
done

die "Timed out waiting for resource group deletion: $RESOURCE_GROUP"
