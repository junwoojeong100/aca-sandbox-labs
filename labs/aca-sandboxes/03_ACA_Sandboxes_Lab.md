# 실습 3: ACA Sandboxes 격리 실행과 Office 작업

이 실습은 ACA Sandboxes에서 Python 분석과 Office 문서 작업을 구성하고,
Sandbox lifecycle, state, network policy, artifact 검사와 승인을 검증한다.

## 공식 포털과 문서

| 링크 | 용도 |
| --- | --- |
| [ACA Sandboxes 포털](https://sandboxes.azure.com/) | SandboxGroup의 Sandbox, disk image, snapshot, volume 관리 |
| [ACA Sandboxes 개발 문서](https://sandboxes.azure.com/docs/sandboxes/) | SDK, lifecycle, image, storage와 network |
| [Microsoft Learn 개요](https://learn.microsoft.com/azure/container-apps/sandboxes-overview) | 서비스 개요와 지원 범위 |

개별 Sandbox와 disk image는 data-plane 객체다. Azure Portal의 Resource
Group에는 상위 SandboxGroup이 보이며, 세부 객체는 전용 포털이나 SDK에서
확인한다.

## 실습 구성

| 실습 | 대상 | 수행 내용 |
| --- | --- | --- |
| **3A** | [Python 코드 실행 - 관리자](03A_ACA_Sandboxes_Admin_Lab.md) | SandboxGroup, RBAC, Python image, 파일 I/O, 격리, egress, suspend/resume, snapshot |
| **3B** | [Python 분석 - 사용자 흐름](03B_ACA_Sandboxes_User_Lab.md) | 자연어 요청, 첨부, artifact, 승인, lifecycle |
| **3C** | [Office Custom Image - 관리자](03C_ACA_Sandboxes_Office_Admin_Lab.md) | Office image, 생성, 변환, 편집, egress, suspend/resume |
| **3D** | [Office 작업 - 사용자 흐름](03D_ACA_Sandboxes_Office_User_Lab.md) | 문서 생성, 변환, 편집, 검토와 승인 |

## 권장 순서

Python 경로:

1. 관리자가 [실습 3A](03A_ACA_Sandboxes_Admin_Lab.md)로 SandboxGroup과
   Python disk image를 준비한다.
2. 실습 운영자가 [실습 3B](03B_ACA_Sandboxes_User_Lab.md)로 사용자 API
   흐름을 검증한다.

Office 경로:

1. 관리자가 [실습 3C](03C_ACA_Sandboxes_Office_Admin_Lab.md)로 Office
   disk image를 준비한다.
2. 실습 운영자가 [실습 3D](03D_ACA_Sandboxes_Office_User_Lab.md)로 문서
   작업과 승인 흐름을 검증한다.

## 새 환경 Fast Path

repository root에서 실행한다.

```bash
bash scripts/common/check-prereqs.sh
bash scripts/aca-sandboxes/quickstart.sh python # Python
bash scripts/aca-sandboxes/quickstart.sh office # Office
bash scripts/aca-sandboxes/quickstart.sh all    # 두 image 모두
```

Quick Start는 전용 virtual environment와 검증된 Preview SDK를 준비하고,
SandboxGroup·RBAC·ACR·disk image를 생성 또는 재사용한다. 활성 검증
Sandbox는 종료 시 삭제하지만 SandboxGroup과 Ready disk image는 재사용을
위해 남긴다.

Quick Start 이후 특정 검증만 다시 실행할 때:

```bash
export ACA_PYTHON="${ACA_PYTHON:-.work/aca-sandboxes/venv/bin/python}"
"$ACA_PYTHON" scripts/aca-sandboxes/python-lab.py
"$ACA_PYTHON" scripts/aca-sandboxes/office-lab.py
bash scripts/aca-sandboxes/agent-lab.sh
```

Image build와 disk image 변환을 포함한 첫 실행은 10~20분 이상 걸릴 수 있다.
플랫폼 할당 시간과 사용자 관점의 end-to-end 시간은 별도로 측정한다.

실행 중 Sandbox는 vCPU와 memory 사용 시간에 따라 과금된다. 중지 또는
suspend된 compute 비용이 없어도 snapshot, volume, ACR, 로그와 다른 Azure
리소스 비용은 남을 수 있다. 비용과 운영 기준은
[ACA Sandboxes Architecture](../../docs/aca-sandboxes/architecture.md#비용)를
따르고, 실습 후 [3A 정리](03A_ACA_Sandboxes_Admin_Lab.md#19-정리)를
검토한다.

> 실제 LLM 호출은 실습 3A의 backend 설정 후 실습 3B에서 수행한다.
