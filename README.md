# AI Workspace 격리형 Sandbox on Azure Container Apps Dynamic Sessions

AI Workspace가 사용자의 자연어 요청과 첨부파일을 받아 코드를 생성·실행하고, 데이터 분석 및 Office 문서 생성 작업을 격리된 환경에서 수행한 뒤 **검사와 사용자 승인 후에만** 실제 업무 시스템에 반영하기 위한 권장 아키텍처와 실습 자료다.

## 문서 구성

| 문서 | 주 대상 | 용도 |
| --- | --- | --- |
| [AI Workspace 권장 아키텍처](docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md) | 관리자·아키텍트 | Azure 권장 구조, 보안·격리, 세션·리소스·비용 운영, 모니터링, 제약사항, 대안 비교, 도입 단계 |
| [실습 1: Python Code Interpreter와 LLM](labs/01_Python_Code_Interpreter_Lab.md) | 관리자·사용자 | 관리자용 Pool·LLM backend 구성과 사용자용 자연어 코드 생성·실행·승인 |
| [실습 2: Office Custom Container](labs/02_Office_Custom_Container_Lab.md) | 관리자·사용자 | 관리자용 인프라 구성과 사용자용 DOCX/PDF/PPTX/XLSX 생성·변환·편집 |
| [Agent 오케스트레이션 소스](agent/) | 관리자·개발자 | 정책 엔진, Session Broker, LLM client, Artifact Staging, Approval Service |
| [Python 사용자 Gateway](python_gateway/) | 관리자·개발자 | 자연어·첨부파일 analysis job, 결과 다운로드와 동일 artifact 승인 API 제공 |
| [Office 이미지 소스](office-container/) | 관리자·개발자 | LibreOffice, Pandoc, Poppler를 포함한 비루트 HTTP 생성·변환·편집 서비스 |
| [Office 사용자 Gateway](office_gateway/) | 관리자·개발자 | Azure token·identifier를 숨기고 사용자용 document job·파일·승인 API 제공 |
| [자동 실행 스크립트](scripts/) | 관리자 | 사전 조건 검사, Python·Office·Agent 배포 및 검증, 명시적 전체 정리 |
| [Offline 테스트](tests/) | 관리자·개발자 | Azure 없이 정책·검사·승인 게이트 검증 |

## 가장 빠른 시작

모든 명령은 repository root에서 실행한다. 자동 스크립트는 현재 `az` subscription과 기본 리전 `koreacentral`을 사용하며, `SUBSCRIPTION_ID`, `RESOURCE_GROUP`, `LOCATION` 같은 환경 변수로 재정의할 수 있다.

### Python Sandbox만 확인

```bash
bash scripts/check-prereqs.sh
bash scripts/python-lab.sh
```

### 자연어 요청부터 승인까지 확인

실습 2의 Office pool은 필요 없다.

```bash
bash scripts/check-prereqs.sh
bash scripts/python-lab.sh
bash scripts/agent-lab.sh
```

### Office 생성·변환·편집만 확인

Python pool은 필요 없다. Custom Container ready session과 관련 리소스에 비용이 발생하므로 정리 절차까지 확인한다.

```bash
bash scripts/check-prereqs.sh
bash scripts/office-lab.sh
```

### 전체 경로 확인

```bash
bash scripts/check-prereqs.sh
bash scripts/python-lab.sh
bash scripts/office-lab.sh
bash scripts/agent-lab.sh
```

`check-prereqs.sh`는 로컬 도구, Azure CLI 버전과 로그인만 확인한다. quota와 RBAC 권한은 각 실습 스크립트가 조회하거나 실제 리소스·역할 생성 과정에서 검증한다. 자동 실행이 기본 경로이며, 세부 명령과 문제 해결이 필요할 때 역할별 [실습 1](labs/01_Python_Code_Interpreter_Lab.md)과 [실습 2](labs/02_Office_Custom_Container_Lab.md)를 참고한다.

## Session 연결 방식과 화면

Dynamic Sessions는 SSH, RDP, Azure Portal의 웹 터미널로 접속하지 않는다. AI Workspace backend가 Microsoft Entra token과 backend-only `identifier`를 사용해 session pool management endpoint의 REST API를 호출한다. 같은 identifier를 사용하면 기존 session으로 라우팅되고, 없으면 pool에서 새 session이 할당된다.

