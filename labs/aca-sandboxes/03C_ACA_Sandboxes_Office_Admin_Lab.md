# 실습 3C: ACA Sandboxes Office Custom Image - 관리자

## 목표

LibreOffice, Pandoc, Poppler를 포함한 Office 도구 이미지를 OCI disk image로 ACA Sandboxes에 등록하고 다음을 검증한다.

- ACR private image를 SandboxGroup disk image로 변환·등록
- 단기 ACR refresh token 기반 private image pull
- Sandbox에서 `exec()`로 LibreOffice·Python 직접 실행
- DOCX·PDF·PPTX·XLSX **생성**
- 허용 목록 기반 형식 **변환** (LibreOffice)
- DOCX·PPTX text와 XLSX cell의 선언적 **편집**
- 파일 형식과 SHA-256 검증
- `EgressPolicy(default_action='Deny', traffic_inspection='Full')` 외부 통신 차단
- Suspend와 Resume으로 Office 작업 Sandbox 상태 보존

예상 시간은 60~90분이며 ACR 이미지 빌드와 disk image 변환 시간은 별도다.

> Office Sandbox는 L tier(2 vCPU, 4GB)를 사용해 실행 중 초 단위 compute
> 비용이 발생한다. Fast Path는 active Sandbox를 삭제하지만 Ready disk
> image와 ACR image는 남긴다. 재사용하지 않으면 14절과 실습 3A §19절에서
> 정리한다.

### Custom Image가 필요한 경우

| 필요한 작업 | 권장 방식 |
| --- | --- |
| DOCX·XLSX·PPTX 생성만 | 실습 3A의 `python-3.12` 공개 image |
| LibreOffice 기반 PDF 변환 | Custom Image (이 실습) |
| CJK 폰트 렌더링 고정 | Custom Image (이 실습) |
| 도구 버전을 image digest로 고정 | Custom Image (이 실습) |
| Pandoc, Poppler 필요 | Custom Image (이 실습) |

## 1. 사전 조건

- Bash 또는 Azure Cloud Shell과 Azure CLI 2.79.0 이상
- Python 3.10 이상과 `pip`
- `curl`, `jq`, `file`, `shasum` 또는 `sha256sum`
- 대상 subscription의 Contributor
- role assignment를 위한 Owner 또는 User Access Administrator
- `aca_sandboxes/images/office/`의 Dockerfile
- 수동 경로에서는 [실습 3A](03A_ACA_Sandboxes_Admin_Lab.md)의 SandboxGroup 생성 완료. Fast Path는 자동 준비

현재 subscription과 필수 도구를 확인한다.

```bash
az account show --query '{name:name,id:id,user:user.name}' --output table
command -v az python3 curl jq file
```

Fast Path는 SDK를 전용 virtual environment에 설치한다. 수동 경로에서는
실습 3A §3의 virtual environment를 먼저 활성화한다.

### 권장 Fast Path

repository root에서 다음 한 명령을 실행한다.

```bash
bash scripts/aca-sandboxes/quickstart.sh office
```

이 스크립트는 전용 virtual environment와 SDK, SandboxGroup·RBAC을
준비한다. Ready Office disk image가 없으면 ACR을 생성 또는 재사용하고
`aca_sandboxes/images/office/` image를 build·등록한다. 생성·변환·편집·egress와
suspend/resume을 검증한 뒤 active Sandbox만 삭제하고, SandboxGroup과
Ready disk image는 재사용을 위해 남긴다.

2026-07-28 한국 중부에서 `azure-containerapps-sandbox 0.1.0b4`로 실제
검증한 경로다. 결과는 `.work/aca-sandboxes/live/office-validation.json`에
저장된다.

## 2. 변수 설정

