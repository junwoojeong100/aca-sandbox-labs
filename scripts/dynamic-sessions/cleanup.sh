#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")/../common" && pwd)/lib.sh"

RESOURCE_GROUP=${RESOURCE_GROUP:-rg-ai-workspace-dynamic-sessions-lab}
[[ "${CONFIRM_DELETE:-}" == "yes" ]] \
  || die "Set CONFIRM_DELETE=yes to delete $RESOURCE_GROUP"

az group show --name "$RESOURCE_GROUP" --output none
az group delete --name "$RESOURCE_GROUP" --yes --no-wait
az group wait --name "$RESOURCE_GROUP" --deleted --interval 30 --timeout 1200
log "Deleted resource group: $RESOURCE_GROUP"
