"""환경 변수 기반 설정.

모든 secret과 endpoint는 backend 프로세스의 환경에서만 읽는다.
사용자 입력이나 LLM 응답이 이 값을 바꿀 수 없다.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DYNAMIC_SESSIONS_SCOPE = "https://dynamicsessions.io"
COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com"

PYTHON_API_VERSION = (
    os.environ.get("PYTHON_API_VERSION", "2025-10-02-preview").strip()
    or "2025-10-02-preview"
)
SESSION_API_VERSION = (
    os.environ.get("SESSION_API_VERSION", "2025-02-02-preview").strip()
    or "2025-02-02-preview"
)


class ConfigError(RuntimeError):
    """설정이 부족하거나 잘못된 경우."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    return value or None


def _run(command: list[str]) -> str:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout.strip()


def pool_endpoint(pool_name: str, resource_group: str) -> str:
    """Azure CLI로 pool management endpoint를 조회한다."""
    return _run(
        [
            "az",
            "containerapp",
            "sessionpool",
            "show",
            "--name",
            pool_name,
            "--resource-group",
            resource_group,
            "--query",
            "properties.poolManagementEndpoint",
            "--output",
            "tsv",
        ]
    )


@dataclass
class Settings:
    """Orchestrator 실행에 필요한 설정."""

    resource_group: str = field(
        default_factory=lambda: _env("RESOURCE_GROUP", "rg-ai-workspace-sandbox-lab")
        or "rg-ai-workspace-sandbox-lab"
    )
    python_pool_name: str = field(
        default_factory=lambda: _env("PYTHON_POOL_NAME", "ai-workspace-python-sbx")
        or "ai-workspace-python-sbx"
    )
    office_pool_name: str = field(
        default_factory=lambda: _env("OFFICE_POOL_NAME", "ai-workspace-office-sbx")
        or "ai-workspace-office-sbx"
    )
    python_endpoint: str | None = field(
        default_factory=lambda: _env("PYTHON_ENDPOINT")
    )
    office_endpoint: str | None = field(
        default_factory=lambda: _env("OFFICE_ENDPOINT")
    )
    staging_dir: Path = field(
        default_factory=lambda: Path(
            _env("STAGING_DIR", ".work/agent/staging") or ".work/agent/staging"
        )
    )
    approved_dir: Path = field(
        default_factory=lambda: Path(
            _env("APPROVED_DIR", ".work/agent/approved") or ".work/agent/approved"
        )
    )

    # LLM
    llm_provider: str = field(
        default_factory=lambda: (_env("LLM_PROVIDER", "stub") or "stub").lower()
    )
    azure_openai_endpoint: str | None = field(
        default_factory=lambda: _env("AZURE_OPENAI_ENDPOINT")
    )
    azure_openai_deployment: str | None = field(
        default_factory=lambda: _env("AZURE_OPENAI_DEPLOYMENT")
    )
    azure_openai_api_version: str = field(
        default_factory=lambda: _env("AZURE_OPENAI_API_VERSION", "2024-10-21")
        or "2024-10-21"
    )
    # gpt-5.x 같은 추론 모델의 사고 깊이. low·medium·high 등을 지원한다.
    # 빈 값으로 두면 파라미터를 보내지 않는다.
    reasoning_effort: str | None = field(
        default_factory=lambda: _env("REASONING_EFFORT", "medium")
    )
    # 추론 모델은 추론 토큰도 이 한도를 소비하므로 넉넉히 잡는다.
    max_output_tokens: int = field(
        default_factory=lambda: int(_env("MAX_OUTPUT_TOKENS", "8000") or "8000")
    )

    # 실행 제한
    max_code_retries: int = field(
        default_factory=lambda: int(_env("MAX_CODE_RETRIES", "2") or "2")
    )
    execution_timeout_seconds: int = field(
        default_factory=lambda: int(_env("EXECUTION_TIMEOUT_SECONDS", "220") or "220")
    )

    def resolved_python_endpoint(self) -> str:
        if not self.python_endpoint:
            self.python_endpoint = pool_endpoint(
                self.python_pool_name, self.resource_group
            )
        return self.python_endpoint

    def resolved_office_endpoint(self) -> str:
        if not self.office_endpoint:
            self.office_endpoint = pool_endpoint(
                self.office_pool_name, self.resource_group
            )
        return self.office_endpoint

    def validate_llm(self) -> None:
        if self.llm_provider == "stub":
            return
        if self.llm_provider != "azure-openai":
            raise ConfigError(
                f"Unsupported LLM_PROVIDER: {self.llm_provider}. "
                "Use 'azure-openai' or 'stub'."
            )
        if not self.azure_openai_endpoint or not self.azure_openai_deployment:
            raise ConfigError(
                "AZURE_OPENAI_ENDPOINT와 AZURE_OPENAI_DEPLOYMENT를 설정한다."
            )