```bash
export SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}"
export RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ai-workspace-aca-sandboxes-lab}"
export LOCATION="${LOCATION:-koreacentral}"
export SANDBOX_GROUP_NAME="${SANDBOX_GROUP_NAME:-ai-workspace-sandboxes}"
export ACR_NAME="${ACR_NAME:-aiwssbx$(printf '%s' "$SUBSCRIPTION_ID" | tr -d '-' | cut -c1-20)}"
export IMAGE_REPOSITORY="office-sandbox"
export IMAGE_TAG="$(date -u +%Y%m%d%H%M%S)"
export IMAGE="$ACR_NAME.azurecr.io/$IMAGE_REPOSITORY:$IMAGE_TAG"
export LAB_WORK_DIR="$PWD/.work/aca-sandboxes/office-manual"

az account set --subscription "$SUBSCRIPTION_ID"
mkdir -p "$LAB_WORK_DIR"
```

기존 ACR이 있으면 동일한 이름을 재사용할 수 있다. 없으면 3절에서 새로 만든다.

## 3. ACR 확인 또는 생성

```bash
# 이미 있으면 건너뜀
az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --query '{name:name,loginServer:loginServer}' --output json 2>/dev/null || {
  az provider register --namespace Microsoft.ContainerRegistry --wait
  az acr create \
    --name "$ACR_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Basic \
    --admin-enabled false \
    --tags purpose=ai-workspace-office-sandbox \
    --output none
}
```

## 4. Private ACR 인증 제한 확인

2026-07-28 기준 `azure-containerapps-sandbox 0.1.0b4`의
`managed_identity_resource_id`는 private ACR disk image 생성에서 실제로
동작하지 않았다. 서비스는 `RegistryAuthFailed`와 함께
`registryCredentials` 또는 `managedIdentityClientId`를 요구했다.

따라서 이 실습은 `az acr login --expose-token`이 반환하는 단기 ACR refresh
token을 메모리에서만 `RegistryCredentials`로 전달한다. token을 파일,
shell history 또는 로그에 출력하지 않는다. Managed Identity pull이
서비스와 SDK에서 지원되면 별도의 ACR pull identity를 만들고 6절을
`managed_identity_resource_id` 방식으로 전환한다.

## 5. Office Container 이미지 빌드와 ACR 푸시

ACA Sandboxes의 Office 작업은 `sandbox.exec()`로 도구를 직접 호출한다.
`aca_sandboxes/images/office/` image는 LibreOffice와 Python 도구를
포함하고 disk image entrypoint는 장기 실행 상태를 유지하도록 구성한다.

```bash
# ACR cloud build (Docker 데몬 없이)
az acr build \
  --registry "$ACR_NAME" \
  --image "$IMAGE_REPOSITORY:$IMAGE_TAG" \
  --file aca_sandboxes/images/office/Dockerfile \
  . \
  --output none

echo "Built image: $IMAGE"
```

빌드를 확인한다.

```bash
az acr repository show-tags \
  --name "$ACR_NAME" \
  --repository "$IMAGE_REPOSITORY" \
  --orderby time_desc \
  --top 3 \
  --output table
```

## 6. SandboxGroup에 Disk Image 등록

OCI 이미지를 ACA Sandboxes disk image로 변환·등록한다.
Sandboxes가 container image를 VM root filesystem으로 변환한다.

