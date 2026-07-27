# AI Workspace 격리형 Sandbox on Azure Container Apps Dynamic Sessions

AI Workspace가 사용자의 자연어 요청과 첨부파일을 받아 코드를 생성·실행하고, 데이터 분석 및 Office 문서 생성 작업을 격리된 환경에서 수행한 뒤 **검사와 사용자 승인 후에만** 실제 업무 시스템에 반영하기 위한 권장 아키텍처와 실습 자료다.

## 문서 구성

| 문서 | 주 대상 | 용도 |
| --- | --- | --- |
| [AI Workspace 권장 아키텍처](docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md) | 관리자·아키텍트 | Azure 권장 구조, 보안·격리, 세션·리소스·비용 운영, 모니터링, 제약사항, 대안 비교, 도입 단계 |
| [실습 1: Python Code Interpreter와 LLM](labs/01_Python_Code_Interpreter_Lab.md) | 관리자·실습 운영자 | 관리자용 Pool·LLM backend 구성과 사용자 API 기반 자연어 코드 생성·실행·승인 검증 |
| [실습 2: Office Custom Container](labs/02_Office_Custom_Container_Lab.md) | 관리자·실습 운영자 | 관리자용 인프라 구성과 사용자 API 기반 DOCX/PDF/PPTX/XLSX 생성·변환·편집 검증 |
| [Agent 오케스트레이션 소스](agent/) | 관리자·개발자 | 정책 엔진, Session Broker, LLM client, Artifact Staging, Approval Service |
| [Python 사용자 Gateway](python_gateway/) | 관리자·개발자 | 자연어·첨부파일 analysis job, 결과 다운로드와 동일 artifact 승인 API 제공 |
| [Office 이미지 소스](office-container/) | 관리자·개발자 | LibreOffice, Pandoc, Poppler를 포함한 비루트 HTTP 생성·변환·편집 서비스 |
| [Office 사용자 Gateway](office_gateway/) | 관리자·개발자 | Azure token·identifier를 숨기고 사용자용 document job·파일·승인 API 제공 |
| [자동 실행 스크립트](scripts/) | 관리자 | 사전 조건 검사, Python·Office·Agent 배포 및 검증, 명시적 전체 정리 |
| [Offline 테스트](tests/) | 관리자·개발자 | Azure 없이 정책·검사·승인 게이트 검증 |

## 처음이라면 이 순서로 진행

처음 수행하는 사람은 여러 경로를 섞지 말고 다음 순서만 따른다.

