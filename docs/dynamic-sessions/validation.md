# Dynamic Sessions 과거 검증 기록

이 문서는 repository 실습에서 확인한 과거 관측 결과를 기록한다. 현재
platform 동작을 보장하는 문서가 아니라 regression 계획을 위한 근거다.
배포 결정을 내리기 전에 현재 Fast Path를 다시 실행한다.

## 2026-07-24~2026-07-25, 한국 중부

### Python Code Interpreter

| 항목 | 결과 |
| --- | --- |
| Python 실행 | HTTP 200, platform status `Succeeded` |
| CSV upload 및 분석 | 통과 |
| PNG 및 JSON artifact | 생성, download, SHA-256 검증 통과 |
| Runtime version | Python 3.12.7 |
| 주요 package | pandas 2.2.2, NumPy 1.26.4, matplotlib 3.8.4, SciPy 1.13.1, scikit-learn 1.5.1, python-docx 1.2.0, python-pptx 1.0.2, openpyxl 3.1.5 |
| Outbound access | `EgressDisabled`에서 차단 |
| Session isolation | 두 번째 session의 file 목록이 비어 있고 교차 download는 404 |
| 오류 수정 | 첫 실행 `Failed`, 수정 실행 `Succeeded` |
| 실행 limit | 300초 작업이 약 221.5초 후 실패 |
| Memory pressure | 무한 할당이 `Execution aborted`로 종료 |
| Session 삭제 | HTTP 204, 이후 file 목록과 download 불가 |
| Python User Gateway | 자연어 multipart 요청, 실행, download, hash 보존 approval 및 delete 통과 |

### Office Custom Container

| 항목 | 결과 |
| --- | --- |
| `/health` | HTTP 200, tool version과 허용 operation 반환 |
| Tool version | LibreOffice 7.4.7.2, Pandoc 2.17.1.1, Poppler 22.12.0 |
| 생성 | DOCX, PDF, PPTX, XLSX 생성 및 download 통과 |
| 변환 | 허용된 PPTX→PDF 통과, 허용되지 않은 target은 HTTP 400 |
| 원본 보존 | 변환 중 기존 output hash 유지 |
| 선언적 편집 | DOCX/PPTX text 및 XLSX cell/sheet 편집 통과 |
| 안전하지 않은 편집 | `runShell`과 formula injection은 HTTP 400 |
| 입력 안전성 | URL 및 local path 형태의 text를 literal로 유지하고 remote media 미조회 |
| Transaction 동작 | 실패한 XLSX edit batch rollback |
| Probe | Startup 및 Liveness 구성 확인 |
| Monitoring | Console log와 ready/executing/pending metric 관측 |
| Identifier 노출 | Log 검색에서 session identifier 없음 |
| Local image smoke | 생성, 변환, 편집, hash, 오류 처리 및 log sanitization 통과 |
| Office User Gateway | Create, status, download, convert, edit, approve, delete 통과 |

### Agent Orchestration 검증

| 항목 | 결과 |
| --- | --- |
| 자연어 요청부터 staging | 관측 실행에서 16초에 통과 |
| Deterministic policy class | 예상 allow/deny routing, 거부 요청은 session 미할당 |
| Code 수정 | 첫 시도 실패, 두 번째 시도 성공 |
| Retry ceiling | `MAX_CODE_RETRIES=0`에서 한 번 실행 후 중단 |
| 승인 없는 승격 | 차단 |
| 승인된 승격 | Hash 재검증 후 통과 |
| Identifier 노출 | 사용자 응답에 없음 |
| 당시 offline suite | 76개 test 통과 |

### 실제 Model 연결 실행

| 항목 | 결과 |
| --- | --- |
| Region 및 deployment | Korea Central, `gpt-5.6-terra` GlobalStandard |
| 인증 | Entra token, API key 미사용 |
| `reasoning_effort: medium` | 정상 처리 |
| 자연어 요청부터 승인 artifact | 첫 시도, 17.8초에 통과 |
| 예상 합계 | 200, 240, 240, 총합 680.0 |
| 필수 artifact | `monthly_sales.png`, `summary.json` 생성 및 승격 |

관측 당시 model deployment는 사용 가능한 quota를 확인하기 위해 250K TPM
capacity를 사용했다. 이는 권장 baseline이 아니라 검증 조건이었다.

## 2026-07-25 재배포 Regression

| 항목 | 결과 |
| --- | --- |
| Python Pool | 재사용, 분석·egress 차단·isolation·수정·cleanup 통과 |
| Office Pool | Image rebuild 및 Pool update 후 생성·변환·편집·log·metric 통과 |
| 실제 Model 흐름 | 기존 deployment와 Entra RBAC 재사용 성공 |
| User Gateway | Python과 Office end-to-end REST 흐름 통과 |
| Live RBAC | Session Executor와 ACR `AcrPull` 확인 |
| Regional quota 관측 | Managed Environment와 Session Pool 각각 48개 추가 가능 |

Regression에서 확인한 사항:

- 값이 일치하면 model output의 object 또는 `{month, sales}` array 형식을
  모두 허용한다.
- 정상 요청도 한 번의 code 수정이 필요할 수 있으므로 설정한 retry ceiling
  안에서 성공하면 정상으로 판단한다.
- Retry ceiling은 stub provider로 deterministic하게 검증한다.
- 기존 RBAC assignment와 model deployment를 우선 재사용한다.
- 성공과 실패 모두에서 validation session을 delete 또는 stop한다.
- 실행 성공은 빈 `stderr`가 아니라 platform `status`로 판단한다.
- 필수 artifact 이름을 지정하고 누락 시 retry한다.

## 2026-07-28 Regression 검증

| 항목 | 결과 |
| --- | --- |
| 당시 repository validation | Python parse와 offline test 76개 통과 |
| Python 경로 | 분석, file 처리, egress 차단, isolation, 수정, 반복 cleanup 통과 |
| Office image tag 값 | `office-sandbox:20260727224541` |
| Office image digest 값 | `sha256:719165f1725599562221736110d300c40cdaf2e3aa8d61dd6eb535e5d840ed2b` |
| Release 검증 | `/health.release`가 예상 image tag와 일치 |
| Office 편집 | DOCX, PPTX, XLSX 편집 artifact 생성 및 검증 |
| 실제 Model 흐름 | 자연어 생성, 실행 및 approval 통과 |
| User Gateway | Python과 Office create/download/edit/approve/delete 통과 |
| 최종 session | Python 0, Office 0 |
| Pool 상태 | 모두 `Succeeded`, `EgressDisabled`, Office ready 1, pending 0 |

Pool image update 직후 기존 ready session 하나가 이전 release를 반환했다.
배포 흐름은 `/health.release`를 비교하고, 확인된 응답 후 stale session을
stop한 다음 새 identifier로 retry하도록 변경했다.

## 관련 실습

- [Python 관리자 실습](../../labs/dynamic-sessions/01A_Python_Code_Interpreter_Admin_Lab.md)
- [Python 사용자 실습](../../labs/dynamic-sessions/01B_Python_Code_Interpreter_User_Lab.md)
- [Office 관리자 실습](../../labs/dynamic-sessions/02A_Office_Custom_Container_Admin_Lab.md)
- [Office 사용자 실습](../../labs/dynamic-sessions/02B_Office_Custom_Container_User_Lab.md)
