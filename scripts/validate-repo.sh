#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
cd "$REPO_ROOT"

for script in "$REPO_ROOT"/scripts/*.sh; do
  bash -n "$script"
done

for lab in "$REPO_ROOT"/labs/*.md; do
  temporary_file=$(mktemp)
  awk '
    /^```bash$/ { in_block=1; next }
    /^```$/ {
      if (in_block) {
        in_block=0
        print ""
      }
      next
    }
    in_block { print }
  ' "$lab" > "$temporary_file"
  bash -n "$temporary_file"
  rm -f "$temporary_file"
done

python3 -c \
  'import ast, pathlib; ast.parse(pathlib.Path("office-container/server.py").read_text())'

# agent package와 테스트의 구문을 확인한다.
python3 - <<'PY'
import ast
import pathlib

for source in sorted(pathlib.Path(".").glob("agent/*.py")) + sorted(
    pathlib.Path(".").glob("tests/*.py")
):
    ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
print("python sources parsed")
PY

python3 -m unittest discover -s tests >/dev/null

python3 <<'PY'
import pathlib
import re

root = pathlib.Path(".").resolve()
missing = []
for document in root.rglob("*.md"):
    text = document.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        relative_path = target.split("#", 1)[0]
        if relative_path and not (document.parent / relative_path).resolve().exists():
            missing.append(f"{document.relative_to(root)} -> {target}")
if missing:
    raise SystemExit("Missing local links:\n" + "\n".join(missing))
PY

git -C "$REPO_ROOT" diff --check
log "Repository validation passed."