```python
# 6-create-disk-image.py
import os
import json
import subprocess
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import (
    RegistryCredentials, SandboxGroupClient, endpoint_for_region,
)

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
ACR_NAME        = os.environ["ACR_NAME"]
IMAGE_REPOSITORY = os.environ.get("IMAGE_REPOSITORY", "office-sandbox")
IMAGE_TAG       = os.environ["IMAGE_TAG"]
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/office-manual")

IMAGE = f"{ACR_NAME}.azurecr.io/{IMAGE_REPOSITORY}:{IMAGE_TAG}"

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION),
    credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)

print(f"Converting OCI image to disk image: {IMAGE}")
print("This may take several minutes...")

# 단기 ACR refresh token을 메모리에서만 사용한다.
token_result = subprocess.run(
    [
        "az", "acr", "login",
        "--name", ACR_NAME,
        "--expose-token",
        "--output", "json",
    ],
    check=True,
    capture_output=True,
    text=True,
)
token = json.loads(token_result.stdout)

disk_image = client.begin_create_disk_image(
    IMAGE,
    name=f"office-{IMAGE_TAG}",
    # 기존 image의 HTTP server CMD를 실행하지 않는다.
    entrypoint=["/bin/sh", "-c"],
    cmd=["sleep infinity"],
    registry_credentials=RegistryCredentials(
        username=token["username"],
        token=token["refreshToken"],
    ),
).result()

print(f"Disk image created:")
print(f"  id    : {disk_image.id}")
print(f"  name  : {disk_image.name}")
print(f"  status: {disk_image.status.state if disk_image.status else 'n/a'}")

# 이후 절에서 사용할 disk_image ID 저장
with open(f"{WORK_DIR}/disk_image_id.txt", "w") as f:
    f.write(disk_image.id)

client.close()
```

```bash
python3 6-create-disk-image.py
```

disk image 목록을 확인한다.

```bash
python3 -c "
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ['SUBSCRIPTION_ID']
RESOURCE_GROUP  = os.environ.get('RESOURCE_GROUP', 'rg-ai-workspace-aca-sandboxes-lab')
LOCATION        = os.environ.get('LOCATION', 'koreacentral')
SANDBOX_GROUP   = os.environ.get('SANDBOX_GROUP_NAME', 'ai-workspace-sandboxes')

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
for img in client.list_disk_images():
    print(f'  {img.id}  state={img.status.state if img.status else \"n/a\"}')
client.close()
"
```

## 7. Office Sandbox 생성

등록한 disk image로 Office 작업 전용 Sandbox를 만든다.

```python
# 7-create-office-sandbox.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import (
    SandboxGroupClient, endpoint_for_region, EgressPolicy,
)

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/office-manual")

with open(f"{WORK_DIR}/disk_image_id.txt") as f:
    disk_image_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)

print("Creating Office sandbox from custom disk image...")
sandbox_client = client.begin_create_sandbox(
    disk=None,                   # disk_id와 기본 public disk를 함께 지정하지 않음
    disk_id=disk_image_id,       # 커스텀 disk image 지정
    cpu="2000m",                 # L tier: 2 vCPU
    memory="4096Mi",             # L tier: 4 GB
    egress_policy=EgressPolicy(
        default_action="Deny",
        traffic_inspection="Full",
    ),
    auto_suspend_seconds=1800,
    auto_suspend_mode="Memory",
    labels={"lab": "sandboxes-03c-office"},
).result()

print(f"Office sandbox created: {sandbox_client.sandbox_id}")
print(f"  state: {sandbox_client.get().state}")

# Sandbox 초기화 확인
result = sandbox_client.exec(
    "libreoffice --version 2>&1 || echo 'LIBREOFFICE_MISSING'"
)
print(f"LibreOffice: {result.stdout.strip()}")

result2 = sandbox_client.exec(
    "python3 --version && "
    "python3 -c 'import docx, pptx, openpyxl; print(\"Office libs OK\")'"
)
print(f"Python libs: {result2.stdout.strip()}")

with open(f"{WORK_DIR}/office_sandbox_id.txt", "w") as f:
    f.write(sandbox_client.sandbox_id)

sandbox_client.close()
client.close()
```

```bash
python3 7-create-office-sandbox.py
```

통과 기준:

- Sandbox 상태 `Running`
- `libreoffice --version`이 버전 문자열 반환
- Python Office 라이브러리 import 성공

> `LIBREOFFICE_MISSING`이 나오면 `aca_sandboxes/images/office/Dockerfile`을 확인하고 disk image를 재등록한다.

## 8. Health 및 도구 확인

HTTP 서버 없이 `exec()`로 직접 도구를 확인한다.

