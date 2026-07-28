"""Backend-neutral deterministic request policy primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

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
    PYTHON = "python"
    OFFICE = "office"
    ASYNC_COMPUTE = "async-compute"
    CONTROLLED_EGRESS = "controlled-egress"
    DENY = "deny"


@dataclass
class PolicyInput:
    tenant_id: str
    user_id: str
    request_text: str
    attachment_names: tuple[str, ...] = ()
    attachment_sizes: tuple[int, ...] = ()
    estimated_seconds: int = 30
    data_classification: str = "internal"


@dataclass
class PolicyDecision:
    classification: str
    route: Route
    allowed: bool
    reason: str
    rules_version: str
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
    return next((keyword for keyword in keywords if keyword in lowered), None)


def classify_base(
    request: PolicyInput,
    *,
    rules_version: str,
) -> PolicyDecision:
    admin_hit = _contains(request.request_text, ADMIN_KEYWORDS)
    if admin_hit:
        return PolicyDecision(
            classification="E",
            route=Route.DENY,
            allowed=False,
            reason=f"관리자 명령 또는 운영 시스템 직접 변경 요청: {admin_hit}",
            rules_version=rules_version,
        )

    network_hit = _contains(request.request_text, NETWORK_KEYWORDS)
    if network_hit:
        return PolicyDecision(
            classification="D",
            route=Route.CONTROLLED_EGRESS,
            allowed=False,
            reason=f"인터넷 접근이 필요한 요청: {network_hit}. 승인 대기",
            rules_version=rules_version,
        )

    office_hit = _contains(request.request_text, OFFICE_KEYWORDS) or any(
        name.lower().endswith((".docx", ".xlsx", ".pptx", ".pdf"))
        for name in request.attachment_names
    )
    if office_hit and request.attachment_names:
        return PolicyDecision(
            classification="B",
            route=Route.OFFICE,
            allowed=False,
            reason="Reference Office Gateway는 사용자 입력 파일 업로드를 지원하지 않는다",
            rules_version=rules_version,
            controls={"inputUploadImplemented": False},
        )
    if office_hit:
        return PolicyDecision(
            classification="B",
            route=Route.OFFICE,
            allowed=True,
            reason="Office 문서 생성, 변환 또는 편집 요청",
            rules_version=rules_version,
        )
    return PolicyDecision(
        classification="A",
        route=Route.PYTHON,
        allowed=True,
        reason="범용 Python 분석·계산 요청",
        rules_version=rules_version,
    )


def inspect_code(code: str) -> list[str]:
    return [
        description
        for pattern, description in DENIED_CODE_PATTERNS
        if re.search(pattern, code)
    ]
