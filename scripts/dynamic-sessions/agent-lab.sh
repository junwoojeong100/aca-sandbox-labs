#!/usr/bin/env bash

set -euo pipefail
SCRIPT_ROOT=$(cd "$(dirname "$0")/.." && pwd)

AGENT_PYTHON=python3 \
AGENT_MODULE=dynamic_sessions.cli \
AGENT_WORK_DIR="${WORK_ROOT:-"$SCRIPT_ROOT/../.work"}/dynamic-sessions/agent" \
bash "$SCRIPT_ROOT/common/agent-validation.sh"
