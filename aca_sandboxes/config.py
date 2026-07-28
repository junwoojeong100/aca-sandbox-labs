"""ACA Sandboxes-only environment settings."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() or None if value is not None else None


@dataclass
class Settings:
    subscription_id: str | None = field(
        default_factory=lambda: _env("SUBSCRIPTION_ID")
    )
    resource_group: str = field(
        default_factory=lambda: _env(
            "RESOURCE_GROUP",
            "rg-ai-workspace-aca-sandboxes-lab",
        )
        or "rg-ai-workspace-aca-sandboxes-lab"
    )
    location: str = field(
        default_factory=lambda: _env("LOCATION", "koreacentral")
        or "koreacentral"
    )
    sandbox_group_name: str = field(
        default_factory=lambda: _env(
            "SANDBOX_GROUP_NAME",
            "ai-workspace-sandboxes",
        )
        or "ai-workspace-sandboxes"
    )
    python_disk_image_id: str | None = field(
        default_factory=lambda: _env("PYTHON_SANDBOX_DISK_ID")
    )
    office_disk_image_id: str | None = field(
        default_factory=lambda: _env("OFFICE_SANDBOX_DISK_ID")
    )
    execution_timeout_seconds: int = field(
        default_factory=lambda: int(
            _env("ACA_EXECUTION_TIMEOUT_SECONDS", "900") or "900"
        )
    )

    def resolved_subscription_id(self) -> str:
        if not self.subscription_id:
            result = subprocess.run(
                [
                    "az",
                    "account",
                    "show",
                    "--query",
                    "id",
                    "--output",
                    "tsv",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.subscription_id = result.stdout.strip()
        return self.subscription_id
