"""정책 엔진.

LLM의 판단과 분리된 결정론적 규칙으로 실행 경로를 정한다.
아키텍처 문서 4.2절의 분류 A~E를 구현한다.
LLM 응답은 정책 입력일 뿐이며 정책 결정을 덮어쓸 수 없다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

MAX_EXECUTION_SECONDS = 220
MAX_UPLOAD_BYTES = 128 * 1024 * 1024

OFFICE_KEYWORDS = (
    "docx",
    "xlsx",
    "pptx",
    "pdf",
    "워드",
    "엑셀",
    "파워포인트",
    "보고서 문서",
    "문서 생성",
    "문서 변환",
    "presentation",
    "slide",
    "spreadsheet",
)

NETWORK_KEYWORDS = (
    "http://",
    "https://",
    "download from",
    "인터넷",
    "외부 api",
    "웹에서",
    "크롤",
    "crawl",
    "scrape",
)

ADMIN_KEYWORDS = (
    "az login",
    "az account",
    "kubectl",
    "terraform",
    "rm -rf /",
    "운영 db",
    "production database",
    "프로덕션 배포",
    "배포해줘",
    "credential",
    "secret 조회",
)

# 생성된 코드에서 거부하는 pattern. 격리 자체는 Hyper-V가 담당하고
# 이 검사는 명백한 정책 위반을 조기에 걸러내는 보조 수단이다.
DENIED_CODE_PATTERNS = (
    (r"\bsubprocess\b", "subprocess 호출"),
    (r"\bos\.system\b", "os.system 호출"),
    (r"\bos\.popen\b", "os.popen 호출"),
    (r"\bsocket\b", "raw socket 사용"),
    (r"\brequests\b", "외부 HTTP client"),
    (r"\burllib\.request\b", "외부 HTTP 요청"),
    (r"\bpip\s+install\b", "package 설치"),
    (r"\b__import__\s*\(", "동적 import"),
)


class Route(str, Enum):
    PYTHON_POOL = "python-pool"
    OFFICE_POOL = "office-pool"
    ASYNC_COMPUTE = "async-compute"
    CONTROLLED_EGRESS = "controlled-egress"
    DENY = "deny"


@dataclass
class PolicyInput:
    """정책 결정에 사용하는 입력. LLM이 아니라 backend가 채운다."""

    tenant_id: str
    user_id: str
    request_text: str
    attachment_names: tuple[str, ...] = ()
    attachment_bytes: int = 0
    estimated_seconds: int = 30
    data_classification: str = "internal"


@dataclass
class PolicyDecision:
    classification: str
    route: Route
    allowed: bool
    reason: str
    rules_version: str = "2026-07-25"
    controls: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "route": self.route.value,
            "allowed": self.allowed,
            "reason": self.reason,
            "rulesVersion": self.rules_version,
            "controls": self.controls,
        }


def _contains(text: str, keywords: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for keyword in keywords:
        if keyword in lowered:
            return keyword
    return None


def classify(request: PolicyInput) -> PolicyDecision:
    """요청을 A~E로 분류한다. 순서가 곧 우선순위다."""
    base_controls: dict[str, object] = {
        "network": "EgressDisabled",
        "maxExecutionSeconds": MAX_EXECUTION_SECONDS,
        "maxUploadBytes": MAX_UPLOAD_BYTES,
        "maxCodeRetries": 2,
        "writeToBusinessSystem": False,
    }

    admin_hit = _contains(request.request_text, ADMIN_KEYWORDS)
    if admin_hit:
        return PolicyDecision(
            classification="E",
            route=Route.DENY,
            allowed=False,
            reason=f"관리자 명령 또는 운영 시스템 직접 변경 요청: {admin_hit}",
            controls=base_controls,
        )

    if request.attachment_bytes > MAX_UPLOAD_BYTES:
        return PolicyDecision(
            classification="C",
            route=Route.ASYNC_COMPUTE,
            allowed=False,
            reason=(
                f"첨부 크기 {request.attachment_bytes} bytes가 "
                f"session 업로드 한도 {MAX_UPLOAD_BYTES} bytes를 초과"
            ),
            controls=base_controls,
        )

    if request.estimated_seconds > MAX_EXECUTION_SECONDS:
        return PolicyDecision(
            classification="C",
            route=Route.ASYNC_COMPUTE,
            allowed=False,
            reason=(
                f"예상 실행 {request.estimated_seconds}초가 "
                f"실행당 한도 {MAX_EXECUTION_SECONDS}초를 초과"
            ),
            controls=base_controls,
        )

    network_hit = _contains(request.request_text, NETWORK_KEYWORDS)
    if network_hit:
        return PolicyDecision(
            classification="D",
            route=Route.CONTROLLED_EGRESS,
            allowed=False,
            reason=f"인터넷 접근이 필요한 요청: {network_hit}. 승인 대기",
            controls=base_controls,
        )

    office_hit = _contains(request.request_text, OFFICE_KEYWORDS) or any(
        name.lower().endswith((".docx", ".xlsx", ".pptx", ".pdf"))
        for name in request.attachment_names
    )
    if office_hit:
        controls = dict(base_controls)
        controls["allowedOperations"] = ["generate", "convert"]
        return PolicyDecision(
            classification="B",
            route=Route.OFFICE_POOL,
            allowed=True,
            reason="Office 문서 생성 또는 변환 요청",
            controls=controls,
        )

    return PolicyDecision(
        classification="A",
        route=Route.PYTHON_POOL,
        allowed=True,
        reason="범용 Python 분석·계산 요청",
        controls=base_controls,
    )


def inspect_code(code: str) -> list[str]:
    """LLM이 생성한 코드에서 정책 위반 pattern을 찾는다.

    이 검사는 sandbox 격리를 대체하지 않는다. 우회는 언제든 가능하므로
    실제 방어선은 Hyper-V session 격리, EgressDisabled, 실행 시간 한도,
    production credential 미주입이다.
    """
    violations: list[str] = []
    for pattern, description in DENIED_CODE_PATTERNS:
        if re.search(pattern, code):
            violations.append(description)
    return violations