| Pool | Backend 연결 | 실습 운영자가 보는 결과 | 실제 사용자가 보는 화면 |
| --- | --- | --- | --- |
| Python Code Interpreter | `/executions`, `/files`, `/session` | terminal의 JSON `status`, `stdout`, `stderr`와 다운로드 파일 | AI Workspace의 작업 계획, 진행 상태, 결과 파일, 미리보기와 승인 |
| Office Custom Container | `/health`, `/generate`, `/convert`, `/edit` | terminal의 JSON operation·job·파일 metadata와 다운로드 문서 | AI Workspace의 문서 요청, 미리보기, Diff, 검사 결과와 승인 |

Azure Portal에서는 pool 구성, provisioning 상태, metrics와 logs를 확인할 수 있지만 session 내부 desktop이나 shell 화면은 제공되지 않는다. Custom Container에 별도 HTML UI를 구현하지 않는 한 management endpoint도 JSON·파일 API만 반환한다.

## 지원하는 AI Workspace 시나리오

| 고객 요건 | 검증 위치 |
| --- | --- |
| 자연어 요청에 따른 Python 코드 생성·실행과 결과 반환 | 실습 1B §3 |
| 실행 오류 분석, 제한된 코드 수정과 재실행 | 실습 1A §13.2, 실습 1B §5 |
| 데이터 분석, 계산, 차트 및 결과 파일 생성 | 실습 1A §9~12, 실습 1B §3 |
| 첨부파일을 사용한 분석·가공 | 실습 1A §10, 실습 1B §2~3 |
| DOCX, XLSX, PPTX, PDF 생성 | 실습 1A §8.1, 실습 2B §2 |
| Office 문서 변환과 편집 | 실습 2B §3~4 |
| 사용자 또는 요청 단위의 독립 세션과 임시 파일 공간 | 실습 1A §13.1 |
| 실행 시간, 메모리, 네트워크, 허용 명령어 제한 | 실습 1A §13, §13.3, 실습 1B §5 |
| 작업 완료 또는 session 종료 시 환경과 파일 자동 정리 | 실습 1A §14.1 |
| 검사, 미리보기, Diff와 사용자 승인 후 실제 업무 시스템 반영 | 실습 1B §4, 실습 2B §5 |

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
- Custom Container: Startup/Liveness probe와 비루트 실행, 허용 목록 기반 API만 노출
- 정책 엔진은 LLM보다 먼저 실행하고, 승격 기본값은 "승격하지 않음"
- 최종 반영: Sandbox와 분리된 Approval Service만 수행

