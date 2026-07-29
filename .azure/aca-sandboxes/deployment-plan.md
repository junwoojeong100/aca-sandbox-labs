# ACA Sandboxes 배포 계획

## 범위

- SandboxGroup 하나를 생성하거나 기존 Group을 재사용한다.
- 독립된 Python 및 Office disk image를 build하고 등록한다.
- 실행, egress, isolation, suspend/resume, artifact 및 cleanup을 검증한다.

## 명령

```bash
export RESOURCE_GROUP="${RESOURCE_GROUP:-rg-ai-workspace-aca-sandboxes-lab}"
export ACA_EXECUTION_TIMEOUT_SECONDS="${ACA_EXECUTION_TIMEOUT_SECONDS:-900}"
bash scripts/aca-sandboxes/quickstart.sh python
bash scripts/aca-sandboxes/quickstart.sh office
bash scripts/aca-sandboxes/agent-lab.sh
```

`ACA_EXECUTION_TIMEOUT_SECONDS=900`은 platform timeout이 아니라 reference
application이 `exec` request에 적용하는 limit이다.

## 비용 통제

- Running Sandbox는 vCPU와 memory를 초 단위로 과금한다.
- Stop 또는 suspend된 Sandbox에는 compute 비용이 없다.
- Disk image, snapshot, volume 및 ACR storage에는 명시적 retention이 필요하다.
- Auto-suspend, auto-delete, concurrency limit 및 orphan cleanup을 사용한다.

## 정리

```bash
RESOURCE_GROUP="rg-ai-workspace-aca-sandboxes-lab" \
CONFIRM_DELETE=yes \
.work/aca-sandboxes/venv/bin/python \
  scripts/aca-sandboxes/cleanup.py
```

## 검증 근거

실제 검증 결과는 `docs/aca-sandboxes/validation.md`에 기록한다.

## 2026-07-29 실제 검증

- 전용 Resource Group과 SandboxGroup 생성
- `Container Apps SandboxGroup Data Owner` RBAC 확인
- Python 및 Office ACR cloud build와 disk image 등록 성공
- Python 실행·file I/O·egress·isolation·suspend/resume 성공
- Office 생성·변환·편집·egress·suspend/resume 성공
- Python·Office 사용자 Gateway 전체 REST 흐름 성공
- Agent policy·retry·approval·cleanup 성공
- 검증 종료 시 active Sandbox 0건, snapshot 0건
