# Dynamic Sessions 배포 계획

## 범위

- Python Code Interpreter Session Pool을 배포하거나 기존 Pool을 재사용한다.
- 필요한 경우 Office Custom Container Session Pool을 배포한다.
- RBAC, quota, egress, isolation, artifact, monitoring 및 cleanup을 검증한다.

## 명령

```bash
bash scripts/common/check-prereqs.sh
bash scripts/dynamic-sessions/python-lab.sh
bash scripts/dynamic-sessions/office-lab.sh
bash scripts/dynamic-sessions/agent-lab.sh
```

## 비용 통제

- Code Interpreter session은 할당된 동안 1시간 단위로 과금된다.
- Office Custom Container Pool은 ready 및 active session에 Dedicated E16
  capacity를 사용한다.
- `max-sessions`와 `ready-sessions`는 검증된 최소값으로 유지한다.
- ACR, Container Apps Environment 및 Log Analytics는 Pool 삭제 후에도 남는다.

## 정리

```bash
RESOURCE_GROUP="rg-ai-workspace-dynamic-sessions-lab" \
CONFIRM_DELETE=yes \
bash scripts/dynamic-sessions/cleanup.sh
```

## 검증 근거

실제 검증 결과는 `docs/dynamic-sessions/validation.md`에 기록한다.

## 2026-07-29 실제 검증

- 전용 Resource Group에서 Python 및 Office Session Pool 생성
- Python 분석·egress·isolation·retry·cleanup 성공
- Office 생성·변환·편집·probe·log·metric·cleanup 성공
- Python·Office 사용자 Gateway 전체 REST 흐름 성공
- Agent policy·retry·approval·cleanup 성공
- Office Pool 최종 metric ready 1, executing 0, pending 0
- 테스트 종료 후 전용 Resource Group 전체 삭제와 ACR 부재 확인
