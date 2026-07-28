# 실습 2: Office Custom Container

이 실습은 역할에 따라 두 문서로 나뉜다.

| 역할 | 가이드 | 수행 내용 |
| --- | --- | --- |
| 관리자 | [실습 2A](02A_Office_Custom_Container_Admin_Lab.md) | ACR, identity, environment, Custom Container pool, monitoring과 비용 통제 |
| 실습 운영자 | [실습 2B](02B_Office_Custom_Container_User_Lab.md) | 최종 사용자 경험을 REST API로 검증: Office 생성·변환·편집, 결과 검토와 오류 확인 |

## 필요한 경우

DOCX·XLSX·PPTX 단순 생성은 Python pool로도 가능하다. 다음 요구가 있을 때 실습 2를 수행한다.

- 기존 Office 문서의 PDF 변환
- Poppler 기반 PDF 처리
- CJK 폰트와 도구 버전 고정
- 허용 목록 기반 선언적 문서 편집

Pandoc 기반 Markdown·HTML 변환 도구는 image에 포함돼 있지만 현재 reference API와 사용자 Gateway에는 노출하지 않는다. 이 기능이 필요하면 관리자 가이드의 허용 목록과 API schema를 명시적으로 확장해야 한다.

## 권장 순서

1. 관리자가 실습 2A로 Custom Container pool을 준비한다.
2. 실습 운영자가 실습 2B로 최종 사용자 관점의 생성·변환·편집 API 흐름을 검증한다.
3. 비용이 계속 발생하지 않도록 관리자가 pool과 Resource Group 정리 여부를 결정한다.
