# AI Workspace 격리형 Sandbox on Azure Container Apps Dynamic Sessions

AI Workspace가 사용자의 자연어 요청과 첨부파일을 받아 코드를 생성·실행하고, 데이터 분석 및 Office 문서 생성 작업을 격리된 환경에서 수행한 뒤 **검사와 사용자 승인 후에만** 실제 업무 시스템에 반영하기 위한 권장 아키텍처와 실습 자료다.

## 문서 구성

| 문서 | 용도 |
| --- | --- |
| [AI Workspace 권장 아키텍처](docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md) | Azure 권장 구조, 보안·격리, 세션 및 리소스 운영, 모니터링, 제약사항과 운영 사례 |
| [실습 1: Python Code Interpreter](labs/01_Python_Code_Interpreter_Lab.md) | Python 코드 실행, CSV 분석, 차트·파일 생성, egress 차단 및 세션 lifecycle 검증 |
| [실습 2: Office Custom Container](labs/02_Office_Custom_Container_Lab.md) | ACR 이미지, Managed Identity, Log Analytics, Custom Container pool, DOCX/PDF/PPTX/XLSX 생성 검증 |
| [Office 이미지 소스](office-container/) | LibreOffice, Pandoc, Poppler, OpenPyXL, python-pptx를 포함한 비루트 HTTP 변환 서비스 |
| [자동 실행 스크립트](scripts/) | 사전 조건 검사, Python·Office 배포 및 검증, 명시적 전체 정리 |

## 지원하는 AI Workspace 시나리오

- 자연어 요청에 따른 Python 코드 생성·실행과 결과 반환
- 실행 오류 분석, 제한된 코드 수정과 재실행
- 데이터 분석, 계산, 차트 및 결과 파일 생성
- 첨부파일을 사용한 분석·가공
- DOCX, XLSX, PPTX, PDF 생성·편집·변환
- 사용자 또는 요청 단위의 독립 세션과 임시 파일 공간
- 작업 완료 또는 session lifecycle 종료 시 환경과 파일 자동 정리
- 검사, 미리보기, Diff와 사용자 승인 후 실제 업무 시스템 반영

## 권장 기준선

```text
AI Workspace 사용자
  -> AI Workspace Agent / 정책 엔진
  -> Session Broker
       -> Python Code Interpreter Pool
       -> Office Custom Container Pool
  -> 격리된 임시 산출물 저장소
  -> Malware / DLP / 형식 검사 / 미리보기 / Diff
  -> 사용자 승인
  -> 승인된 Connector
  -> 실제 업무 시스템
```

- 기본 네트워크 정책: `EgressDisabled`
- session identifier와 Entra token: AI Workspace 백엔드만 관리
- Sandbox 내부: 프로덕션 credential 및 직접 쓰기 권한 금지
- Office image pull identity와 runtime resource identity 분리
- Custom Container: Startup/Liveness probe와 비루트 실행
- 최종 반영: Sandbox와 분리된 Approval Service만 수행

## 실제 검증 상태

2026-07-24 한국 중부 리전에서 다음 항목을 실제 검증했다.

| 항목 | 결과 |
| --- | --- |
| Python Code Interpreter 실행 | HTTP 200, `Succeeded` |
| CSV 업로드 및 분석 | 성공 |
| PNG·JSON 산출물 생성과 다운로드 | 성공, SHA-256 확인 |
| Python pool 외부 통신 차단 | `EGRESS_BLOCKED` 확인 |
| Office Custom Container `/health` | HTTP 200 |
| LibreOffice, Pandoc, Poppler | 컨테이너 내부 버전 확인 |
| DOCX·PDF·PPTX·XLSX 생성과 다운로드 | 성공, 파일 형식과 SHA-256 확인 |
| Custom Container Startup/Liveness probe | pool 구성 반영 확인 |
| Log Analytics와 pool metrics | console log와 ready/executing/pending metric 확인 |
| Session identifier log 비노출 | Log Analytics 검색 결과 0건 |
| Session pool 수 | Python 1개 + Office 1개 |
| Clean 재배포 | 자동 스크립트로 기존 그룹 삭제 후 두 pool 재생성 성공 |
| Local container smoke | 네 형식, hash, 오류 응답과 log sanitization 통과 |

> Custom Container pool은 최소 1개의 ready session이 필요하므로 유지 비용이 발생할 수 있다. 실습 종료 후 각 실습 문서의 정리 절차를 검토한다.

## 시작 순서

1. [권장 아키텍처](docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md)에서 trust boundary와 승인 경계를 결정한다.
2. `bash scripts/check-prereqs.sh`로 로컬 도구와 Azure 로그인을 확인한다.
3. `bash scripts/python-lab.sh`로 기본 격리 실행 경로를 검증한다.
4. `bash scripts/office-lab.sh`로 Custom Container 경로를 검증한다.
5. 세부 명령과 문제 해결은 [Python 실습](labs/01_Python_Code_Interpreter_Lab.md)과 [Office 실습](labs/02_Office_Custom_Container_Lab.md)을 참고한다.
6. 두 pool을 AI Workspace Session Broker의 정책 기반 라우팅과 연결한다.
7. 산출물 검사, 승인 서비스와 최소 권한 Connector를 별도 구현한다.

자동 스크립트는 기본적으로 리소스를 삭제하지 않는다. 전체 삭제는 `CONFIRM_DELETE=yes bash scripts/cleanup.sh`처럼 명시적으로 실행한다.
