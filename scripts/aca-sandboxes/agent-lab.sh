#!/usr/bin/env bash

set -euo pipefail
SCRIPT_ROOT=$(cd "$(dirname "$0")/.." && pwd)
REPO_ROOT=$(cd "$SCRIPT_ROOT/.." && pwd)
ACA_PYTHON=${ACA_PYTHON:-"$REPO_ROOT/.work/aca-sandboxes/venv/bin/python"}

[[ -x "$ACA_PYTHON" ]] || {
  printf 'ERROR: Run bash scripts/aca-sandboxes/quickstart.sh python first.\n' >&2
  exit 1
}

AGENT_PYTHON="$ACA_PYTHON" \
AGENT_MODULE=aca_sandboxes.cli \
AGENT_WORK_DIR="${WORK_ROOT:-"$REPO_ROOT/.work"}/aca-sandboxes/agent" \
LONG_REQUEST_CLASS=A \
LONG_REQUEST_ALLOWED=true \
bash "$SCRIPT_ROOT/common/agent-validation.sh"