1. [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), Bash, `curl`, `jq`, Python 3를 준비하고 `az login`을 실행한다.
2. 아래 **Python Sandbox만 확인** Fast Path를 실행한다.
3. 자연어 요청까지 확인하려면 [실습 1A §16](labs/01A_Python_Code_Interpreter_Admin_Lab.md#16-llm-agent-backend-구성)에서 모델을 연결한다.
4. [실습 1B](labs/01B_Python_Code_Interpreter_User_Lab.md)를 따라 사용자 API 흐름을 검증한다.
5. Office PDF 변환이나 고정된 문서 도구가 필요할 때만 실습 2를 추가한다.
6. 마지막에 사용한 Resource Group 이름을 확인하고 [Python 실습 정리](labs/01A_Python_Code_Interpreter_Admin_Lab.md#17-정리) 또는 [Office 실습 정리](labs/02A_Office_Custom_Container_Admin_Lab.md#17-정리)를 수행한다.

> 실습 1B와 2B의 “사용자”는 실제 최종 사용자가 아니라 **사용자 경험을 REST API로 검증하는 실습 운영자·개발자**를 뜻한다. 실제 사용자는 terminal이나 `curl`을 사용하지 않고 AI Workspace UI에서 요청·미리보기·승인만 수행한다.
>
> 로컬 도구 설치가 부담되면 Azure Portal의 **Cloud Shell - Bash**를 사용한다. Repository를 clone한 뒤 같은 명령을 실행할 수 있다.

자주 쓰는 용어:

| 용어 | 이 문서에서의 의미 |
| --- | --- |
| pool | 격리 session을 할당하는 Azure 리소스 |
| session | 한 요청이나 대화에 할당되는 임시 실행 환경 |
| identifier | backend만 보관하는 session 식별자 |
| staging | 검사와 승인 전 결과 파일을 두는 임시 저장 위치 |
| 승격 | 승인된 결과만 최종 업무 저장소로 복사하는 과정 |

## Azure 사전 점검 빠른 판단표

자동 실습 전에 `bash scripts/check-prereqs.sh`를 실행하고 다음 기준으로 대응한다.

| 확인 결과 | 의미 | 조치 |
| --- | --- | --- |
| Resource 생성 권한 오류 | 현재 identity에 배포 권한이 없음 | 대상 Resource Group 또는 subscription에 `Contributor` 요청 |
| Role assignment 권한 오류 | `Contributor`만으로는 역할을 부여할 수 없음 | `Owner` 또는 `User Access Administrator` 요청. 관리자가 Session Executor 역할을 대신 부여해도 됨 |
| Session pool quota `available=0` | 해당 subscription·region에서 새 pool 생성 불가 | Portal **My quotas**에서 Provider를 **Azure Container Apps**로 선택해 regional `Session pools` 증가 요청 |
| Managed environment quota `available=0` | Office용 Environment를 새로 만들 수 없음 | 같은 Portal에서 `Managed Environment Count` 증가 요청 또는 기존 Environment 재사용 |
| 지역 미지원 오류 | 선택한 `LOCATION`에서 Dynamic Sessions 사용 불가 | [지원 리전](https://learn.microsoft.com/azure/container-apps/sessions#supported-regions) 중 quota가 있는 리전 선택 |
| Data plane HTTP 403 | Session Executor가 없거나 아직 전파되지 않음 | pool scope 역할 확인 → 30~60초 대기 → token 재발급 |
| `SessionRequestValidationFailed` | endpoint, method, query 또는 API version 불일치 | 응답의 `error.code`, `message`, `target`, `traceId`를 확인하고 공식 API 문서와 비교 |

이 실습에서 확인하는 `Session pools`와 `Managed Environment Count` quota는 region 단위이고 Portal에서 증가를 요청한다. 요청은 즉시 승인되기도 하지만 검토가 필요하면 며칠 걸릴 수 있으므로 실습 직전에 처음 확인하지 않는다.

이 repository의 Preview API 기본값은 `PYTHON_API_VERSION=2025-10-02-preview`, `SESSION_API_VERSION=2025-02-02-preview`다. 검증된 기본값을 우선 사용하고, 공식 문서에서 변경을 확인한 경우에만 환경 변수로 재정의한다.

## 가장 빠른 시작

이 절의 명령은 repository root에서 실행한다. 역할별 수동 가이드는 중간에 `.work/` 디렉터리로 이동하므로, 각 코드 블록 바로 앞의 현재 위치 안내를 따른다. 자동 스크립트는 현재 `az` subscription과 기본 리전 `koreacentral`을 사용하며, `SUBSCRIPTION_ID`, `RESOURCE_GROUP`, `LOCATION` 같은 환경 변수로 재정의할 수 있다.

현재 선택된 subscription을 먼저 확인한다.

```bash
az account show --query '{name:name,id:id,user:user.name}' --output table
```

### Python Sandbox만 확인

```bash
bash scripts/check-prereqs.sh
bash scripts/python-lab.sh
```

### 자연어 요청부터 승인까지 자동 검증

실습 2의 Office pool은 필요 없다.

```bash
bash scripts/check-prereqs.sh
bash scripts/python-lab.sh
bash scripts/agent-lab.sh
```

위 명령은 backend 정책·재시도·승인 게이트를 자동 검증한다. 실제 사용자 REST API와 `curl` 흐름은 [실습 1B](labs/01B_Python_Code_Interpreter_User_Lab.md)의 Gateway 실행부터 진행한다.

### Office 생성·변환·편집 자동 검증

Python pool은 필요 없다. Custom Container ready session과 관련 리소스에 비용이 발생하므로 정리 절차까지 확인한다.

```bash
bash scripts/check-prereqs.sh
bash scripts/office-lab.sh
```

실제 사용자 REST API와 `curl` 흐름은 [실습 2B](labs/02B_Office_Custom_Container_User_Lab.md)의 Gateway 실행부터 진행한다.

### 전체 backend 자동 검증

```bash
bash scripts/check-prereqs.sh
bash scripts/python-lab.sh
bash scripts/office-lab.sh
bash scripts/agent-lab.sh
```

이 명령 묶음은 두 pool과 Agent backend를 자동 검증한다. 사용자 REST 흐름은 이어서 실습 1B와 2B의 Gateway·`curl` 절을 각각 수행한다.

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
| 자연어 요청에 따른 Python 코드 생성·실행과 결과 반환 | 실습 1B §4 |
| 실행 오류 분석, 제한된 코드 수정과 재실행 | 실습 1A §13.2, 실습 1B §6 |
| 데이터 분석, 계산, 차트 및 결과 파일 생성 | 실습 1A §9~12, 실습 1B §4 |
| 첨부파일을 사용한 분석·가공 | 실습 1A §10, 실습 1B §3~4 |
| DOCX, XLSX, PPTX, PDF 생성 | 실습 1A §8.1, 실습 2B §3 |
| Office 문서 변환과 편집 | 실습 2B §4~5 |
| 사용자 또는 요청 단위의 독립 세션과 임시 파일 공간 | 실습 1A §13.1 |
| Code Interpreter 실행 시간·업로드·메모리, Custom Container CPU·메모리·lifecycle, 네트워크와 허용 명령어 제한 | 실습 1A §13, §13.3, 실습 2A §9, 실습 1B §6 |
| 작업 완료 또는 session 종료 시 환경과 파일 자동 정리 | 실습 1A §14.1 |
| 검사, 미리보기, Diff와 사용자 승인 후 실제 업무 시스템 반영 | 실습 1B §5, 실습 2B §6 |

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
| 선언적 편집 | DOCX·PPTX text 교체와 XLSX cell·sheet 편집 성공, `runShell`과 수식 주입 HTTP 400 |
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
| Offline 테스트 | 76개 통과 |

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

### 2026-07-25 재배포 재검증

| 항목 | 결과 |
| --- | --- |
| Python pool | 기존 pool 재사용, 분석·egress 차단·격리·오류 수정·session 정리 통과 |
| Office pool | image `office-sandbox:20260725120350` 재빌드·업데이트, 생성·변환·편집·로그·metric 통과 |
| 실제 LLM | 기존 `gpt-5.6-terra` 배포와 Entra RBAC를 재사용해 자연어 실행·승인 통과 |
| Python 사용자 Gateway | create·download·approve·delete 전체 REST 흐름 통과 |
| Office 사용자 Gateway | create·download·convert·edit·approve·delete 전체 REST 흐름 통과 |
| Live RBAC | 두 pool의 Session Executor와 ACR의 AcrPull 확인 |
| Regional quota | Managed Environment 48개, Session pool 48개 추가 사용 가능 |

재검증에서 실제 모델의 정상적인 출력 변동도 확인했다.

- `summary.json.monthly_sales`가 object 또는 `{month, sales}` array로 생성될 수 있어 값 기준 검증으로 변경했다.
- 실제 모델은 정상 요청도 코드 오류를 한 번 수정해 2회차에 성공할 수 있으므로 재시도 한도 내 성공을 정상으로 판단한다.
- 재시도 한도 자체의 결정론적 검증은 `LLM_PROVIDER=stub`으로 분리했다.
- 기존 RBAC가 있으면 재사용하고, 없을 때만 생성한다.
- 자동 스크립트는 성공·실패와 관계없이 검증용 session을 즉시 delete/stop한다.

### 2026-07-28 수정 후 Azure 재검증

| 항목 | 결과 |
| --- | --- |
| Repository validation | Python parse와 offline 테스트 **76개 통과** |
| Python Code Interpreter | 분석·파일·egress 차단·격리·오류 수정·이중 delete cleanup 통과 |
| Office image | `office-sandbox:20260727224541`, digest `sha256:719165f1725599562221736110d300c40cdaf2e3aa8d61dd6eb535e5d840ed2b` |
| Office release 확인 | `/health.release`가 기대 image tag와 일치하는 새 session에서만 검증 진행 |
| Office 편집 | `report.edited.docx`, `report.edited.pptx`, `report.edited.xlsx` 생성·hash·ZIP·PPTX text 확인 |
| 실제 LLM | `gpt-5.6-terra` Entra 인증으로 자연어 코드 생성·실행·승인 통과 |
| 사용자 Gateway | Python과 Office create·download·edit·approve·delete 전체 REST 흐름 통과 |
| 최종 session 상태 | Python 0건, Office 0건 확인 |
| Pool 상태 | 두 pool `Succeeded`, `EgressDisabled`; Office `nodeCount=1`, ready 1, pending 0 |

이 재검증에서 pool update 직후 기존 ready session이 이전 image를 한 번 더 반환할 수 있음을 확인했다. Office 배포 스크립트는 image tag를 `/health.release`와 비교하고, 이전 release session을 확인된 HTTP 응답으로 중지한 뒤 새 session으로 재시도한다.

> 위 capacity 250은 당시 가용 쿼타를 실측하기 위한 과거 검증 기록이며 권장 설정이 아니다. 현재 관리자 가이드는 기존 배포를 우선 재사용하고, 새 실습 배포는 10K TPM처럼 작은 capacity부터 시작한다.

> 실제 모델 검증에서 **stub으로는 재현되지 않는 버그를 발견해 고쳤다.** matplotlib·pandas 경고가 `stderr`로 출력되면서, `status`가 `Succeeded`인데도 성공한 코드를 실패로 판정해 재시도 한도를 소진했다. 성공 판정은 `stderr`가 아니라 `status`로만 해야 한다. 관리자 확인 사항은 [실습 1A §16.4](labs/01A_Python_Code_Interpreter_Admin_Lab.md#164-관리자-확인-사항)에 있다.

> 역할별 가이드 재검증에서는 LLM이 `summary.json` 대신 `monthly_sales_summary.json`을 만들어도 실행 `status`만 보고 성공으로 반환하는 결함을 발견했다. 필수 산출물 이름을 prompt에 명시하고, 실행 후 누락된 파일이 있으면 제한 횟수 안에서 다시 생성하도록 수정했다.

> Dynamic Sessions 과금은 pool 유형별로 다르다. Code Interpreter는 할당 session 시간을 1시간 단위로 과금하고, Custom Container는 active·ready session을 수용하는 전용 E16 `nodeCount` 기반이다. Environment, ACR, Log Analytics는 pool을 지워도 남을 수 있다. [아키텍처 10.5절](docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md#105-비용-모델)을 확인한다.

Production 설계 전에는 [권장 아키텍처](docs/AI_Workspace_Dynamic_Sessions_Reference_Architecture.md)에서 trust boundary와 승인 경계를 결정하고, `agent/`의 reference 구현을 실제 인증, malware·DLP 검사, 승인 UI, 최소 권한 Connector로 대체한다.

현재 reference 구현은 사용자 Office/PDF 원본 업로드, PDF 페이지 편집, malware·DLP, 렌더링 미리보기·Diff와 실제 업무 Connector를 구현하지 않는다. 이 항목은 아키텍처 목표이며 Production 통합 범위다.

실행 한도 테스트는 약 4분이 걸리므로 기본적으로 건너뛴다. 필요하면 다음과 같이 실행한다.

```bash
RUN_LIMIT_TESTS=yes bash scripts/python-lab.sh
```

Azure 없이 통제 장치만 확인하려면 다음을 실행한다.

```bash
python3 -m unittest discover -s tests -v
bash scripts/validate-repo.sh
```

자동 스크립트는 검증용 임시 session만 즉시 delete/stop하고 pool과 Azure 리소스는 삭제하지 않는다. `cleanup.sh`는 Resource Group 전체를 삭제하므로 대상 이름을 확인한 뒤 실행한다.

```bash
RESOURCE_GROUP="rg-ai-workspace-sandbox-lab" \
CONFIRM_DELETE=yes \
bash scripts/cleanup.sh
```
