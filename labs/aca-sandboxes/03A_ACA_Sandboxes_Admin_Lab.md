# 실습 3A: ACA Sandboxes - 관리자

## 목표

Azure Container Apps Sandboxes(Public Preview)로 다음을 실제 검증한다.

- SandboxGroup(ARM) 생성과 RBAC
- `aca_sandboxes/images/python/` Code Interpreter custom disk image 기반 Sandbox 생성
- Python SDK로 코드 실행
- 파일 쓰기·읽기·목록·삭제
- `EgressPolicy(default_action='Deny', traffic_inspection='Full')` 외부 통신 차단
- **Sandbox 간 파일 격리**
- **실행 오류 분석, 코드 수정, 재실행**
- **Suspend(중단)와 Resume(재개)**
- **Snapshot 생성**
- **Sandbox 삭제와 정리**

예상 시간은 60~90분이다.

이 문서는 관리자가 SandboxGroup, RBAC, Sandbox 환경을 구성·검증하는 절차다.
자연어 요청과 승인 중심의 사용자 실습은 [실습 3B](03B_ACA_Sandboxes_User_Lab.md)에서 수행한다.

> 실행 중 Sandbox는 Container Apps Consumption과 같은 vCPU·memory 초 단위
> 종량제다. 중지·suspend 시 compute 비용은 없지만 disk image, snapshot,
> volume과 ACR storage는 별도 lifecycle을 가진다. Fast Path는 검증용
> Sandbox를 삭제하지만 SandboxGroup과 Ready disk image는 재사용을 위해
> 남기므로 마지막에 19절을 확인한다.

> **Public Preview 주의사항**
> ACA Sandboxes는 현재 Public Preview 상태다. 미리 보기 중에 생성된 Sandbox는 향후 릴리스와 호환되지 않을 수 있으며 재생성이 필요할 수 있다. Python SDK와 aca CLI의 API 표면은 미리 보기 중에 변경될 수 있다.

## 1. 사전 조건

- Bash 또는 Azure Cloud Shell
- Azure CLI 2.79.0 이상
- Python 3.10 이상과 `pip`
- `curl`, `jq`
- 대상 subscription의 Contributor
- role assignment를 위한 Owner 또는 User Access Administrator
- ACA Sandboxes 지원 리전(koreacentral 포함)의 quota

현재 subscription과 필수 도구를 확인한다.

```bash
az account show --query '{name:name,id:id,user:user.name}' --output table
command -v az python3 curl jq
python3 --version
```

### 권장 Fast Path

repository root에서 다음 한 명령을 실행한다.

```bash
bash scripts/aca-sandboxes/quickstart.sh python
```

이 스크립트는 사전 조건 검사, `.work/aca-sandboxes/venv` 생성, 검증된 SDK
설치, provider·Resource Group·RBAC·SandboxGroup 준비를 수행한다. Ready
상태의 `python-code-interpreter-*` disk image가 없으면 ACR을 생성 또는
재사용하고 `aca_sandboxes/images/python/` image를 build·등록한 뒤 검증을 계속한다.
새 RBAC 역할의 데이터 평면 전파가 늦으면 최대 3분 동안 자동 재시도한다.

기본값은 현재 `az` subscription, `koreacentral`, `rg-ai-workspace-aca-sandboxes-lab`, `ai-workspace-sandboxes`다.
다른 값을 쓰려면 2절의 환경 변수를 명령 실행 전에 설정한다.

아래 절은 자동 스크립트가 수행하는 세부 명령을 설명한다.

2026-07-28 한국 중부에서 `azure-containerapps-sandbox 0.1.0b4`로
SandboxGroup 생성, Python 3.12 실행, 파일 I/O, 두 Sandbox 간 격리,
Full inspection egress 차단, suspend/resume과 active Sandbox 정리를 실제
검증했다. 결과는 `.work/aca-sandboxes/live/python-validation.json`에 저장된다.

## 2. 변수 설정