```python
# 8-health-check.py
import json
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/office-manual")

with open(f"{WORK_DIR}/office_sandbox_id.txt") as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

# 도구 버전과 라이브러리 확인 (health 계약과 동일)
result = sb.exec(
    "python3 -c \""
    "import json, subprocess, importlib\n"
    "lo = subprocess.run(['libreoffice','--version'], capture_output=True, text=True).stdout.strip()\n"
    "libs = {}\n"
    "for name in ['docx','pptx','openpyxl']:\n"
    "    try:\n"
    "        m = importlib.import_module(name)\n"
    "        libs[name] = getattr(m,'__version__','ok')\n"
    "    except ImportError:\n"
    "        libs[name] = 'MISSING'\n"
    "print(json.dumps({'libreoffice':lo,'python_libs':libs}))\n"
    "\""
)
health = json.loads(result.stdout)
print(json.dumps(health, indent=2, ensure_ascii=False))

assert "MISSING" not in str(health["python_libs"].values()), "라이브러리가 누락됨"
assert "LibreOffice" in health["libreoffice"], "LibreOffice 버전 확인 실패"
print("\n✓ Office 도구 health 확인 통과")

sb.close()
client.close()
```

```bash
python3 8-health-check.py
```

## 9. DOCX·PDF·PPTX·XLSX 생성

Python 라이브러리와 LibreOffice를 `exec()`로 직접 호출한다.

```python
# 9-generate.py
import hashlib
import json
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/office-manual")

with open(f"{WORK_DIR}/office_sandbox_id.txt") as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

TITLE   = "AI Workspace 격리형 Sandbox 검증 보고서"
CONTENT = "Azure Container Apps Sandboxes Office Custom Image에서 생성했습니다.\n\n- Custom disk image 기반 격리 실행\n- Egress 기본 차단\n- 승인 전 실제 업무 시스템 반영 금지"

# 작업 디렉터리 초기화
sb.mkdir("/work")

# Python으로 DOCX, PPTX, XLSX 생성 스크립트 작성
generate_py = f"""\
import json, hashlib, pathlib
from docx import Document
from pptx import Presentation
from pptx.util import Inches, Pt
import openpyxl

TITLE   = {repr(TITLE)}
CONTENT = {repr(CONTENT)}
out     = pathlib.Path("/work")

# DOCX
doc = Document()
doc.add_heading(TITLE, level=1)
doc.add_paragraph(CONTENT)
doc.save(str(out / "report.docx"))

# PPTX
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = TITLE
slide.placeholders[1].text = CONTENT
prs.save(str(out / "slides.pptx"))

# XLSX
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Summary"
ws["A1"] = TITLE
ws["A2"] = CONTENT
wb.save(str(out / "report.xlsx"))

# PDF는 설치되지 않은 reportlab이 아니라 LibreOffice 변환으로 생성한다.
import subprocess
for source in ["report.docx", "slides.pptx"]:
    subprocess.run([
        "libreoffice", "--headless", "--convert-to", "pdf",
        "--outdir", str(out), str(out / source)
    ], check=True, capture_output=True)

# 파일 크기와 hash 계산
results = {{}}
for name in [
    "report.docx", "report.pdf", "slides.pptx", "slides.pdf", "report.xlsx"
]:
    data = (out / name).read_bytes()
    results[name] = {{"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
print(json.dumps(results, indent=2))
"""
sb.write_file("/work/generate.py", generate_py)

print("Generating Office documents...")
result = sb.exec("python3 /work/generate.py")
if result.exit_code != 0:
    print(f"stderr: {result.stderr}")
    raise RuntimeError(f"Generation failed (exit {result.exit_code})")

metadata = json.loads(result.stdout)
print(json.dumps(metadata, indent=2, ensure_ascii=False))

# 결과 파일 다운로드
for name in [
    "report.docx", "report.pdf", "slides.pptx", "slides.pdf", "report.xlsx"
]:
    raw = sb.read_file(f"/work/{name}")
    local_path = os.path.join(WORK_DIR, name)
    with open(local_path, "wb") as f:
        f.write(raw)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    expected_sha256 = metadata[name]["sha256"]
    assert actual_sha256 == expected_sha256, f"{name} SHA-256 불일치"
    print(f"✓ {name}: {len(raw):,} bytes, sha256={actual_sha256[:16]}...")

# metadata 저장 (10~11절에서 사용)
with open(os.path.join(WORK_DIR, "generate_metadata.json"), "w") as f:
    json.dump(metadata, f, indent=2)

sb.close()
client.close()
```

