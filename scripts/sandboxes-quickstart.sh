#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
cd "$REPO_ROOT"

MODE=${1:-python}
VENV_DIR=${SANDBOXES_VENV_DIR:-"$REPO_ROOT/.work/sandboxes-venv"}

case "$MODE" in
  python|office|all) ;;
  *) die "Usage: bash scripts/sandboxes-quickstart.sh [python|office|all]" ;;
esac

bash scripts/check-prereqs.sh

python3 - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit(
        f"Python 3.10 or later is required; found {sys.version.split()[0]}"
    )
PY

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  log "Creating ACA Sandboxes virtual environment: $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit(
        f"Sandbox virtual environment requires Python 3.10 or later; "
        f"found {sys.version.split()[0]}"
    )
PY

log "Installing the validated ACA Sandboxes SDK"
"$VENV_DIR/bin/python" -m pip install \
  --disable-pip-version-check \
  "azure-containerapps-sandbox==0.1.0b4" \
  azure-identity

case "$MODE" in
  python)
    "$VENV_DIR/bin/python" scripts/sandboxes-lab.py
    ;;
  office)
    "$VENV_DIR/bin/python" scripts/sandboxes-lab.py --provision-only
    "$VENV_DIR/bin/python" scripts/sandboxes-office-lab.py
    ;;
  all)
    "$VENV_DIR/bin/python" scripts/sandboxes-lab.py
    "$VENV_DIR/bin/python" scripts/sandboxes-office-lab.py
    ;;
esac
