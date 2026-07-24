#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

require_base_commands
az account show --output none

log "Azure CLI: $(az version --query '"azure-cli"' --output tsv)"
log "Subscription: $(az account show --query name --output tsv)"
log "All prerequisites are available."