```bash
python3 9-generate.py
```

통과 기준:

- 네 파일 모두 생성(DOCX, PDF, PPTX, XLSX)
- 다운로드한 파일의 SHA-256이 Sandbox 내 계산값과 일치
- `report.pdf`가 `%PDF` header로 시작

```bash
file .work/aca-sandboxes/office-manual/report.docx .work/aca-sandboxes/office-manual/report.pdf \
     .work/aca-sandboxes/office-manual/slides.pptx .work/aca-sandboxes/office-manual/slides.pdf \
     .work/aca-sandboxes/office-manual/report.xlsx
head -c 4 .work/aca-sandboxes/office-manual/report.pdf
```

## 10. 허용 목록 기반 형식 변환

허용된 변환 matrix를 gateway가 관리하고, `exec()`로 LibreOffice를 호출한다.

```python
# 10-convert.py
import hashlib
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/office-manual")

with open(f"{WORK_DIR}/office_sandbox_id.txt") as f:
    sandbox_id = f.read().strip()

# 허용 목록: {source_ext: [target_ext, ...]}
ALLOWED_CONVERSIONS = {
    "docx": ["pdf", "txt"],
    "pptx": ["pdf"],
    "xlsx": ["pdf"],
}

def convert_in_sandbox(sb, source: str, target: str) -> bytes:
    """허용 목록 검사 후 LibreOffice로 변환."""
    src_ext = source.rsplit(".", 1)[-1].lower()
    if src_ext not in ALLOWED_CONVERSIONS or target not in ALLOWED_CONVERSIONS[src_ext]:
        raise ValueError(
            f"변환 불가: {src_ext} → {target}. "
            f"허용 목록: {ALLOWED_CONVERSIONS}"
        )
    result = sb.exec(
        f"libreoffice --headless --convert-to {target} "
        f"--outdir /work /work/{source} 2>&1"
    )
    if result.exit_code != 0:
        raise RuntimeError(f"변환 실패: {result.stdout}\n{result.stderr}")
    converted_name = source.rsplit(".", 1)[0] + f".{target}"
    return sb.read_file(f"/work/{converted_name}")

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

# 허용되지 않은 변환 거부 확인
try:
    convert_in_sandbox(sb, "report.docx", "exe")
    print("UNEXPECTED: 변환 허용 - 허용 목록 누락")
except ValueError as e:
    print(f"✓ 허용 목록 차단: {e}")

# PPTX → PDF 변환
pdf_bytes = convert_in_sandbox(sb, "slides.pptx", "pdf")
sha256 = hashlib.sha256(pdf_bytes).hexdigest()
local_path = os.path.join(WORK_DIR, "slides.pdf")
with open(local_path, "wb") as f:
    f.write(pdf_bytes)
print(f"✓ PPTX → PDF 변환 완료: {len(pdf_bytes):,} bytes, sha256={sha256[:16]}...")
print(f"  PDF header: {pdf_bytes[:4]}")
assert pdf_bytes[:4] == b"%PDF", "PDF 헤더 불일치"

# DOCX → TXT 변환
txt_bytes = convert_in_sandbox(sb, "report.docx", "txt")
print(f"✓ DOCX → TXT 변환 완료: {len(txt_bytes):,} bytes")

sb.close()
client.close()
```

