# 실습 1: Python Code Interpreter와 LLM 코드 실행

이 실습은 역할에 따라 두 문서로 나뉜다.

| 역할 | 가이드 | 수행 내용 |
| --- | --- | --- |
| 관리자 | [실습 1A](01A_Python_Code_Interpreter_Admin_Lab.md) | Python pool, RBAC, 격리·한도, Agent backend와 실제 LLM 연결 구성 |
| 실습 운영자 | [실습 1B](01B_Python_Code_Interpreter_User_Lab.md) | 최종 사용자 경험을 REST API로 검증: 자연어 요청, 파일 첨부, LLM 실행, 결과 검토와 승인 |

## 권장 순서

1. 관리자가 실습 1A를 완료해 Python pool과 LLM backend를 준비한다.
2. 실습 운영자가 실습 1B에서 최종 사용자 관점의 자연어 분석 API 흐름을 검증한다.
3. Office 생성·변환·편집이 필요할 때만 [실습 2](02_Office_Custom_Container_Lab.md)를 수행한다.

빠른 자동 검증:

```bash
bash scripts/check-prereqs.sh
bash scripts/python-lab.sh
bash scripts/agent-lab.sh
```

`python-lab.sh`는 Sandbox 기반을 검증하고, `agent-lab.sh`는 정책·재시도·검사·승인 게이트를 검증한다. 실제 LLM 호출은 실습 1A에서 `LLM_PROVIDER=azure-openai` backend를 구성한 뒤 실습 1B에서 수행한다.
