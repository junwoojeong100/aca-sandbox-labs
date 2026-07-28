# 실습 3: ACA Sandboxes (Public Preview)로 격리 코드 실행과 Office 문서 작업

이 실습은 실습 1(Python Code Interpreter)과 실습 2(Office Custom Container)의 시나리오를 모두 ACA Sandboxes로 구현한다.

## 공식 포털과 문서

| 링크 | 용도 |
| --- | --- |
| [ACA Sandboxes 포털](https://sandboxes.azure.com/) | SandboxGroup 안의 개별 Sandbox, disk image, snapshot, volume 확인·관리 |
| [ACA Sandboxes 문서](https://sandboxes.azure.com/docs/sandboxes/) | Sandboxes 개념, CLI·SDK, lifecycle, image와 네트워크 문서 |
| [Microsoft Learn 개요](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) | Azure Container Apps Sandboxes Public Preview 개요와 Dynamic Sessions 비교 |

일반 Azure Portal의 Resource Group 목록에는 ARM 리소스인 SandboxGroup과 ACR이
보인다. 개별 Sandbox와 disk image는 data-plane 객체이므로 ACA Sandboxes
포털에서 확인한다.

## 실습 구성

| 실습 | 대상 | 대응 Dynamic Sessions 실습 |
| --- | --- | --- |
| **3A** (관리자) | [Python 코드 실행 - 관리자](03A_ACA_Sandboxes_Admin_Lab.md) | 실습 1A |
| **3B** (사용자) | [Python 분석 - 사용자](03B_ACA_Sandboxes_User_Lab.md) | 실습 1B |
| **3C** (관리자) | [Office Custom Image - 관리자](03C_ACA_Sandboxes_Office_Admin_Lab.md) | 실습 2A |
| **3D** (사용자) | [Office 생성·변환·편집 - 사용자](03D_ACA_Sandboxes_Office_User_Lab.md) | 실습 2B |

## Dynamic Sessions와의 관계

두 방식을 나란히 구성할 수 있지만, 현재 실습 Resource Group에서는 비용
절감을 위해 Dynamic Sessions 리소스를 삭제하고 ACA Sandboxes만 유지한다.

| 항목 | Dynamic Sessions (실습 1·2) | ACA Sandboxes (실습 3) |
| --- | --- | --- |
| **리소스 형식** | `Microsoft.App/sessionPools` | `Microsoft.App/SandboxGroups` |
| **관리 CLI** | `az containerapp sessionpool` | `aca sandboxgroup` / Python SDK |
| **Python 코드 실행** | REST API `POST /executions` | SDK `sandbox.exec(command)` |
| **Office 실행** | container 내 HTTP 서버 `POST /generate` | SDK `sandbox.exec("libreoffice ...")` |
| **파일 I/O** | `POST /files`, `GET /files/{name}/content` | SDK `write_file()`, `read_file()` |
| **Custom Image** | `az containerapp sessionpool create --custom-container-image` | `client.begin_create_disk_image(base_image=...)` |
| **Egress 제어** | `--network-status EgressDisabled` (이진) | `EgressPolicy(default_action='Deny', traffic_inspection='Full')` (세분화) |
| **RBAC 역할** | `Azure ContainerApps Session Executor` | `Container Apps SandboxGroup Data Owner` |
| **상태** | 일시적(cooldown 후 삭제) | 상태 유지(suspend/resume, snapshot) |
| **HTTP 서버(Office)** | container 안에 필요 | 불필요(exec으로 직접 호출) |
| **스토리지** | 없음 | Azure Blob, Data Disk 볼륨 |

### 선택 기준

| 조건 | 추천 |
| --- | --- |
| LLM 생성 코드를 관리되는 pool에서 실행 | Dynamic Sessions |
| 세션 간 상태 유지, suspend/resume 필요 | ACA Sandboxes |
| Egress를 도메인·메서드 단위로 세밀하게 제어 | ACA Sandboxes |
| 사용자별 영구 격리 workspace | ACA Sandboxes |
| Office 실행에 HTTP 서버 계층을 없애고 싶음 | ACA Sandboxes |
| 기존 `az containerapp sessionpool` workflow 유지 | Dynamic Sessions |

## 권장 순서

**Python 코드 실행:**
1. 관리자가 [실습 3A](03A_ACA_Sandboxes_Admin_Lab.md)로 SandboxGroup과 Python 환경을 준비한다.
2. [실습 3B](03B_ACA_Sandboxes_User_Lab.md)로 Sandboxes 전용 Python
   Gateway의 자연어 분석·다운로드·승인 흐름을 검증한다.

**Office Custom Image:**
1. 관리자가 [실습 3C](03C_ACA_Sandboxes_Office_Admin_Lab.md)로 ACR 이미지를 disk image로 등록하고 Office Sandbox를 구성한다.
2. [실습 3D](03D_ACA_Sandboxes_Office_User_Lab.md)로 Sandboxes 전용 Office
   Gateway의 생성·변환·편집·다운로드·승인 흐름을 검증한다.

## 새 환경 Quick Start

repository root에서 다음 중 하나만 실행한다. Quick Start는
`bash scripts/check-prereqs.sh`, 전용 virtual environment와 검증된 Preview
SDK 설치, SandboxGroup·RBAC 준비를 포함한다. Ready custom disk image가
없으면 ACR을 만들고 image를 build·등록한다.

```bash
bash scripts/sandboxes-quickstart.sh python # Python 코드 실행
bash scripts/sandboxes-quickstart.sh office # Office Custom Image
bash scripts/sandboxes-quickstart.sh all    # 두 경로 모두
```

기존 환경에서는 Ready disk image와 Azure 리소스를 재사용한다. 수동으로
각 Azure 명령과 SDK 호출을 학습하거나 실패 원인을 추적할 때만 3A·3C의
세부 절차를 수행한다.

> 실제 LLM 호출은 실습 3A §18절에서 LLM backend 구성 후 실습 3B에서 수행한다.