```bash
python3 10-convert.py
```

통과 기준:

- 허용 목록 밖 변환은 `ValueError`로 거부됨
- PPTX → PDF 변환 성공, `%PDF` 헤더 확인
- DOCX → TXT 변환 성공

## 11. 선언적 문서 편집

임의 shell command 없이 선언적 operation만 실행한다.

```python
# 11-edit.py
import hashlib
import json
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/office-manual")

with open(f"{WORK_DIR}/office_sandbox_id.txt") as f:
    sandbox_id = f.read().strip()

# 허용 operation 목록
ALLOWED_OPS = {"replaceText", "setCell", "renameSheet"}

def validate_operations(ops: list) -> None:
    for op in ops:
        if op.get("op") not in ALLOWED_OPS:
            raise ValueError(
                f"허용되지 않은 operation: {op.get('op')}. "
                f"허용 목록: {sorted(ALLOWED_OPS)}"
            )
        # 수식 주입 차단
        if op.get("op") == "setCell" and str(op.get("value", "")).startswith("="):
            raise ValueError("수식 주입 차단: value가 '='로 시작하는 값은 허용하지 않습니다.")

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

# 허용되지 않은 operation 차단 확인
try:
    validate_operations([{"op": "runShell", "cmd": "id"}])
except ValueError as e:
    print(f"✓ runShell 차단: {e}")

try:
    validate_operations([{"op": "setCell", "cell": "B2", "value": "=1+1"}])
except ValueError as e:
    print(f"✓ 수식 주입 차단: {e}")

# 허용된 편집 실행
operations = [
    {"op": "renameSheet", "name": "Final"},
    {"op": "setCell",     "cell": "B2", "value": "approved-draft"},
    {"op": "replaceText", "find": "검토 전",  "replace": "검토 완료"},
]
validate_operations(operations)

edit_py = f"""\
import json, hashlib
import openpyxl
from docx import Document
from pptx import Presentation
ops = {json.dumps(operations, ensure_ascii=False)}

results = {{}}
for op in ops:
    if op['op'] == 'renameSheet':
        wb = openpyxl.load_workbook('/work/report.xlsx')
        ws = wb.active
        ws.title = op['name']
        wb.save('/work/report.xlsx')
        results['renameSheet'] = 'ok'
    elif op['op'] == 'setCell':
        wb = openpyxl.load_workbook('/work/report.xlsx')
        wb.active[op['cell']] = op['value']
        wb.save('/work/report.xlsx')
        results['setCell'] = 'ok'
    elif op['op'] == 'replaceText':
        doc = Document('/work/report.docx')
        for para in doc.paragraphs:
            if op['find'] in para.text:
                for run in para.runs:
                    run.text = run.text.replace(op['find'], op['replace'])
        doc.save('/work/report.docx')
        prs = Presentation('/work/slides.pptx')
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        for run in para.runs:
                            run.text = run.text.replace(op['find'], op['replace'])
        prs.save('/work/slides.pptx')
        results['replaceText'] = 'ok'

# 편집된 파일 hash
hashes = {{}}
for name in ['report.docx', 'slides.pptx', 'report.xlsx']:
    data = open(f'/work/{{name}}', 'rb').read()
    hashes[name] = hashlib.sha256(data).hexdigest()
print(json.dumps({{'ops': results, 'hashes': hashes}}))
"""
sb.write_file("/work/edit.py", edit_py)
result = sb.exec("python3 /work/edit.py")
if result.exit_code != 0:
    print(f"stderr: {result.stderr}")
    raise RuntimeError(f"편집 실패 (exit {result.exit_code})")

edit_result = json.loads(result.stdout)
print(json.dumps(edit_result, indent=2, ensure_ascii=False))

# 편집된 파일 다운로드와 hash 검증
for name in ["report.docx", "slides.pptx", "report.xlsx"]:
    raw = sb.read_file(f"/work/{name}")
    sha256 = hashlib.sha256(raw).hexdigest()
    assert sha256 == edit_result["hashes"][name], f"{name} SHA-256 불일치"
    local_path = os.path.join(WORK_DIR, f"edited_{name}")
    with open(local_path, "wb") as f:
        f.write(raw)
    print(f"✓ {name}: sha256={sha256[:16]}... → {local_path}")

sb.close()
client.close()
```