```bash
export SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-$(az account show --query id --output tsv)}"
export RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ai-workspace-aca-sandboxes-lab}"
export LOCATION="${LOCATION:-koreacentral}"
export SANDBOX_GROUP_NAME="${SANDBOX_GROUP_NAME:-ai-workspace-sandboxes}"
export PYTHON_SANDBOX_DISK_ID="${PYTHON_SANDBOX_DISK_ID:-}"
export LAB_WORK_DIR="$PWD/.work/aca-sandboxes/python-manual"

az account set --subscription "$SUBSCRIPTION_ID"
az account show --query '{subscription:id,user:user.name}' --output json
mkdir -p "$LAB_WORK_DIR"
```

Code Interpreter image source는 `aca_sandboxes/images/python/Dockerfile`과
`aca_sandboxes/images/python/requirements.txt`이며
pandas, NumPy, matplotlib, SciPy, scikit-learn과 Office 생성 라이브러리를
포함한다.

Fast Path는 Ready disk image가 없을 때 아래 수동 명령과 같은 ACR build와
disk image 등록을 자동 수행한다.

```bash
export ACR_NAME="${ACR_NAME:-aiwssbx$(printf '%s' "$SUBSCRIPTION_ID" | tr -d '-' | cut -c1-20)}"
export PYTHON_IMAGE_TAG="$(date -u +%Y%m%d%H%M%S)"

az acr show \
  --name "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --output none 2>/dev/null || {
    az provider register --namespace Microsoft.ContainerRegistry --wait
    az acr create \
      --name "$ACR_NAME" \
      --resource-group "$RESOURCE_GROUP" \
      --location "$LOCATION" \
      --sku Basic \
      --admin-enabled false \
      --output none
  }

az acr build \
  --registry "$ACR_NAME" \
  --image "python-code-interpreter:$PYTHON_IMAGE_TAG" \
  --file aca_sandboxes/images/python/Dockerfile \
  aca_sandboxes/images/python/
```

