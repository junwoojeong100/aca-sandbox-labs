"""Dynamic Sessions-only environment settings."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

DYNAMIC_SESSIONS_SCOPE = "https://dynamicsessions.io"
PYTHON_API_VERSION = "2025-10-02-preview"
SESSION_API_VERSION = "2025-02-02-preview"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() or None if value is not None else None


def _pool_endpoint(pool_name: str, resource_group: str) -> str:
    result = subprocess.run(
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
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    endpoint = result.stdout.strip()
    if not endpoint:
        raise RuntimeError(f"Session pool endpoint is empty: {pool_name}")
    return endpoint


@dataclass
class Settings:
    resource_group: str = field(
        default_factory=lambda: _env(
            "RESOURCE_GROUP",
            "rg-ai-workspace-dynamic-sessions-lab",
        )
        or "rg-ai-workspace-dynamic-sessions-lab"
    )
    python_pool_name: str = field(
        default_factory=lambda: _env(
            "PYTHON_POOL_NAME",
            "ai-workspace-python-sbx",
        )
        or "ai-workspace-python-sbx"
    )
    office_pool_name: str = field(
        default_factory=lambda: _env(
            "OFFICE_POOL_NAME",
            "ai-workspace-office-sbx",
        )
        or "ai-workspace-office-sbx"
    )
    python_endpoint: str | None = field(
        default_factory=lambda: _env("PYTHON_ENDPOINT")
    )
    office_endpoint: str | None = field(
        default_factory=lambda: _env("OFFICE_ENDPOINT")
    )
    python_api_version: str = field(
        default_factory=lambda: _env(
            "PYTHON_API_VERSION",
            PYTHON_API_VERSION,
        )
        or PYTHON_API_VERSION
    )
    session_api_version: str = field(
        default_factory=lambda: _env(
            "SESSION_API_VERSION",
            SESSION_API_VERSION,
        )
        or SESSION_API_VERSION
    )

    def resolved_python_endpoint(self) -> str:
        if not self.python_endpoint:
            self.python_endpoint = _pool_endpoint(
                self.python_pool_name,
                self.resource_group,
            )
        return self.python_endpoint

    def resolved_office_endpoint(self) -> str:
        if not self.office_endpoint:
            self.office_endpoint = _pool_endpoint(
                self.office_pool_name,
                self.resource_group,
            )
        return self.office_endpoint