```bash
python3 11-edit.py
```

통과 기준:

- `runShell`은 `ValueError`로 거부
- 수식(`=1+1`)은 `ValueError`로 거부
- `renameSheet`, `setCell`, `replaceText` 모두 성공
- 편집된 파일의 SHA-256이 Sandbox 내 계산값과 일치

## 12. Egress 차단 확인

Office Sandbox에서도 egress가 차단되는지 검증한다.

```python
# 12-egress.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/office-manual")

with open(f"{WORK_DIR}/office_sandbox_id.txt") as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

result = sb.exec(
    "curl --silent --max-time 5 https://example.com 2>&1 || echo EGRESS_BLOCKED"
)
print(f"egress test stdout: {result.stdout.strip()}")
assert "EGRESS_BLOCKED" in result.stdout or result.exit_code != 0, "Egress가 차단되지 않음"
print("✓ Office Sandbox Egress 차단 확인")

sb.close()
client.close()
```

```bash
python3 12-egress.py
```

## 13. Suspend와 Resume

Office 작업 Sandbox를 중단하고 재개한 뒤 파일 상태가 유지되는지 확인한다.

```python
# 13-suspend-resume.py
import hashlib
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/office-manual")

with open(f"{WORK_DIR}/office_sandbox_id.txt") as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

# Suspend 전 report.docx hash
docx_before = sb.read_file("/work/report.docx")
hash_before = hashlib.sha256(docx_before).hexdigest()
print(f"Suspend 전 report.docx sha256={hash_before[:16]}...")

sb.begin_stop().result()
info = sb.get()
print(f"Suspend 후 상태: {info.state}")

sb.begin_resume().result()
info = sb.get()
print(f"Resume 후 상태: {info.state}")

# Resume 후 파일 hash 비교
docx_after = sb.read_file("/work/report.docx")
hash_after = hashlib.sha256(docx_after).hexdigest()
print(f"Resume 후 report.docx sha256={hash_after[:16]}...")
assert hash_before == hash_after, "Resume 후 파일 내용 변경"
print("✓ Office Sandbox Suspend/Resume - 파일 상태 보존 확인")

sb.close()
client.close()
```

```bash
python3 13-suspend-resume.py
```

## 14. 정리

Office Sandbox와 disk image를 삭제한다.

```python
# 14-cleanup.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/office-manual")

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)

# Office Sandbox 삭제
with open(f"{WORK_DIR}/office_sandbox_id.txt") as f:
    sandbox_id = f.read().strip()
sb = client.get_sandbox_client(sandbox_id)
sb.delete()
print(f"Office sandbox deleted: {sandbox_id}")
sb.close()

# Disk image 삭제 (선택: 재사용 예정이면 보관)
with open(f"{WORK_DIR}/disk_image_id.txt") as f:
    disk_image_id = f.read().strip()
client.delete_disk_image(disk_image_id)
print(f"Disk image deleted: {disk_image_id}")

client.close()
```

```bash
export ACA_PYTHON="${ACA_PYTHON:-.work/aca-sandboxes/venv/bin/python}"
"$ACA_PYTHON" 14-cleanup.py
```

> disk image는 자동 삭제되지 않는다. 재사용 예정이면 `delete_disk_image()` 호출을 건너뛴다.
> SandboxGroup 전체를 삭제하려면 [실습 3A §19절](03A_ACA_Sandboxes_Admin_Lab.md#19-정리)의 스크립트를 사용한다.