private ACR image의 disk image 등록은
[실습 3C §6](03C_ACA_Sandboxes_Office_Admin_Lab.md#6-sandboxgroup에-disk-image-등록)과
같은 `RegistryCredentials` 절차를 사용하되 label을
`python-code-interpreter-$PYTHON_IMAGE_TAG`로 지정한다. Gateway와 Fast
Path는 Ready 상태의 최신 label을 자동 선택한다.

## 3. Python SDK 설치

ACA Sandboxes의 기본 인터페이스는 `azure-containerapps-sandbox` Python SDK다.
`az containerapp` CLI와 별개인 전용 `aca` CLI(`curl -fsSL https://aka.ms/aca-cli-install | sh`)도 있지만, 이 실습은 SDK를 사용한다.

Homebrew Python처럼 system package 설치가 제한된 환경을 고려해 virtual
environment를 사용한다.

```bash
python3 -m venv .work/aca-sandboxes/venv
source .work/aca-sandboxes/venv/bin/activate
python -m pip install \
  "azure-containerapps-sandbox==0.1.0b4" \
  azure-identity
```

버전을 확인한다.

```bash
python -c "import azure.containerapps.sandbox as sb; print(sb.VERSION)"
```

> SDK는 Preview 상태이므로 버전이 올라가면 API가 바뀔 수 있다. 현재 검증된 버전은 `0.1.0b4`다.

## 4. CLI와 provider 준비

```bash
az extension add \
  --name containerapp \
  --upgrade \
  --allow-preview true \
  --yes

az provider register \
  --namespace Microsoft.App \
  --wait
```

ACA Sandboxes는 `Microsoft.App` namespace에 속한다.

## 5. Resource Group 생성

```bash
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --tags purpose=ai-workspace-sandbox-lab \
  --output table
```

이미 있는 경우 `--output table`은 기존 Resource Group 정보를 반환한다.

## 6. SandboxGroup 생성(ARM 제어 평면)

SandboxGroup은 모든 Sandbox, disk image, snapshot, volume, secret의 상위 관리 경계다.
ARM 리소스이므로 `management.azure.com`을 통해 생성한다.

```python
# 6-create-group.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupManagementClient

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")

credential = AzureCliCredential()
mgmt = SandboxGroupManagementClient(
    credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
)

group = mgmt.begin_create_group(
    SANDBOX_GROUP,
    location=LOCATION,
    tags={"purpose": "ai-workspace-sandboxes-live-validation"},
).result()
print(f"SandboxGroup created: {group.id}")
print(f"  location : {group.location}")
print(f"  state    : {group.properties.get('provisioningState', 'n/a')}")
mgmt.close()
```

```bash
python3 6-create-group.py
```

또는 aca CLI를 사용할 수 있다.

```bash
# aca CLI 대안 (설치 필요: curl -fsSL https://aka.ms/aca-cli-install | sh)
aca sandboxgroup create \
  --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$RESOURCE_GROUP" \
  --name "$SANDBOX_GROUP_NAME" \
  --location "$LOCATION" \
  --set-config
```

## 7. RBAC - SandboxGroup Data Owner 역할

Sandbox를 생성·관리하려면 `Container Apps SandboxGroup Data Owner` 역할이 필요하다.
이 역할은 SandboxGroup data plane에서 Sandbox와 관련 객체를 관리한다.

```bash
SANDBOX_GROUP_ID=$(az resource show \
  --resource-group "$RESOURCE_GROUP" \
  --resource-type "Microsoft.App/SandboxGroups" \
  --name "$SANDBOX_GROUP_NAME" \
  --query id --output tsv 2>/dev/null \
  || echo "")

# SandboxGroup ARM ID를 찾지 못하면 Resource Group 범위로 할당
RBAC_SCOPE=${SANDBOX_GROUP_ID:-"/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP"}

if [[ -z "${CALLER_OBJECT_ID:-}" ]]; then
  CALLER_OBJECT_ID=$(az ad signed-in-user show --query id --output tsv)
  CALLER_PRINCIPAL_TYPE="User"
fi
CALLER_PRINCIPAL_TYPE="${CALLER_PRINCIPAL_TYPE:-User}"

az role assignment create \
  --role "Container Apps SandboxGroup Data Owner" \
  --assignee-object-id "$CALLER_OBJECT_ID" \
  --assignee-principal-type "$CALLER_PRINCIPAL_TYPE" \
  --scope "$RBAC_SCOPE" \
  --output table
```

역할 할당을 확인한다.

```bash
az role assignment list \
  --assignee "$CALLER_OBJECT_ID" \
  --scope "$RBAC_SCOPE" \
  --query "[?contains(roleDefinitionName,'SandboxGroup')].{role:roleDefinitionName,scope:scope}" \
  --output table
```

역할 전파는 수 분 걸릴 수 있다.

## 8. Python SDK 연결(데이터 평면)

SandboxGroup 생성(ARM)과 Sandbox 조작(데이터 평면)은 다른 클라이언트를 사용한다.
데이터 평면 엔드포인트는 `https://management.<region>.azuredevcompute.io`다.

```python
# 8-connect.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")

credential = AzureCliCredential()
endpoint   = endpoint_for_region(LOCATION)
print(f"Data-plane endpoint: {endpoint}")

client = SandboxGroupClient(
    endpoint,
    credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)

# 공개 disk image 목록 확인
images = list(client.list_public_disk_images())
print(f"Public disk images ({len(images)}):")
for img in images:
    print(f"  {img.id:20s}  {img.name}")

client.close()
```

```bash
python3 8-connect.py
```

공개 image 목록을 확인하는 단계다. 아래 수동 실행은 기본적으로
`python-3.12` 공개 disk image를 사용하고, `PYTHON_SANDBOX_DISK_ID`가 있으면
custom Code Interpreter disk image를 사용한다.

## 9. Sandbox 생성과 첫 실행

Egress를 기본 차단(`default_action='Deny'`)하고 Sandbox를 생성한 뒤 Python 코드를 실행한다.

```python
# 9-create-sandbox.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import (
    SandboxGroupClient, endpoint_for_region,
    EgressPolicy,
)

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION),
    credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)

# HTTP와 비 HTTP 트래픽을 모두 검사하고 기본 차단한다.
deny_all_egress = EgressPolicy(
    default_action="Deny",
    traffic_inspection="Full",
)

print("Creating sandbox...")
disk_image_id = os.environ.get("PYTHON_SANDBOX_DISK_ID")
source = (
    {"disk": None, "disk_id": disk_image_id}
    if disk_image_id
    else {"disk": "python-3.12"}
)
sandbox_client = client.begin_create_sandbox(
    **source,
    cpu="1000m",        # M tier: 1 vCPU
    memory="2048Mi",    # M tier: 2 GB
    egress_policy=deny_all_egress,
    auto_suspend_seconds=1800,
    auto_suspend_mode="Memory",  # 메모리+디스크 전체 snapshot으로 중단
    labels={"lab": "sandboxes-03a"},
).result()

print(f"Sandbox created: {sandbox_client.sandbox_id}")
print(f"  state: {sandbox_client.get().state}")

# 첫 실행
result = sandbox_client.exec(
    "python3 -c 'print(\"AI Workspace Sandboxes validation passed\")'"
)
print(f"exit_code: {result.exit_code}")
print(f"stdout   : {result.stdout.strip()}")

# sandbox ID를 파일로 보관
with open(os.path.join(os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/python-manual"), "sandbox_id.txt"), "w") as f:
    f.write(sandbox_client.sandbox_id)

sandbox_client.close()
client.close()
```

`PYTHON_SANDBOX_DISK_ID`가 비어 있으면 공개 `python-3.12` image로 기본
실행만 검증한다. 자연어 분석 Gateway의 chart·dataframe 기능에는 custom
Code Interpreter disk image가 필요하다.

```bash
python3 9-create-sandbox.py
```

통과 기준:

- Sandbox가 `Running` 상태
- `exit_code: 0`
- `stdout`에 validation 문구 포함

> Sandbox 생성에는 image와 현재 capacity에 따라 수 초~수십 초가 걸릴 수 있다.
> `.result()`는 생성 완료까지 block한다.

## 10. 사전 설치 패키지 확인

`python-3.12` 공개 image 또는 지정한 custom image의 Python 환경에서
사용 가능한 라이브러리를 확인한다.

```python
# 10-check-packages.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/python-manual")

with open(os.path.join(WORK_DIR, "sandbox_id.txt")) as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

# Python 버전과 pip 라이브러리 확인
result = sb.exec(
    "python3 --version && pip3 list --format=columns 2>/dev/null | head -30"
)
print(result.stdout)

sb.close()
client.close()
```

```bash
python3 10-check-packages.py
```

> 공개 `python-3.12` image의 package 집합은 고정 계약으로 가정하지 않는다.
> 자연어 분석 Gateway에는 Fast Path가 준비하는 custom Code
> Interpreter disk image를 사용한다. Sandbox 안에서 `pip install`하기 위해
> egress를 열기보다 필요한 패키지를 image build 단계에서 고정한다.

## 11. 파일 쓰기와 분석 실행

Sandbox에 CSV 파일과 Python 분석 코드를 SDK `write_file()`로 작성한 뒤
`exec()`로 실행한다.

```python
# 11-write-and-exec.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/python-manual")

with open(os.path.join(WORK_DIR, "sandbox_id.txt")) as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

# 작업 디렉터리 생성
sb.mkdir("/work")

# CSV 파일 업로드
sales_csv = """\
month,product,amount
2026-01,Notebook,120
2026-01,Pen,80
2026-02,Notebook,150
2026-02,Pen,90
2026-03,Notebook,130
2026-03,Pen,110
"""
sb.write_file("/work/sales.csv", sales_csv)
print("sales.csv written")

# 분석 스크립트 업로드
analysis_py = """\
import csv
import json
from collections import defaultdict

monthly = defaultdict(float)
products = defaultdict(float)
with open("/work/sales.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        amount = float(row["amount"])
        monthly[row["month"]] += amount
        products[row["product"]] += amount

with open("/work/summary.json", "w", encoding="utf-8") as out:
    json.dump(
        {
            "monthly_sales": dict(sorted(monthly.items())),
            "top_products": sorted(products.items(), key=lambda x: x[1], reverse=True)[:5],
        },
        out, ensure_ascii=False, indent=2,
    )

for month, total in sorted(monthly.items()):
    print(f"{month}: {total}")
"""
sb.write_file("/work/analyze_sales.py", analysis_py)
print("analyze_sales.py written")

# 파일 목록 확인
listing = sb.list_files("/work")
print(f"\n/work contents: {[e.name for e in listing.entries]}")

# 분석 실행
result = sb.exec("python3 /work/analyze_sales.py")
print(f"\nAnalysis exit_code: {result.exit_code}")
print(f"stdout:\n{result.stdout.strip()}")
if result.stderr:
    print(f"stderr: {result.stderr[:200]}")

sb.close()
client.close()
```

```bash
python3 11-write-and-exec.py
```

통과 기준:

- `exit_code: 0`
- `stdout`에 월별 합계(`2026-01: 200.0`, `2026-02: 240.0`, `2026-03: 240.0`)가 있음
- `/work` 목록에 `sales.csv`, `analyze_sales.py`가 있음

## 12. 결과 파일 읽기와 검증

Sandbox 내부에서 생성된 `summary.json`을 읽고 SHA-256 해시를 확인한다.

```python
# 12-read-results.py
import hashlib
import json
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/python-manual")

with open(os.path.join(WORK_DIR, "sandbox_id.txt")) as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

raw = sb.read_file("/work/summary.json")
data = json.loads(raw)
sha256 = hashlib.sha256(raw).hexdigest()

print("summary.json:")
print(json.dumps(data, ensure_ascii=False, indent=2))
print(f"\nSHA-256: {sha256}")

# 검증
assert data["monthly_sales"]["2026-01"] == 200.0, "2026-01 합계 오류"
assert data["monthly_sales"]["2026-02"] == 240.0, "2026-02 합계 오류"
assert data["monthly_sales"]["2026-03"] == 240.0, "2026-03 합계 오류"
print("\n✓ 월별 합계 검증 통과")

# 로컬에 저장
local_path = os.path.join(WORK_DIR, "summary.json")
with open(local_path, "wb") as f:
    f.write(raw)
print(f"  → {local_path}")

sb.close()
client.close()
```

```bash
python3 12-read-results.py
```

## 13. Egress 차단 확인

`default_action='Deny'`가 외부 통신을 실제로 차단하는지 검증한다.
HTTP와 비 HTTP traffic을 full inspection하고 기본적으로 거부한다.

```python
# 13-egress.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/python-manual")

with open(os.path.join(WORK_DIR, "sandbox_id.txt")) as f:
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
    "python3 -c \""
    "import urllib.request\n"
    "try:\n"
    "    urllib.request.urlopen('https://example.com', timeout=5)\n"
    "    print('UNEXPECTED_EGRESS_ALLOWED')\n"
    "except Exception as e:\n"
    "    print('EGRESS_BLOCKED', type(e).__name__)\n"
    "\""
)
print(f"exit_code: {result.exit_code}")
print(f"stdout:    {result.stdout.strip()}")

assert "EGRESS_BLOCKED" in result.stdout, "EGRESS_BLOCKED가 없음"
assert "UNEXPECTED_EGRESS_ALLOWED" not in result.stdout, "외부 접근이 허용됨"
print("✓ Egress 차단 검증 통과")

# 현재 정책 확인
policy = sb.get_egress_policy()
print(f"\nEgress policy default_action: {policy.default_action}")

sb.close()
client.close()
```

```bash
python3 13-egress.py
```

### 선택적: 특정 도메인만 허용

Sandboxes는 domain, CIDR, protocol과 method 단위의 세분화된 허용 규칙을 지원한다.
예를 들어 특정 API만 허용하고 나머지를 차단하려면:

```python
from azure.containerapps.sandbox import EgressPolicy, EgressHostRule

allow_internal_only = EgressPolicy(
    default_action="Deny",
    host_rules=[
        EgressHostRule(host="internal-api.example.com", action="Allow"),
    ],
)
sb.set_egress_policy(allow_internal_only)
```

## 14. Sandbox 간 격리 확인

두 번째 Sandbox를 생성하고 첫 번째 Sandbox의 파일이 보이지 않는지 확인한다.

```python
# 14-isolation.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region, EgressPolicy

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/python-manual")

with open(os.path.join(WORK_DIR, "sandbox_id.txt")) as f:
    first_sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)

# 두 번째 Sandbox 생성
print("Creating second sandbox...")
second = client.begin_create_sandbox(
    disk="python-3.12",
    egress_policy=EgressPolicy(
        default_action="Deny",
        traffic_inspection="Full",
    ),
    labels={"lab": "sandboxes-03a-isolation"},
).result()
sb2 = second

# 두 번째 Sandbox에서 /work 디렉터리 확인 (첫 번째 Sandbox에서 생성)
result = sb2.exec("ls /work 2>/dev/null || echo 'DIR_NOT_FOUND'")
print(f"second sandbox /work: {result.stdout.strip()}")

# 두 번째 Sandbox에서 첫 번째 파일을 직접 읽으려 하면 실패
try:
    sb2.read_file("/work/sales.csv")
    print("UNEXPECTED: 파일 읽기 성공 - 격리 실패")
except Exception as e:
    print(f"✓ 격리 확인: {type(e).__name__}")

assert "DIR_NOT_FOUND" in result.stdout or result.stdout.strip() == "", \
    "두 번째 Sandbox에서 첫 번째 Sandbox의 /work가 보임 - 격리 실패"
print("✓ Sandbox 간 파일 격리 검증 통과")

# 두 번째 Sandbox 즉시 삭제
sb2.delete()
print("Second sandbox deleted")

sb2.close()
client.close()
```

```bash
python3 14-isolation.py
```

## 15. 오류 발생, 코드 수정, 재실행

존재하지 않는 열을 참조해 일부러 실패시키고, 오류를 분석해 코드를 수정하는 루프를 검증한다.

```python
# 15-error-retry.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/python-manual")

with open(os.path.join(WORK_DIR, "sandbox_id.txt")) as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

# 의도적으로 잘못된 열 이름 사용
bad_code = (
    "python3 -c \""
    "import csv\n"
    "with open('/work/sales.csv') as f:\n"
    "    print(sum(float(r['sales_amount']) for r in csv.DictReader(f)))\n"
    "\""
)
result = sb.exec(bad_code)
print(f"1차 실행 exit_code: {result.exit_code}")
print(f"stderr: {result.stderr[:200]}")
assert result.exit_code != 0, "첫 실행은 실패해야 함"
assert "KeyError" in result.stderr, "stderr에 KeyError가 없음"
print("✓ 1차 실행 실패 확인 (KeyError 예상)")

# 오류에서 열 이름 확인 후 수정 코드 실행
fixed_code = (
    "python3 -c \""
    "import csv\n"
    "with open('/work/sales.csv') as f:\n"
    "    rows = list(csv.DictReader(f))\n"
    "print('columns:', list(rows[0]))\n"
    "print('total:', sum(float(r['amount']) for r in rows))\n"
    "\""
)
result2 = sb.exec(fixed_code)
print(f"\n2차 실행 exit_code: {result2.exit_code}")
print(f"stdout: {result2.stdout.strip()}")
assert result2.exit_code == 0, "수정 코드 실행 실패"
assert "680.0" in result2.stdout, "합계가 680.0이 아님"
print("✓ 2차 실행 성공 (total: 680.0)")

# 상태 유지 확인: sales.csv가 여전히 /work에 있음 (재업로드 불필요)
listing = sb.list_files("/work")
names = [e.name for e in listing.entries]
assert "sales.csv" in names, "/work/sales.csv가 사라짐"
print("✓ 파일 상태 유지 확인 (재업로드 불필요)")

sb.close()
client.close()
```

```bash
python3 15-error-retry.py
```

통과 기준:

- 1차 실행 `exit_code != 0`, `stderr`에 `KeyError`
- 2차 실행 `exit_code == 0`, `total: 680.0`
- 같은 Sandbox에서 `/work/sales.csv`가 유지됨

> 같은 Sandbox ID를 사용하므로 작업 디렉터리 상태가 수정 실행에서도
> 그대로 유지된다.

## 16. Suspend와 Resume (일시 중단과 재개)

Sandboxes는 메모리 또는 디스크 상태를 보존하고 이후 요청에서 재개할 수 있다.

```python
# 16-suspend-resume.py
import os, time
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/python-manual")

with open(os.path.join(WORK_DIR, "sandbox_id.txt")) as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

# 중단 전 상태 확인
info = sb.get()
print(f"Suspend 전 상태: {info.state}")

# 메모리 모드로 중단 (Memory suspend: 디스크 + 메모리 전체 snapshot)
print("Suspending sandbox (Memory mode)...")
sb.begin_stop().result()
info = sb.get()
print(f"Suspend 후 상태: {info.state}")
assert info.state in ("Stopped", "Stopping"), f"예상치 못한 상태: {info.state}"

# Resume: sub-second 재개
print("Resuming sandbox...")
sb.begin_resume().result()
info = sb.get()
print(f"Resume 후 상태: {info.state}")
assert info.state == "Running", f"재개 후 Running이 아님: {info.state}"

# 재개 후 파일 상태 확인
result = sb.exec("cat /work/summary.json | python3 -c \"import json,sys; d=json.load(sys.stdin); print('2026-01:', d['monthly_sales']['2026-01'])\"")
print(f"Resume 후 summary.json 확인: {result.stdout.strip()}")
assert "200.0" in result.stdout, "Resume 후 파일 내용이 바뀜"
print("✓ Suspend/Resume 검증 통과 - 상태 보존 확인")

sb.close()
client.close()
```

```bash
python3 16-suspend-resume.py
```

통과 기준:

- Suspend 후 상태가 `Stopped`
- Resume 후 상태가 `Running`
- Resume 후에도 `/work/summary.json` 내용 유지

## 17. Snapshot 생성 (선택)

Snapshot으로 현재 Sandbox 상태를 독립적으로 보관하고, 나중에 같은 상태로 새 Sandbox를 만들 수 있다.

```python
# 17-snapshot.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")
WORK_DIR        = os.environ.get("LAB_WORK_DIR", ".work/aca-sandboxes/python-manual")

with open(os.path.join(WORK_DIR, "sandbox_id.txt")) as f:
    sandbox_id = f.read().strip()

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)
sb = client.get_sandbox_client(sandbox_id)

# Snapshot 생성
snapshot = sb.begin_create_snapshot(name="lab-03a-checkpoint").result()
print(f"Snapshot created: {snapshot.id}")
print(f"  name  : {snapshot.name if hasattr(snapshot, 'name') else 'n/a'}")

# Snapshot 목록 확인
snapshots = list(client.list_snapshots())
print(f"Total snapshots: {len(snapshots)}")
for s in snapshots:
    print(f"  {s.id}")

# Snapshot에서 새 Sandbox 생성
print("\nCreating sandbox from snapshot...")
new_sb = client.begin_create_sandbox(
    disk=None,
    snapshot_id=snapshot.id,
    egress_policy=None,  # snapshot에서 복원하면 정책도 복원됨
    labels={"lab": "sandboxes-03a-snapshot-test"},
).result()
new_client = new_sb

# 복원된 Sandbox에서 파일 확인
result = new_client.exec("ls /work")
print(f"Snapshot 복원 후 /work: {result.stdout.strip()}")

# 검증 후 정리
new_client.delete()
print("Snapshot-based sandbox deleted")

# Snapshot 삭제 (lab 정리)
client.delete_snapshot(snapshot.id)
print("Snapshot deleted")

new_client.close()
sb.close()
client.close()
```

```bash
python3 17-snapshot.py
```

> Snapshot은 자동으로 삭제되지 않는다. 정기적으로 목록을 확인하고 불필요한 Snapshot을 삭제한다.

## 18. LLM Backend 구성 (선택)

실제 모델을 연결하기 전에 deterministic orchestration, 재시도, 정책 거부와
승인 gate를 Fast Path로 검증한다.

```bash
ACA_EXECUTION_TIMEOUT_SECONDS=900 LLM_PROVIDER=stub \
  bash scripts/aca-sandboxes/agent-lab.sh
```

Azure OpenAI endpoint와 deployment를 설정하고 ACA Sandboxes 전용
entrypoint를 실행한다. Credential과 Sandbox ID는 backend에서만 관리한다.

```bash
export SANDBOX_GROUP_NAME="ai-workspace-sandboxes"
export LLM_PROVIDER="azure-openai"
export AZURE_OPENAI_ENDPOINT="https://<your-endpoint>.openai.azure.com/"
export AZURE_OPENAI_DEPLOYMENT="<your-deployment>"
export RESOURCE_GROUP="rg-ai-workspace-aca-sandboxes-lab"
export LOCATION="koreacentral"
export ACA_PYTHON="${ACA_PYTHON:-.work/aca-sandboxes/venv/bin/python}"
export ACA_EXECUTION_TIMEOUT_SECONDS="${ACA_EXECUTION_TIMEOUT_SECONDS:-900}"

"$ACA_PYTHON" -m aca_sandboxes.cli
```

`ACA_EXECUTION_TIMEOUT_SECONDS=900`은 platform 보장값이 아니라 reference
application이 각 `exec` request에 적용하는 limit이다.

Python 사용자 Gateway는 `$ACA_PYTHON -m aca_sandboxes.python_gateway`로
실행한다. Backend는 Sandbox ID와 SDK file I/O를 사용자 API에서 숨긴다.

## 19. 정리

Fast Path로 만든 모든 Sandbox, snapshot, disk image와 SandboxGroup을
삭제하려면 repository root에서 다음 명령을 실행한다.

```bash
export ACA_PYTHON="${ACA_PYTHON:-.work/aca-sandboxes/venv/bin/python}"
CONFIRM_DELETE=yes "$ACA_PYTHON" scripts/aca-sandboxes/cleanup.py
```

아래 코드는 같은 작업을 SDK 단계별로 학습하기 위한 수동 경로다.

```python
# 19-cleanup.py
import os
from azure.identity import AzureCliCredential
from azure.containerapps.sandbox import SandboxGroupClient, SandboxGroupManagementClient, endpoint_for_region

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP  = os.environ.get("RESOURCE_GROUP", "rg-ai-workspace-aca-sandboxes-lab")
LOCATION        = os.environ.get("LOCATION", "koreacentral")
SANDBOX_GROUP   = os.environ.get("SANDBOX_GROUP_NAME", "ai-workspace-sandboxes")

credential = AzureCliCredential()
client = SandboxGroupClient(
    endpoint_for_region(LOCATION), credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
    sandbox_group=SANDBOX_GROUP,
)

# 모든 Sandbox 삭제
sandboxes = list(client.list_sandboxes())
print(f"Deleting {len(sandboxes)} sandbox(es)...")
for sb_info in sandboxes:
    sb_client = client.get_sandbox_client(sb_info.id)
    sb_client.delete()
    print(f"  Deleted: {sb_info.id}")
    sb_client.close()

client.close()

# SandboxGroup 삭제
mgmt = SandboxGroupManagementClient(
    credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group=RESOURCE_GROUP,
)
mgmt.delete_group(SANDBOX_GROUP)
print(f"SandboxGroup '{SANDBOX_GROUP}' deleted")
mgmt.close()
```

```bash
"${ACA_PYTHON:-.work/aca-sandboxes/venv/bin/python}" 19-cleanup.py
```

SandboxGroup을 삭제해도 Resource Group, Log Analytics, ACR은 남는다.
Resource Group 자체를 삭제하려면 별도로 확인한 뒤 수행한다.

```bash
export RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ai-workspace-aca-sandboxes-lab}"
az group show --name "$RESOURCE_GROUP" \
  --query '{name:name,location:location,id:id}' --output table
az group delete --name "$RESOURCE_GROUP" --yes --no-wait
```
