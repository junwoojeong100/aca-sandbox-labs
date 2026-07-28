# ACA Sandboxes 과거 검증 기록

이 문서는 ACA Sandboxes 실습에서 확인한 과거 관측 결과를 기록한다. 검증
당시 service와 SDK는 Preview였으므로, 현재 호환성을 보장하는 문서가 아니라
regression 근거로 사용한다.

## 2026-07-28, 한국 중부

검증 환경:

- `azure-containerapps-sandbox==0.1.0b4`
- Management plane에서 생성한 SandboxGroup
- SandboxGroup data plane을 사용하는 Broker access
- 기본 egress 거부와 full traffic inspection
- Private registry를 사용하는 Custom Python 및 Office disk image

### Python Sandbox 경로

| 항목 | 결과 |
| --- | --- |
| SandboxGroup 및 RBAC | 생성하고 data-plane access 확인 |
| Python runtime | Python 3.12 실행 통과 |
| Custom 분석 image | Build, Ready disk image 변환 및 선택 통과 |
| File operation | Write, read, list, delete 통과 |
| CSV 분석 | 월별 합계 200.0, 240.0, 240.0 확인 |
| Artifact 검증 | JSON 값과 download byte 확인 |
| Egress | 기본 deny 및 full inspection에서 차단 |
| Isolation | 두 번째 Sandbox에서 첫 workspace file list/read 불가 |
| 오류 수정 | 잘못된 column 실행 실패 후 수정 실행 통과 |
| State 유지 | 수정 실행에서도 input file 유지 |
| Suspend 및 resume | Memory suspend/resume 후 file state 유지 |
| Cleanup | Validation Sandbox 삭제, active count 0 |

관측 당시 Fast Path의 structured result 경로는
`.work/sandboxes-live/python-validation.json`이었다.

### Office Sandbox 경로

| 항목 | 결과 |
| --- | --- |
| Private image 등록 | ACR image를 Ready Office disk image로 변환 |
| Registry 동작 | 고정된 Preview SDK에서 short-lived ACR refresh token 사용 성공 |
| Office tool | LibreOffice, Pandoc, Poppler 및 문서 library 확인 |
| 생성 | DOCX, PDF, PPTX, XLSX 생성 |
| 변환 | 허용된 PPTX→PDF 및 DOCX→text 통과 |
| 변환 policy | 허용되지 않은 source-target 조합 거부 |
| 선언적 편집 | DOCX/PPTX text 및 XLSX cell 편집 통과 |
| 안전하지 않은 operation | 승인되지 않은 operation을 실행 전에 거부 |
| Artifact 검사 | File signature, size 및 SHA-256 확인 |
| Egress | External access 차단 |
| Suspend 및 resume | Office artifact hash 유지 |
| Cleanup | Active Office Sandbox 삭제 |

관측 당시 Fast Path의 structured result 경로는
`.work/sandboxes-live/office-validation.json`이었다.

### Preview 호환성 관측

`azure-containerapps-sandbox==0.1.0b4`에서 검증한
`managed_identity_resource_id` 경로는 private ACR disk image 등록을
완료하지 못했다. Service는 registry credential 또는 Managed Identity
client ID를 요구했다. 실습은 `az acr login --expose-token`이 반환한
short-lived token을 memory에서만 사용하고 저장하거나 log에 기록하지 않았다.
SDK upgrade 전 이 동작을 다시 검증해야 한다.

## 운영 관측

- Platform allocation 또는 memory resume 수치는 전체 application latency가
  아니다. Image boot, SDK polling, file staging 및 initialization을 별도로
  측정한다.
- Running Sandbox는 vCPU 및 memory 비용이 발생한다. Suspend 후 compute
  activity는 중단됐지만 disk image, snapshot, volume, registry storage 및
  log는 별도 lifecycle로 남았다.
- Fast Path는 active validation Sandbox를 삭제했지만 재사용을 위해
  SandboxGroup과 Ready disk image를 유지했다.
- Snapshot과 disk image에는 명시적인 retention 및 orphan cleanup이 필요하다.
- Resource ID와 SDK object는 Backend 내부 정보이며 사용자 job contract에
  포함하지 않았다.
- Preview resource와 SDK surface는 변경될 수 있으므로 version pin, canary 및
  recreation runbook이 필요하다.

## 관련 실습

- [Python 관리자 실습](../../labs/aca-sandboxes/03A_ACA_Sandboxes_Admin_Lab.md)
- [Python 사용자 실습](../../labs/aca-sandboxes/03B_ACA_Sandboxes_User_Lab.md)
- [Office 관리자 실습](../../labs/aca-sandboxes/03C_ACA_Sandboxes_Office_Admin_Lab.md)
- [Office 사용자 실습](../../labs/aca-sandboxes/03D_ACA_Sandboxes_Office_User_Lab.md)
