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

for source in (
    sorted(pathlib.Path(".").glob("agent/*.py"))
    + sorted(pathlib.Path(".").glob("office_gateway/*.py"))
    + sorted(pathlib.Path(".").glob("python_gateway/*.py"))
    + sorted(pathlib.Path(".").glob("tests/*.py"))
):
    ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
print("python sources parsed")
PY

python3 -m unittest discover -s tests >/dev/null

python3 <<'PY'
import pathlib
import re
import urllib.parse

root = pathlib.Path(".").resolve()
missing = []
missing_anchors = []


def github_slug(heading):
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = re.sub(r"[`*_~]", "", heading).strip().lower()
    heading = re.sub(r"[^\w\-\s\u0080-\uffff]", "", heading)
    return re.sub(r"\s+", "-", heading)


anchors = {}
for document in root.rglob("*.md"):
    counts = {}
    document_anchors = set()
    for line in document.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if not match:
            continue
        base = github_slug(match.group(1))
        duplicate = counts.get(base, 0)
        counts[base] = duplicate + 1
        document_anchors.add(base if duplicate == 0 else f"{base}-{duplicate}")
    anchors[document.resolve()] = document_anchors

for document in root.rglob("*.md"):
    text = document.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        relative_path, separator, fragment = target.partition("#")
        target_document = (
            (document.parent / relative_path).resolve()
            if relative_path
            else document.resolve()
        )
        if relative_path and not target_document.exists():
            missing.append(f"{document.relative_to(root)} -> {target}")
            continue
        decoded_fragment = urllib.parse.unquote(fragment)
        if separator and decoded_fragment not in anchors.get(target_document, set()):
            missing_anchors.append(f"{document.relative_to(root)} -> {target}")
if missing:
    raise SystemExit("Missing local links:\n" + "\n".join(missing))
if missing_anchors:
    raise SystemExit("Missing local anchors:\n" + "\n".join(missing_anchors))
PY

git -C "$REPO_ROOT" diff --check
log "Repository validation passed."