> 통제 원칙: Python pool은 **"안에서 무엇이든 할 수 있지만 밖으로는 아무것도 못 한다"**, Office pool은 **"허용된 operation만 호출할 수 있다"**. 근거는 [아키텍처 4.4.1절](docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md#441-허용-명령어-제한을-실제로-어떻게-달성하는가)에 있다.

## 실제 검증 상태

2026-07-24~25 한국 중부 리전에서 다음 항목을 실제 검증했다.

### Sandbox 실행 경로

| 항목 | 결과 |
| --- | --- |
| Python Code Interpreter 실행 | HTTP 200, `Succeeded` |
| CSV 업로드 및 분석 | 성공 |
| PNG·JSON 산출물 생성과 다운로드 | 성공, SHA-256 확인 |
| 사전 설치 라이브러리 | Python 3.12.7, pandas·numpy·matplotlib·scipy·scikit-learn·python-docx·python-pptx·openpyxl 등 확인 |
| Python pool 외부 통신 차단 | `EGRESS_BLOCKED` 확인 |
| 세션 간 파일 격리 | 두 번째 session 목록 비어 있음, 교차 다운로드 HTTP 404 |
| 오류 후 코드 수정 재실행 | 1회차 `Failed`, 2회차 `Succeeded` |
| 실행 시간 한도 | 300초 작업이 221.5초에 `Failed` |
| 메모리 한도 | 무한 할당이 `Execution aborted`로 종료 |
| Session 삭제와 파일 정리 | HTTP 204, 이후 목록 비어 있고 다운로드 404 |
| Python 사용자 Gateway | multipart 자연어·CSV → 실제 LLM 실행 → 다운로드 → 동일 hash 승인 → 삭제 성공 |

### Office 문서 경로

| 항목 | 결과 |
| --- | --- |
| Office Custom Container `/health` | HTTP 200, 도구 버전과 허용 operation 노출 |
| LibreOffice, Pandoc, Poppler | 컨테이너 내부 버전 확인 |
| DOCX·PDF·PPTX·XLSX 생성과 다운로드 | 성공, 파일 형식과 SHA-256 확인 |
| 허용 목록 기반 변환 | PPTX→PDF 성공, 비허용 조합 HTTP 400 |
| 변환 시 원본 보존 | PPTX→PDF 후 기존 `report.pdf` hash 불변 |
| 선언적 편집 | 3개 operation 적용 성공, `runShell`과 수식 주입 HTTP 400 |
| 문서 입력 안전성 | Markdown형 로컬 경로·URL을 literal text로 처리, 외부·로컬 media 미삽입 |
| XLSX 입력 안전성 | formula형 생성 입력은 text로 저장, 실패한 편집 batch는 전체 rollback |
| Custom Container Startup/Liveness probe | pool 구성 반영 확인 |
| Log Analytics와 pool metrics | console log와 ready/executing/pending metric 확인 |
| Session identifier log 비노출 | Log Analytics 검색 결과 0건 |
| Local container smoke | 생성·변환·편집, hash, 오류 응답과 log sanitization 통과 |
| Office 사용자 Gateway | create·status·download·convert·edit·approve·delete `curl` 흐름 성공 |

### Agent 오케스트레이션

| 항목 | 결과 |
| --- | --- |
| 자연어 요청 → 분류 → 실행 → staging | 성공, 전체 16초 |
| 정책 분류 A/B/C/D/E | 기대대로 분기, 거부 시 session 미할당 |
| 오류 복구 루프 | 1회차 `Failed`, 2회차 `Succeeded` |
| 재시도 한도 | `MAX_CODE_RETRIES=0`에서 1회 후 중단 |
| 승인 없는 승격 | 차단 확인 |
| 승인 후 승격과 hash 재검증 | 성공 |
| Session identifier 사용자 응답 비노출 | 확인 |
| Offline 테스트 | 70개 통과 |

### 실제 모델 연결 (gpt-5.6-terra)

| 항목 | 결과 |
| --- | --- |
| Foundry 리소스·프로젝트 생성 | koreacentral, `Succeeded` |
| `gpt-5.6-terra` GlobalStandard 배포 | capacity **250 (250K TPM, 가용 쿼타 전량)** |
| Entra token 추론 호출 | HTTP 200, API key 미사용 |
| `reasoning_effort: medium` | 정상 동작 |
| 자연어 요청 → 코드 생성 → 실행 → 승격 | 1회 시도 성공, **17.8초** |
| 결과 정확성 | 월별 합계 200/240/240, 총합 680.0 일치 |
| 필수 산출물 이름 | `monthly_sales.png`, `summary.json` 생성과 승인 승격 확인 |

> 실제 모델 검증에서 **stub으로는 재현되지 않는 버그를 발견해 고쳤다.** matplotlib·pandas 경고가 `stderr`로 출력되면서, `status`가 `Succeeded`인데도 성공한 코드를 실패로 판정해 재시도 한도를 소진했다. 성공 판정은 `stderr`가 아니라 `status`로만 해야 한다. 관리자 확인 사항은 [실습 1A §18.4](labs/01A_Python_Code_Interpreter_Admin_Lab.md#184-관리자-확인-사항)에 있다.

> 역할별 가이드 재검증에서는 LLM이 `summary.json` 대신 `monthly_sales_summary.json`을 만들어도 실행 `status`만 보고 성공으로 반환하는 결함을 발견했다. 필수 산출물 이름을 prompt에 명시하고, 실행 후 누락된 파일이 있으면 제한 횟수 안에서 다시 생성하도록 수정했다.

> Custom Container pool은 최소 1개의 ready session이 필요하므로 유지 비용이 발생한다. Environment, ACR, Log Analytics는 pool을 지워도 남는다. 비용 구조는 [아키텍처 10.5절](docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md#105-비용-모델)을 확인한다.

Production 설계 전에는 [권장 아키텍처](docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md)에서 trust boundary와 승인 경계를 결정하고, `agent/`의 reference 구현을 실제 인증, malware·DLP 검사, 승인 UI, 최소 권한 Connector로 대체한다.

실행 한도 테스트는 약 4분이 걸리므로 기본적으로 건너뛴다. 필요하면 다음과 같이 실행한다.

```bash
RUN_LIMIT_TESTS=yes bash scripts/python-lab.sh
```

Azure 없이 통제 장치만 확인하려면 다음을 실행한다.

```bash
python3 -m unittest discover -s tests -v
bash scripts/validate-repo.sh
```

자동 스크립트는 기본적으로 리소스를 삭제하지 않는다. `cleanup.sh`는 기본 Resource Group 전체를 삭제하므로 대상 이름을 확인한 뒤 `CONFIRM_DELETE=yes bash scripts/cleanup.sh`처럼 명시적으로 실행한다.
