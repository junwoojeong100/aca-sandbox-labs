"""환경 변수 기반 설정.

모든 secret과 endpoint는 backend 프로세스의 환경에서만 읽는다.
사용자 입력이나 LLM 응답이 이 값을 바꿀 수 없다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com"


class ConfigError(RuntimeError):
    """설정이 부족하거나 잘못된 경우."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    return value or None


@dataclass
class Settings:
    """Backend-neutral orchestration, LLM, and artifact settings."""
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
        default_factory=lambda: int(
            _env("EXECUTION_TIMEOUT_SECONDS", "300") or "300"
        )
    )

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
