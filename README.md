# Azure Container Apps 기반 AI Workspace Sandbox 실습

이 repository는 AI Workspace가 신뢰할 수 없는 Python 및 Office 작업을
Azure Container Apps의 격리 환경에서 실행하고, 생성된 artifact를 검사한 뒤,
별도 Connector가 업무 시스템을 변경하기 전에 명시적 승인을 요구하는 방법을
보여준다. Dynamic Sessions와 ACA Sandboxes는 서로 독립된 경로로 문서화하고
운영하며, workload별로 하나를 선택해 해당 architecture와 실습을 따른다.

## 서비스 선택

| 기준 | Dynamic Sessions | ACA Sandboxes (Preview) |
| --- | --- | --- |
| 적합한 workload | 짧고 일시적인 코드 또는 문서 작업 | 상태를 유지하는 Agent workspace와 개발 환경 |
| Lifecycle | Pool이 session을 할당하고 만료 처리 | Application이 Sandbox 생성, suspend, resume, snapshot, 삭제 관리 |
| 시작 및 resume | Prewarmed 할당. 전체 요청 latency는 별도 측정 | Prewarmed 생성 및 memory resume. Image boot와 staging도 측정 |
| 상태 및 storage | 임시 session 파일 | Memory/disk suspend, snapshot, Blob 및 Data Disk volume |
| Network | Egress on/off, Custom Pool의 VNet | Domain, CIDR, VNet 및 ingress policy |
| Compute 선택 | Managed Interpreter 또는 Custom Container Pool | Sandbox별 XS~XL tier |
| 비용 구조 | Interpreter session-hour 또는 전용 Custom Pool capacity | 실행 중 vCPU/GiB-second, suspend 시 compute 중단, 보존 storage는 계속 과금 |
| 운영 부담 | 낮음. Pool이 할당과 만료 관리 | 높음. Application이 ID, lifecycle, state, orphan 정리 관리 |

성능과 가격은 region, image, quota 및 시점에 따라 달라진다. Production
도입 전 end-to-end p95를 측정하고 현재 Azure meter를 확인한다.

## 빠른 시작: Dynamic Sessions

Repository root에서 실행한다.

```bash
export RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ai-workspace-dynamic-sessions-lab}"
bash scripts/common/check-prereqs.sh
bash scripts/common/validate-repo.sh
bash scripts/dynamic-sessions/python-lab.sh
```

Office 도구 또는 Agent orchestration이 필요할 때만 추가한다.

```bash
bash scripts/dynamic-sessions/office-lab.sh
bash scripts/dynamic-sessions/agent-lab.sh
```

[Dynamic Sessions 실습 index](labs/dynamic-sessions/01_Python_Code_Interpreter_Lab.md)와
[Reference Architecture](docs/dynamic-sessions/architecture.md)를 이어서 확인한다.

## 빠른 시작: ACA Sandboxes

Repository root에서 실행한다.

```bash
export RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ai-workspace-aca-sandboxes-lab}"
bash scripts/common/check-prereqs.sh
bash scripts/common/validate-repo.sh
bash scripts/aca-sandboxes/quickstart.sh python
export ACA_PYTHON="${ACA_PYTHON:-.work/aca-sandboxes/venv/bin/python}"
```

필요한 image에 따라 `python` 대신 `office` 또는 `all`을 사용한다.
[ACA Sandboxes 실습 index](labs/aca-sandboxes/03_ACA_Sandboxes_Lab.md)와
[Reference Architecture](docs/aca-sandboxes/architecture.md)를 이어서 확인한다.

## Repository 구성

| 경로 | 내용 |
| --- | --- |
| [`docs/dynamic-sessions/`](docs/dynamic-sessions/) | Dynamic Sessions architecture와 과거 검증 기록 |
| [`docs/aca-sandboxes/`](docs/aca-sandboxes/) | ACA Sandboxes architecture와 과거 검증 기록 |
| [`labs/dynamic-sessions/`](labs/dynamic-sessions/) | Python Code Interpreter 및 Office Custom Container 실습 |
| [`labs/aca-sandboxes/`](labs/aca-sandboxes/) | Python, Office, lifecycle, state 및 network 실습 |
| `dynamic_sessions/` | Dynamic Sessions application module과 Office image |
| `aca_sandboxes/` | ACA Sandboxes module과 Python/Office image |
| `scripts/common/` | 공통 사전 조건 및 repository 검증 명령 |
| `scripts/dynamic-sessions/` | Dynamic Sessions 자동화와 정리 |
| `scripts/aca-sandboxes/` | ACA Sandboxes 자동화와 정리 |
| `tests/` | Policy, artifact, approval 및 Gateway offline test |

과거 검증 근거는
[Dynamic Sessions 검증 기록](docs/dynamic-sessions/validation.md)과
[ACA Sandboxes 검증 기록](docs/aca-sandboxes/validation.md)에 보관한다.

## 공통 안전 원칙

- Model 생성 작업보다 deterministic policy를 먼저 실행한다.
- Token, endpoint, session 또는 Sandbox ID, production credential을 prompt,
  browser, artifact 및 사용자에게 보이는 log에 포함하지 않는다.
- Outbound access는 기본 거부하고, 범위와 만료가 명확한 예외만 허용한다.
- Source scanning은 isolation, resource limit 및 credential 분리의 보조
  수단으로만 사용한다.
- 입력을 quarantine하고 실제 형식, 크기, archive, malware 및 tenant
  ownership을 검증한다.
- 출력을 staging하고 형식과 SHA-256을 검증하며 안전한 preview 또는 Diff를
  만든 뒤 명시적으로 승인한다.
- 최소 권한 Connector가 승격하기 직전에 artifact hash를 다시 검증한다.
- Concurrency, 실행 시간, retry, storage, lifecycle 및 비용을 제한한다.
- 성공, 실패, 취소 및 process restart 모두에서 정리한다.

## 정리

삭제 전에 대상 리소스를 확인한다.

- Dynamic Sessions:
  `RESOURCE_GROUP=rg-ai-workspace-dynamic-sessions-lab CONFIRM_DELETE=yes bash scripts/dynamic-sessions/cleanup.sh`
  — [Python 정리](labs/dynamic-sessions/01A_Python_Code_Interpreter_Admin_Lab.md#17-정리),
  [Office 정리](labs/dynamic-sessions/02A_Office_Custom_Container_Admin_Lab.md#17-정리)
- ACA Sandboxes:
  `export ACA_PYTHON="${ACA_PYTHON:-.work/aca-sandboxes/venv/bin/python}"`,
  이후 `RESOURCE_GROUP=rg-ai-workspace-aca-sandboxes-lab CONFIRM_DELETE=yes $ACA_PYTHON scripts/aca-sandboxes/cleanup.py`
  — [Sandbox 정리](labs/aca-sandboxes/03A_ACA_Sandboxes_Admin_Lab.md#19-정리),
  [Office 정리](labs/aca-sandboxes/03C_ACA_Sandboxes_Office_Admin_Lab.md#14-정리)
