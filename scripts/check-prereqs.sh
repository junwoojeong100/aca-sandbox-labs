#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"

require_base_commands
az account show --output none

MINIMUM_AZ_CLI_VERSION=2.79.0
AZ_CLI_VERSION=$(az version --query '"azure-cli"' --output tsv)

python3 - "$AZ_CLI_VERSION" "$MINIMUM_AZ_CLI_VERSION" <<'PY'
import re
import sys


def version_tuple(value):
    parts = [int(part) for part in re.findall(r"\d+", value)]
    return tuple((parts + [0, 0, 0])[:3])


current, minimum = sys.argv[1:3]
if version_tuple(current) < version_tuple(minimum):
    raise SystemExit(
        f"Azure CLI {minimum} or later is required; found {current}"
    )
PY

log "Azure CLI: $AZ_CLI_VERSION"
log "Subscription: $(az account show --query name --output tsv)"
log "Local tools, Azure CLI version, and Azure login are valid."
log "Lab scripts check service quota and required permissions while running."
